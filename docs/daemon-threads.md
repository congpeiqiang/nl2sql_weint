# 守护线程与后台任务生命周期

> 本文档列出本服务里所有**不在请求路径上**的后台执行体（daemon 线程 / 线程池 / asyncio 后台任务）
> 及其启动、停机、退出条件与数据丢失风险。
>
> **修改任何后台执行体（新建、改起停、改退出条件）时，必须同步更新本文档。**

## 0. 先分清两条正交的轴

「后台任务」在这里是三类东西，停机行为**完全不同**，混在一起谈就会得出错误结论：

| 类型 | 进程退出时 | 后果 | 本文档 |
|------|-----------|------|--------|
| `threading.Thread(daemon=True)` | **被强杀**（不 join、不跑 finally） | 被中断的那一轮**丢**，需要靠落盘状态自愈 | §1（9 个） |
| `ThreadPoolExecutor` 工作线程 | **非 daemon**（实测），进程退出时被 **join** | 在飞的长任务会**把停机拖住** | §2（2 处） |
| `asyncio` 后台 Task | 随事件循环一起取消 | 最后一段采样/写入丢失 | §3（1 个） |

Python 3.9 起 `ThreadPoolExecutor` 的工作线程**不再是 daemon**（实测 Py3.13.5）：

```console
$ .venv/Scripts/python.exe -c "import threading,time;from concurrent.futures import ThreadPoolExecutor; \
  ex=ThreadPoolExecutor(1); ex.submit(lambda: time.sleep(0.3)); time.sleep(0.05); \
  [print(t.name, t.daemon) for t in threading.enumerate()]"
MainThread False
ThreadPoolExecutor-0_0 False        # ← 非 daemon
```

⇒ **只有 `daemon=True` 的线程才「进程退出就没了」。** 池里的任务相反：它会拦住退出。

---

## 1. 守护线程清单（9 个）

审计命令（§1 是本表的唯一来源，改代码后重跑它核对）：

```bash
grep -rn "threading.Thread(" src/ --include="*.py" | grep -v "^src/test/"
```

| # | 线程名 | 文件:行 | 启动时机 | 退出条件 | 停机方式 | 中断的后果 |
|---|--------|--------|---------|---------|---------|-----------|
| 1 | `subagent-sync-<子线程前8位>` | `agent/subagents/sync_subagent_todos.py:142` | 每次起子任务（`launch_sync`，**不幂等**） | 子任务终态收尾；或命中下面 4 条守卫之一 | **不由 lifespan 管**：随进程退出被强杀 | 最后一轮进度同步丢失（≤0.5s，可接受）；/state 里的终态由 `pending_terminal` 兜底补 |
| 2 | `eval-drainer` | `agent/eval/eval_queue.py:342` | 首次 LLM-judge 入队（`evaluators.schedule_judge` → `ensure_worker`） | 永不主动退出（`_drain_loop` 是无限循环） | **`stop_worker()` 无调用者**（见 §5） | 正在跑的那条 judge 被打断；行留在 `running`，下次启动 `ensure_worker` 置回 `pending` 重跑 |
| 3 | `pending-terminal-reaper` | `agent/subagents/pending_terminal.py:598` | lifespan `yield` 前（`start_reaper`） | `_STOP` / 进程退出 | `stop_reaper()` ← lifespan 排空**之后** | 残留行留在 SQLite，下次启动**第一轮立刻**重放 |
| 4 | `retention-loop` | `agent/utils/retention.py:662` | lifespan `yield` 前（`start_maintenance`） | `_STOP` / 进程退出 | `stop_maintenance()` | 没跑完的那一轮下次继续，不留半截状态 |
| 5 | `embed-warmer` | `agent/retrieval/embedder.py:524` | lifespan `yield` 前（`start_warmer`，`NL2SQL_EMBED_WARMUP_INTERVAL<=0` 时不起） | `_WARMER_STOP` / 进程退出 | `stop_warmer()`（join 3s） | 无落盘状态，纯保活请求，丢了无所谓 |
| 6 | `mcp-sub-reload` | `agent/tools/mcp_tool.py:649` | 语义库增删改后（`reload_sub_entries_in_background`，已在跑则**不排队**直接返回 False） | 单趟 `refresh_sub_entries()` 跑完即退 | 无（生命周期是「一趟」） | 这趟对账白做；下一个写路径会再次触发 |
| 7 | `langfuse-feedback` | `api/message_feedback.py:400` | 用户提交反馈时 | 单次打分跑完即退 | 无（fire-and-forget） | 该条反馈分数没上 Langfuse（本地 SQLite 已有，看板口径本就读本地） |
| 8 | `langfuse-feedback-revoke` | `api/message_feedback.py:450` | 单条撤销时 | 同上 | 无 | 该条撤销哨兵没写；`_find_trace_with_retry` 最多重试 3 轮 |
| 9 | `langfuse-feedback-revoke-batch` | `api/message_feedback.py:484` | 批量撤销时（**一个**线程串行写，上限 `_REVOKE_BATCH_LIMIT=100`） | 同上 | 无 | 剩余未写的撤销哨兵丢失（返回的是**已排队**条数，不是已写入） |

### 1.1 #1 sync 线程的四条守卫（为什么它不会变成僵尸）

`_run_sync_loop` 主循环是 `while True`，靠以下上限打破；**任何一条触发都是「放手」而不是「不写」**
——超限的终态由 `pending_terminal` 补写：

| 常量 | 值 | 含义 |
|------|-----|------|
| `POST_COMPLETE_MAX_CYCLES` | 20 | 完成后继续监控 10s（20×0.5s）让耗时稳定 |
| `STALE_RUN_TIMEOUT` | 600 | **无进展**持续 10min 视为卡死（2026-10-16 起改成停滞口径，不再按「跑了多久」） |
| `ABSOLUTE_RUN_TIMEOUT` | 3600 | 单条 run 总时长上限 1h（兜死循环/无限重写；停滞判据之外的第二道） |
| `STALL_PROBE_EVERY_CYCLES` | 8 | 进展探针间隔（8×0.5s=4s；探针要 `get_state`，别每轮都探） |
| `INTERRUPTED_STUCK_TIMEOUT` | 120 | 子 run 停在 `interrupted` 超 2min 视为卡死 |
| `COMPLETE_WRITE_MAX_SECONDS` | 300 | 完成态写入重试上限（主线程持续 in-flight 时放手） |
| `REGRESSION_GUARD_MAX_SECONDS` | 900 | 终态被陈旧快照打回后，最多再守 15min |
| `REGRESSION_GUARD_MAX_REWRITES` | 200 | 上述守护的重写次数上限（防病态刷写） |
| `_AWAIT_APPROVAL_TIMEOUT` | 7200 | 历史审批路径保留；审批已改为只读硬拦截 |

退出时 `finally` 会 `loop.close()` 并从 `_ACTIVE_LOOPS` 摘掉自己（只在仍是登记的那个线程时才删）。
`_ACTIVE_LOOPS` 的用途是 `is_sync_alive` 判重（**调用方必须先判，`launch_sync` 本身不幂等**）。

---

## 2. 非 daemon：两个线程池（会把停机拖住）

| 池 | 文件:行 | 规模 | 关停 |
|----|--------|------|------|
| `offload-long` | `agent/utils/offload.py:47` | `NL2SQL_LONG_OFFLOAD_WORKERS`（默认 4）；import 时创建（首次 submit 前不建线程） | **无 `shutdown()`** |
| path_resolver 回收池 | `agent/utils/path_resolver.py:795` | 每个具名池 `max_workers` 个；满载超时后整个换新池（旧池在锁外 `shutdown`） | 仅回收时关旧池 |

另有 `asyncio.to_thread`（`offload`）走**事件循环默认执行器**，也是非 daemon。

**这是本服务唯一能真正拖住停机的后台执行体。** `offload_long` 的典型负载是秒级到分钟级
——语义库构建/更新（`wren context build`、`git archive` 物化）就挂在它上面。一个在飞的
8 分钟构建会让 uvicorn 收完信号后迟迟退不出（表现为蓝绿脚本「停旧色」超时）。

排查：停机卡住时看线程栈里是否有 `offload-long_*` / `path_resolver` 名字的线程；
这些任务**不落盘中间态**，被强杀就得重来。

---

## 3. 非线程：asyncio 后台 Task

`create_task` 起的任务不是线程，§0 的两条轴都不适用——随事件循环收尾（被 cancel）。

审计命令：

```bash
grep -rn "create_task(" src/ --include="*.py" | grep -v "^src/test/"
```

### 3.1 lifespan 管的（只有 1 个）

| 任务 | 文件:行 | 启动 | 停止 |
|------|--------|------|------|
| `nl2sql-metrics-sampler` | `api/metrics.py:650` | lifespan `yield` 前（`_metrics.start()`） | lifespan **最后**（`await _metrics.stop()` → `task.cancel()`） |

放在停机顺序最后：排空期间还要看得见 `nl2sql_run_queue_*` 在往下掉，不能提前停。

### 3.2 请求内起、**没有**停机钩子的（3 处）

| 任务 | 文件:行 | 生命周期 | 中断的后果 |
|------|--------|---------|-----------|
| 离线 Experiment run | `api/experiment.py:481`（`_RUN_TASKS[stamp]`） | 一次跑完整个数据集，分钟级到小时级 | **跑到一半被 cancel**；状态文件停在 `running`。见 §5.4 |
| 反馈快照补齐 | `api/message_feedback.py:277`（`_backfill()`，不持引用） | 单趟补齐，秒级 | 该轮补齐白跑，下次反馈再补 |
| 工具超时包装 | `agent/utils/path_resolver.py:1198` | `asyncio.wait({task}, timeout)`；超时后 `task.cancel()` | **有意**让底层调用在后台继续跑、结果丢弃（注释明写「后台继续运行也无妨」）——这不是泄漏，是设计 |

这三处都不在 `drain` 的计数里（`drain` 读的是 langgraph 的 worker 数），所以**排空不会等它们**。

---

## 4. 停机顺序（`api/custom_app.py` 的 `_lifespan`，实测）

`yield` 前（起，顺序固定）：

```
adopt_all_stores()      # 运行时库归位 —— 必须最早：搬运前提是「没人打开过老库」
_metrics.start()        # 指标采样
start_reaper()          # P2-4 终态补写器
start_maintenance()     # P2-5 保留策略
start_warmer()          # 2026-10-13 嵌入保活
```

`yield` 后（停）：

```
1. api.drain.begin_drain() + await wait_for_idle()   # 等在跑的 run 排空
2. flush_langfuse()                                  # 让最后一批 run 的 trace 也进本次 flush
3. stop_reaper()        # 放最后几件里最前：排空期间它还在补写，正是最需要的时机
4. stop_maintenance()
5. stop_warmer()
6. await _metrics.stop()
```

每一步都 `try/except` 吞掉异常 + warning——**任何一件起不来或停不掉都不拦服务**。
`atexit` 兜底：`agent/trace/langfuse_client.py:1145`（覆盖不经 uvicorn lifespan 的进程，
如 `collect_badcase` / feedback 批处理脚本）。

排空计数的口径见 `api/drain.py` 模块头：读的是 `langgraph_runtime_inmem.queue` 的
worker 数（**含子 agent run 与 §1#1 的 sync 循环**，一次问数约 3 个）。读不到就返回 0
并标记 `trusted=False`（`drain_state()` 会告诉发版脚本「这个 0 是真的还是读不到」），
避免把发版凭空拖住。

---

## 5. 已知缺口

1. **`eval_queue.stop_worker()` 在生产从不被调用。** 它定义了、写了 docstring（「测试/优雅
   停机用」），但 `src/` 与 `scripts/` 里**没有任何调用者**——lifespan 没接。所以
   `eval-drainer` 实际是「起一次、活到进程被强杀」。数据不丢（队列在 SQLite，重启即续跑），
   但正在跑的那条 judge 会被打断、留 `running` 行到下次启动回收。
   **未修**——要修就是往 `custom_app._lifespan` 的停机段加一行 `stop_worker()`，
   顺序放在 `stop_reaper()` 附近。
2. **`mcp-sub-reload` 无任何退出上限。** 它跑的是 `refresh_sub_entries()`，会去连 MCP 服务器；
   若某个服务器 hang 住，这个线程会一直挂着（不阻塞停机——它是 daemon——但也不回收）。
3. **§2 的两个池没有停机钩子。** 见上。
4. **离线 Experiment run 重启后留僵尸 `running`。** `_RUN_TASKS`（`api/experiment.py:50`）
   是**纯内存**注册表，进程重启即空，而状态文件里的 `running` 还在。代码已按此设防
   （`experiment.py:734` 注释：为空的注册表 ⇒ 按 `running` 拒绝，不静默重跑）。
   没有停机钩子去主动把它标成中断——**已知、已被接受**。

---

## 6. 修改规则

- 新建后台执行体时，**先决定它属于 §0 的哪一类**，再选守护方式；不确定就用 `daemon=True`
  （宁可丢一轮，不可拦停机）。
- 需要跨停机保留状态的，**必须落盘**（照 §1 里 `pending_terminal` / `eval_queue` 的做法：
  启动时先跑一轮回收残留）。
- 改变了起停时机或退出条件 ⇒ 改本表对应行；改了停机顺序 ⇒ 改 §4。
- 核对命令：

```bash
grep -rn "threading.Thread(" src/ --include="*.py" | grep -v "^src/test/"
grep -rn "ThreadPoolExecutor\|create_task(" src/ --include="*.py" | grep -v "^src/test/"
```
