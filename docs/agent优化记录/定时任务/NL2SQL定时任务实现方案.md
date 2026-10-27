# NL2SQL 定时任务实现方案

> 2026-10-21。目标：用户可以创建定时任务（cron / 固定间隔 / 一次性），到点由后端**以该用户身份**自动发起一次问数 run，结果落在一个新会话里；用户能管理任务（改 / 暂停 / 删 / 立即运行）并查看运行历史。
> 设计骨架参考 hermes-agent（`D:\code_work_space\llm\hermes-agent-main`）的 cron 子系统——它把 misfire、at-most-once、重入、时区、双进程并发这些坑全部踩完并写成了注释；本方案将其映射到 nl2sql 现成基础设施（custom_app 组合根 / lifespan worker 三先例 / SQLite 一库一目录 / 服务端自建 run 链路 / thread 归属补打）。

---

## 1. 总体架构

```
前端「定时任务」面板 ──REST──> src/api/scheduled_tasks.py ──> agent/scheduler/service.py
聊天工具 manage_scheduled_task（阶段3）──────────────────────────┘（同一 service，无双实现）
                                        │
                          agent/scheduler/store.py（SQLite，一库一目录）
                                        │
custom_app._lifespan ── yield 前 start_worker()：daemon 线程，每 60s tick
                                        │
              tick：文件锁(.tick.lock) → 取 due → CAS 认领（执行前先推进 next_run_at）
                                        │ fire-and-forget
                                        ▼
              langgraph_sdk.get_client(url=_api_base_url())
              threads.create(metadata) + runs.create(config=owner 身份)
                                        │
              下一轮 tick reconcile：查 run 终态 → 回写 last_status / task_runs
```

核心不变量（全部来自 hermes 的实证取舍）：

1. **`next_run_at` 持久化在任务表里**，调度器无内存状态——重启恢复 = 第一个 tick 按 grace 规则重判 due，天然抗重启。
2. **at-most-once**：执行**前**先原子推进 `next_run_at`（SQLite CAS UPDATE），进程中途死掉最多丢一次触发，绝不 crash-loop 重复触发（hermes `jobs.py:1389` 原话："missing one run is far better than firing dozens of times in a crash loop"）。
3. **同任务不重入**：上一次还没跑完、下一次到点 ⇒ 本轮直接跳过（不排队）。
4. **执行 = 全新会话**：定时 run 不共享任何聊天上下文，prompt 必须自包含。

## 2. 数据模型（`agent/scheduler/store.py`）

SQLite 库：`<AGENT_DATA_ROOT>/scheduled_tasks/scheduled_tasks.sqlite`（落点见下节）。
- 接入 `agent/utils/sqlite_paths.py::adopt_all_stores()` 的归位清单（custom_app lifespan 第一步，先于 worker 启动）。
- 遵守既有铁律：**共享连接 + 一把锁，读也要进锁**（并发读互踩是已发过版的教训）。

### 存储位置与连接信息

**没有网络连接**。这是嵌在后端进程里的 SQLite 文件库，不起独立服务、没有 host/port/账号/密码，也不需要配连接串——所谓「连接信息」只有三样：**库文件路径**、**进程内连接参数**、**同目录的附属文件**。

| 项 | 值 |
|---|---|
| 库文件 | `<AGENT_DATA_ROOT>/scheduled_tasks/scheduled_tasks.sqlite` |
| 目录规则 | 一库一目录：`<root>/<name>/<name>.sqlite`（`sqlite_paths.store_db_path()`，目录名即库名） |
| 容器内（prod 64 / dev 34） | `AGENT_DATA_ROOT=/app/data`（`.env.prod:56` / `.env.dev:13`）→ `/app/data/scheduled_tasks/scheduled_tasks.sqlite` |
| 宿主机落点 | 命名卷 `agent_data` 挂到 `/app/data`（`docker-compose.yml:38`）。**蓝绿两色共用同一个卷**，切色不丢任务；卷名见线上 `docker inspect` 的 Mounts |
| 本机开发 | `.env:117` 的 `AGENT_DATA_ROOT=D:\nl2sql_data` → `D:\nl2sql_data\scheduled_tasks\scheduled_tasks.sqlite` |
| 未配置 `AGENT_DATA_ROOT` | 回退仓库内 `src/agent/scheduled_tasks/`（旧部署兼容；`sqlite_paths.data_root()`） |

> ⚠️ `AGENT_DATA_ROOT` 是**进程 import 期冻结**的（`workspace_manager/manager.py:61` 模块级读取）：运行中改 `.env` 或改环境变量都不会换路径，必须重启进程。给测试/脚本设暂时目录时也一样——只 `os.environ` 不重启 = 静默写到真实数据根。

同目录附属文件（**备份/迁移必须一起带走**）：

| 文件 | 作用 |
|---|---|
| `scheduled_tasks.sqlite-wal` / `-shm` | WAL 模式附属文件。只拷主库会丢掉「已提交但未 checkpoint」的事务 |
| `.tick.lock` | 跨进程 tick 互斥（`fcntl` / `msvcrt` 文件锁）。蓝绿两色同时在线时，**只有抢到锁的那一色在跑调度** |
| `heartbeat` | 每 tick 写一次 UTC ISO；`/metrics` 与排障用它判断「调度线程还活着吗」 |

连接参数（`ScheduledTaskStore.__init__`，`agent/scheduler/store.py:129`）：

- `sqlite3.connect(path, check_same_thread=False, timeout=15.0)` —— **进程内单例共享一条连接**（`get_store()`），所以关掉 `check_same_thread`；
- `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=15000`；
- **所有访问（含读）都进同一把 `threading.RLock`**——共享连接下并发读会互踩；
- 建库时机：首次 `get_store()` 自动建目录 + 建表 + 增量迁移（`PRAGMA table_info` 逐列 `ALTER TABLE`，幂等）；老位置 `<root>/scheduled_tasks.sqlite` 三件套由 `adopt_all_stores()` 在 lifespan 首步原子搬运归位（先搬 `-wal/-shm`，主库最后）。

排障速查（容器内）。⚠️ **镜像没有 `sqlite3` CLI**（Dockerfile 只装了 gcc/libmariadb3/curl/git/openssh-client），用容器自带的 python 查：

```bash
# 目录 + 心跳 + 锁（不需要 sqlite3）
ls -l /app/data/scheduled_tasks/        # 库 + -wal/-shm + .tick.lock + heartbeat
cat /app/data/scheduled_tasks/heartbeat # 调度线程最近一次心跳（UTC ISO），很久不更新=worker 没跑

# 任务清单（谁、什么状态、下次何时跑、固化状态）
docker exec <后端容器> python -c "
import sqlite3
c = sqlite3.connect('/app/data/scheduled_tasks/scheduled_tasks.sqlite', timeout=15)
for r in c.execute('select id,owner,name,state,enabled,next_run_at,last_status,pin_status '
                   'from scheduled_tasks order by created_at desc limit 20'):
    print(r)
"

# 最近流水（含固化走的是 replay 还是 fallback）
docker exec <后端容器> python -c "
import sqlite3
c = sqlite3.connect('/app/data/scheduled_tasks/scheduled_tasks.sqlite', timeout=15)
for r in c.execute('select id,task_id,fired_at,status,pin_mode,pin_reason '
                   'from task_runs order by id desc limit 20'):
    print(r)
"
```

常见排查口：

```sql
-- 「现在就该跑却没跑」：下次触发时间已过、仍是 scheduled
select id,name,next_run_at from scheduled_tasks
 where enabled=1 and state='scheduled' and next_run_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now');
-- 「卡在 running」：running_thread_id 非空的都是没收到终态的
select id,name,running_thread_id,last_run_at from scheduled_tasks where state='running';
-- 「固化为什么失效」
select id,name,pin_status,pin_invalid_reason from scheduled_tasks where pin_status='invalid';
```

备份用在线热备，**别裸 `cp`**（WAL 下会拿到不一致快照）：

```bash
docker exec <后端容器> python -c "
import sqlite3
src = sqlite3.connect('/app/data/scheduled_tasks/scheduled_tasks.sqlite', timeout=15)
dst = sqlite3.connect('/tmp/scheduled_tasks.bak')
src.backup(dst); dst.close(); src.close()
print('ok')
"
docker cp <后端容器>:/tmp/scheduled_tasks.bak ./scheduled_tasks.bak
```

**数据保留**：`task_runs` 保留 **90 天**（`RUNS_RETENTION_DAYS`），worker 每 **1 小时**至多清一次（`PURGE_INTERVAL_S`，进程内节流；蓝绿各清一次，`DELETE` 幂等）。`scheduled_tasks` 本体不自动清理——删任务时连带删该任务的流水。

### 表 `scheduled_tasks`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | uuid4 hex 前 12 位（不可变，兼作对外标识） |
| `owner` | TEXT | user_id。**服务端只信这一列**，触发时的身份从库里读，不信任何请求体 |
| `name` | TEXT | 任务名 |
| `prompt` | TEXT | 自包含问数内容（如「统计上月各部门工时并生成报告」） |
| `db_name` | TEXT | 目标库（创建时按用户 grants 校验） |
| `llm_route` / `llm_model` | TEXT NULL | 空 = 触发时跟随该用户当前默认；非空 = 创建时固化快照（防「配置漂移 → 计划外消费」，hermes #44585 同款） |
| `sched_kind` | TEXT | `cron` / `interval` / `once` |
| `sched_expr` | TEXT | cron 表达式（5 字段）/ 间隔分钟数 / ISO 时间点 |
| `sched_display` | TEXT | 人类可读（「每天 09:00」），创建时生成，前端只读展示 |
| `tz` | TEXT | IANA 时区名，默认 `Asia/Shanghai` |
| `next_run_at` | TEXT | **UTC ISO、T 口径**（保留策略同款口径）；CAS 认领的比较键 |
| `enabled` / `state` | INT / TEXT | state ∈ scheduled / paused / completed / error |
| `repeat_times` / `repeat_completed` | INT NULL / INT | 有限次任务；执行前预扣 completed（one-shot 也不无限重发，hermes `claim_dispatch` 同款） |
| `running_thread_id` | TEXT NULL | 非空 = 在飞（防重入 + reconcile 锚点） |
| `last_run_at` / `last_status` / `last_error` | TEXT | 最近一次执行结果 |
| `created_at` / `updated_at` | TEXT | UTC ISO |
| `pin_*`（6 列） | TEXT | 固化查询：`pin_status` / `pin_spec` / `pin_semantic_version` / `pin_saved_at` / `pin_invalid_reason` / `pin_source_thread_id`（2026-10-23 新增） |

### 表 `task_runs`（运行历史）

`id, task_id, thread_id, run_id, fired_at, finished_at, status, error, pin_mode, pin_reason`。
reconcile 写终态；前端运行历史直接查这张表并链到 thread。

## 3. 调度器（`agent/scheduler/worker.py`）

**生命周期**：`custom_app._lifespan` 里 yield 前 `start_worker()`、停机段**最先** `stop_worker()`——排在 `api.drain.begin_drain()` 之前，排空窗口内不再产生新 run。起不来只 warning、绝不拦启动（与 `start_reaper` / `start_maintenance` / `start_warmer` 三先例逐字同款）。

**tick 循环**（daemon 线程，60s 一轮，`threading.Event.wait` 可中断）：

1. **跨进程锁**：非阻塞抢 `<AGENT_DATA_ROOT>/scheduled_tasks/.tick.lock`（fcntl），抢不到本轮直接结束。蓝绿切换窗口两色并存时只有一边 tick（hermes desktop/gateway 共存同款方案）；单实例部署下它是零成本保险。
2. **取 due**：`enabled=1 AND state='scheduled' AND next_run_at <= now`，含 misfire 判定：
   - **grace = 调度周期的一半，clamp 到 [120s, 2h]**（周期用 croniter 连取两次 next 差值估算）；
   - 迟到在 grace 内 → 正常补跑；
   - 超出 grace → **折叠全部积压**：`next_run_at` 快进到下一个未来时间点，**但仍立即执行一次**——既防重启后 burst 补发刷屏，又防「运行时长 > 间隔」的任务被永久跳过（hermes #33315）。
3. **CAS 认领（执行前）**：`UPDATE scheduled_tasks SET next_run_at=<新值>, running_thread_id=<占位> WHERE id=? AND next_run_at=<旧值>`——affected=0 即被别的触发路径（手动 run / 另一色）抢走，跳过。
4. **派发**：每轮至多 fire **3** 个任务（交互式问数优先，全局只有 10 个 run 槽），其余留到下轮（next_run_at 已被推进的按认领值顺延一个 tick 重试，不算错过）。fire 在独立执行线程里 `asyncio.run(...)`（tick 线程不被 run 创建阻塞；文件/SQLite 操作不落在事件循环上，P1-14 口径）。
5. **reconcile**：每轮扫 `running_thread_id` 非空的任务 → `client.runs.list(thread_id)` 取终态 → 写 `last_status/last_error/finished_at` 到任务行与 `task_runs`，清 `running_thread_id`。run 挂死不需要调度器自己管——**既有看门狗**（600s 无进展 / 3600s 绝对）会把它打成终态，reconcile 照常收尾。
6. **心跳**：每轮 touch `scheduled_tasks/heartbeat` 文件；`/metrics` 暴露 `nl2sql_sched_tasks`、`nl2sql_sched_fires_total{outcome}`、worker 存活与最近心跳龄（hermes 三态诊断思路：进程活着 ≠ 线程活着 ≠ tick 持续成功）。

**时区**：`NL2SQL_SCHEDULER_TZ` env（默认 `Asia/Shanghai`），`zoneinfo` + croniter tz-aware 计算；naive 输入一律按配置时区解释（hermes #51021：按服务器 UTC 解释会错位数小时）；持久化统一 UTC。

## 4. 执行链路（fire 一个任务）

完全复用服务端自建 run 的既有链路（`sync_subagent_todos.py` 已验证的路径）：

1. `client = langgraph_sdk.get_client(url=_api_base_url())`——HTTP 自调用而非 in-process ASGI（后台线程没有主进程 ASGI 上下文，`get_client(url=None)` 会炸，:312-316 有现成注释）。
2. `threads.create(metadata={"scheduled_task_id": id, "title": f"[定时] {name}"})`。
3. `runs.create(thread_id, assistant_id="chat_agent", input=..., config=...)`：
   - input 消息：`[定时任务] {name}：{prompt}（这是定时自动执行，请直接完成数据查询并给出结果）`；
   - config：`{"recursion_limit": 500, "configurable": {"langgraph_auth_user_id": owner, "user_id": owner, "db_name": db_name}}`，pinned 时追加 `llm_route/llm_model`——契约与 `_run_context_config`（:1945）逐字一致；**`db_name` 必须带**，缺了它主 agent 系统提示丢「当前数据库」段、报告口径核验永久降级（2026-09-26 生产实证）。
4. **归属自动正确**：threads.create 钩子先打 `owner="internal"`，`langfuse_metadata._apply_ownership` 再按 run configurable 里的真实 user_id 补打归属（子 agent 线程同款机制，`auth/ownership.py:47-55` 写明了为什么必须记真实用户）⇒ 会话出现在该用户的会话列表，本人可读。
5. **失败路径**：`runs.create` 抛错（如排空中 503、run 槽满）⇒ 记 `last_error`，`next_run_at` 回拨为 now+5min 重试，最多 3 次后按错过处理（折叠到下一个正常周期）。
6. fire-and-forget：不等 run 结束，终态由下一轮 tick 的 reconcile 收口。

**结果投递**：不做站外推送。结果就在 `[定时]` 会话里（含图表 / 报告 / 失败说明，与手动问数完全同构），运行历史面板给出跳转链接。这是 hermes「默认隔离、结果可寻址」取舍的最小实现——nl2sql 没有多平台 adapter，会话列表本身就是投递渠道。

## 5. API（`src/api/scheduled_tasks.py`）

组合根接线：`custom_app.py` import 一行 + `ROUTES` 展开一行。全部经 AuthMiddleware；owner 隔离强制（非 admin 只能碰自己的任务）；**403 必须「返回」JSONResponse 不能「抛」**（langgraph 会把异常折成 `{"detail":…}`，前端只认 `.error`）。

| 端点 | 说明 |
|---|---|
| `GET /api/scheduled-tasks` | 我的任务列表（admin `?all=1` 看全部），含 next_run_at / last_status |
| `POST /api/scheduled-tasks` | 创建。校验：croniter 解析表达式、prompt 非空且 ≤4000 字、db_name 在用户 grants 内、周期 ≥5 分钟、每用户 ≤20 个 |
| `PATCH /api/scheduled-tasks/{id}` | 改名 / 改 prompt / 改周期 / pause / resume（resume 重算 next_run_at，不补历史） |
| `DELETE /api/scheduled-tasks/{id}` | 删除（在飞 run 不打断，reconcile 自然收尾后无家可归的行按 task_id 清掉） |
| `POST /api/scheduled-tasks/{id}/run` | 立即运行一次——与 tick 走**同一个 CAS 认领**，双路触发天然互斥 |
| `GET /api/scheduled-tasks/{id}/runs` | 运行历史（task_runs 分页） |

service 层（`agent/scheduler/service.py`）承载全部校验与 CRUD，API 与阶段 3 的聊天工具都只调它——hermes 的核心纪律："there is NO second job engine"。

## 6. 前端（harness-deep-agents-ui）

侧边栏新增「定时任务」面板（与「文件 (状态)」触发器同款交互），阶段 2 交付：

- **列表**：名称、`sched_display`、下次运行（本地时区）、上次结果（✓/✕/⏳ 图标 + 错误截断）、暂停/删除/立即运行按钮。
- **创建/编辑弹层**：名称；问数内容（textarea，占位提示「请写完整的自包含问题，定时执行时没有当前聊天上下文」）；数据库下拉（复用现有库列表组件）；周期选择器双模式——简单模式（每小时/每天/每周 + 时刻）与高级模式（cron 表达式 + 实时预览未来 3 次运行时间），人类可读 ↔ 表达式双向转换照抄 hermes `web/src/lib/schedule.ts` 的 describeSchedule 思路。
- **运行历史**：任务行展开，每条 = 时间 + 状态 + 「打开会话」链接（跳 `[定时]` thread）。
- 定时触发的会话本身零改动：标题前缀 `[定时]` 自然出现在会话列表，消息流、进度卡、图表/报告渲染全部复用现状。

## 7. 聊天内创建（阶段 3）

主 agent 工具 `manage_scheduled_task`（单工具多 action：`create/list/pause/resume/remove/run_now`，schema 参照 hermes `tools/cronjob_tools.py:960` 的 CRONJOB_SCHEMA 裁剪）：

- 用户说「每天早上 9 点给我跑上月部门工时报告」→ 主 agent 调工具创建，`user_id/db_name` 从 runtime config 取（子 agent 透传同款机制），并盖章 `origin_thread_id` 便于回溯创建现场。
- prompt 原样存储、触发时作为 user 消息注入——不进系统提示，创建期校验与 REST 完全同一套（service 层复用）。
- ⚠️ 工具模块**禁止 `from __future__ import annotations`**（`ToolRuntime` 注入会静默失效，判据 `'runtime' in tool._injected_args_keys`）。
- 主 agent 提示词补一段：创建成功后向用户复述 `sched_display` 与下次运行时间。

## 8. 安全与配额

| 项 | 规则 |
|---|---|
| 身份 | 触发身份只从库里 `owner` 读；API 层非 admin 强制 owner 过滤（grants 四面过滤同款） |
| 库权限 | 创建/修改时 `db_name` 按用户 grants 校验；触发前再验一次（授权可能已被回收，fail-closed：验不过记 error 不 fire） |
| 配额 | 每用户 ≤20 任务；周期 ≥5 分钟；每 tick 全局 fire ≤3——定时任务永不挤爆 10 个 run 槽 |
| 保留 | `task_runs` 保留 90 天（**已实施**：worker tick 内每 1h 清一次）；`[定时]` 会话 thread 清理（每任务保留最近 30 个）**未实施、归属后续阶段** —— 删 thread 是破坏性操作，既有 retention 维护线程只覆盖文件/trace/eval_queue/feedback、不碰 langgraph threads，需单独 E2E 后再上（⚠️ 别把这条当成「已实施」） |
| 发版窗口 | 排空中 DrainGate 拒新 run ⇒ 走 5 分钟回拨重试，蓝绿切换天然不丢触发 |
| 危险面 | 无 shell / 无脚本执行（hermes 的 no_agent 模式不搬）；prompt 是纯问数内容 |

## 9. 依赖与构建闸

- **不新增任何依赖、不动 `pyproject.toml`/`uv.lock`**（2026-10-21 实施时核实）：`croniter 6.2.4` 已是 `langgraph-runtime-inmem 0.31.1` 的硬依赖、在 `uv.lock` 里锁定 ⇒ 镜像里必然存在，`.venv` 实测 `import croniter` + tz-aware 解析通过。
- 这同时绕开了构建闸三坑（`[tool.*]` 表头吞依赖、离线 lock 被剥光、本地 .venv 好 ≠ 镜像能构建）——零 lock 变更 = 零构建闸风险。
- 若未来 langgraph-runtime-inmem 移除 croniter 依赖，再把 `croniter` 显式加进 `dependencies = [...]` 列表**内部**并在有网环境重新生成 `uv.lock`（坑的原文保留如下）。
- ⚠️ 必须加在 `pyproject.toml` 的 `dependencies = [...]` 列表**内部**——任何 `[tool.*]` 表头之后的行都不属于 dependencies（2026-10-14 事故：表头插错位置 ⇒ 依赖被吞 ⇒ 镜像构建闸炸）。
- ⚠️ 加依赖后必须在有网环境重新生成 `uv.lock` 并一起提交（离线 lock 会被剥光，构建闸三坑之二）；部署机有外网。
- 本地 `.venv` 验证通过 ≠ 镜像能构建：提交前跑一次镜像构建闸。

## 10. 实施拆分与验收

| 阶段 | 内容 | 预估 |
|---|---|---|
| 1 后端 | store + worker（tick/grace/CAS/reconcile/心跳）+ fire 链路 + REST API + metrics 接线 | 2~3 天 |
| 2 前端 | 面板 + 创建表单（周期选择器）+ 运行历史 | 1~2 天 |
| 3 聊天工具 | `manage_scheduled_task` + 主 agent 提示词 | 1 天 |

**单测**（选题清单直接继承 hermes `tests/cron/` 的覆盖面）：misfire 折叠（grace 内补跑 / 超 grace 快进+仍跑一次）、CAS 双路互斥（tick vs 手动 run）、同任务在飞跳过、有限次 one-shot 预扣、时区解释（naive 按配置时区）、reconcile 终态回写、配额与 grants 校验。

**E2E（dev 34）**：
1. 创建「每 1 分钟」任务 → 1~2 个 tick 内出现 `[定时]` 会话且归属正确（用户可读、他人 404）；
2. 暂停 / 恢复 / 删除各验一遍 next_run_at 行为；
3. 重启后端 → 停机 <grace 补跑、停机 >grace 折叠只跑一次（无 burst）；
4. 蓝绿切换窗口：两色并存时同一任务只 fire 一次（`.tick.lock` + CAS）；
5. run 槽打满时触发 → 5 分钟回拨重试路径生效；
6. 手动 `run` 与 tick 同秒竞争 → 恰好一次。

## 11. hermes → nl2sql 映射速查

| hermes 机制（文件） | nl2sql 落点 |
|---|---|
| `cron/jobs.py` 存储 + parse_schedule + grace/misfire + claim CAS | `agent/scheduler/store.py` + `service.py`（SQLite 表替代 jobs.json，CAS 用原子 UPDATE 替代文件锁+CAS） |
| `cron/scheduler_provider.py` InProcessCronScheduler（daemon 线程 60s tick） | `agent/scheduler/worker.py`（custom_app lifespan 先例接线） |
| `.tick.lock` 跨进程锁（gateway/desktop 共存） | 同款文件锁（蓝绿两色共存） |
| `run_job` 一次性 AIAgent fresh session | `threads.create + runs.create`（`sync_subagent_todos.py:2035` 先例） |
| origin 回投 / mirror_to_session / [SILENT] | 不搬——结果落 `[定时]` 会话 + 运行历史跳转（单一投递面，无刷屏问题） |
| `cronjob_tools.py` 单工具多 action | 阶段 3 `manage_scheduled_task` |
| `hermes_time.py` 配置化时区 | `NL2SQL_SCHEDULER_TZ` + zoneinfo |
| ticker heartbeat 双文件三态诊断 | heartbeat 文件 + `/metrics` 指标 |
| `web/src/lib/schedule.ts` 周期选择器 | 前端创建表单同款双向转换 |
