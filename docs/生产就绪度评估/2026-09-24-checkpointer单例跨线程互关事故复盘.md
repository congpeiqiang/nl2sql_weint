# 2026-09-24 生产问数全挂复盘：checkpointer 单例 `_cm` 被 GC 关掉别的线程的连接

> 影响：**所有问数 100% 失败**（含 `POST /threads/{id}/state` 500），持续约 **1 小时**（14:07 发版 → 15:09 修复重启），期间**重启无法恢复**。
> 定级：P0（生产不可用 + 重启不恢复 + 与前一次发版同时发生，看起来像"发版炸了"）。

## 一、一句话根因

`src/agent/checkpoint/checkpointer_factory.py` 的 `checkpointer` 是**模块级单例**，`self._cm` 是**一个共享可变槽位**；而 langgraph_api 是**每个线程各调一次** `get_checkpointer()` 并把 saver 缓存进 `threading.local()`，且用 `hasattr` 守卫 ⇒ **只建不改、永不重建**。

于是新线程 `__aenter__` 覆盖 `self._cm` 时，上一个线程的 `@asynccontextmanager` 对象失去最后一个引用 → **被 GC** → 异步生成器终结器执行 `from_conn_string` 里的 `finally: await conn.close()`（`async with AsyncConnection.connect(...)` 的退出路径）→ **关掉上一个线程仍在用的那条 PG 连接**。逐线程递推：线程 2 弄死线程 1，线程 3 弄死线程 2……**启动后第一波 run 就已经中毒**，所以「重启」只能重置一次。

三个条件缺一不可，这也是它为什么隐蔽：
1. 单例 + 共享槽位（我们的代码）；
2. 每线程一个 saver 且**永不重建**（langgraph 的线程本地缓存，`_checkpointer/_adapter.py:65` + `:274`）；
3. GC 终结器会执行被弃 `@asynccontextmanager` 的 `finally`（Python/asyncio 语义，已本地复现）。

## 二、证据链

**报错（每次 2~6ms 内返回）**：

```
MainThread → langgraph_api/api/threads.py:240 get_thread_state
  → langgraph_runtime_inmem/ops.py:1573 get
  → langgraph/checkpoint/base/__init__.py:426 aget
  → langgraph_api/_checkpointer/_adapter.py:251 aget_tuple
  → langgraph/checkpoint/postgres/aio.py:402 _cursor
  → psycopg/connection_async.py:261 cursor → _connection_base.py:532 _check_connection_ok
psycopg.OperationalError: the connection is closed
```

**排除外部因素**（都实测过）：

| 怀疑 | 实测 | 结论 |
|---|---|---|
| postgres 挂了/重启过 | `RestartCount=0`、`StartedAt=2026-09-10`、`checkpoints` 1209 行 | 排除 |
| 连接被服务端掐断 | `idle_session_timeout=0`；PG 侧能看到每个 run 的会话、`state='idle'`、最后一条是 `INSERT INTO checkpoint_writes` | 排除 |
| 网络/DNS | 容器内 fresh `psycopg.AsyncConnection.connect(CHECKPOINT_DB_URI)` 成功 | 排除 |
| 我的压测打的 | 压测脚本只发 HTTP；release(06:07:40Z) → 我第一次问数(06:36:05Z) 之间**零个 PG 会话**；且**第一条问数 5s 内就失败** | 排除（⇒ 任何真实用户在发版后问第一句话都会中同一枪） |
| 优雅停机/关停路径 | 日志里**没有任何 lifespan 关停标记**（`Shutdown`/`teardown` 全无） | 排除：**不是** `exit_checkpointer()` 被调用 |

**langgraph 侧的三个事实（只读核实）**：
- `_adapter.py:65` `CHECKPOINTER_STACK = threading.local()`；`:274` `if not hasattr(CHECKPOINTER_STACK, "inner")` —— 每线程只建一次；
- `_adapter.py:363-371` `exit_checkpointer()` → `stack.aclose()`，**全仓唯一调用点**是 `langgraph_runtime_inmem/lifespan.py:163` 的 `finally:`；
- 启动日志能数出**多个线程各建了一个 saver**（`ThreadPoolExecutor-2_0` / `-4_1` / `-4_0` 各打一行 `Using custom checkpointer`）⇒ 每线程一个连接、共用一个槽位。

**本地最小复现（证明 GC 这一环）**：

```python
@asynccontextmanager
async def make(res):
    try: yield res
    finally: await res.close()      # ← from_conn_string 的退出路径

async def main():
    slot = {}                        # ← 老代码的 self._cm
    slot["cm"] = make(r1); await slot["cm"].__aenter__()   # 线程 A
    slot["cm"] = make(r2); await slot["cm"].__aenter__()   # 线程 B 覆盖槽位
    gc.collect(); await asyncio.sleep(0)
    print(r1.closed)                 # True ← A 的连接被 GC 关掉，全程没人调用 exit_checkpointer
```
实测输出：`A 进入后 r1.closed = False` / `B 覆盖 + GC 之后 r1.closed = True`。

## 三、修法（两层，缺一不可）

`src/agent/checkpoint/checkpointer_factory.py`：

1. **断因**：`self._cm` → `self._local.cm`（`threading.local()`），`__aexit__` 只关自己线程那条；SQLite 模式的 `_DynamicCheckpointer` 同款改法。
2. **兜底**：`_SelfHealingPostgresSaver(AsyncPostgresSaver)` 重写 `_cursor`，用连接前查 `conn.closed`，已死就 `AsyncConnection.connect(uri, autocommit=True, prepare_threshold=0, row_factory=dict_row)` + `setup()` 重连。
   - ②**不依赖我们对「谁关了连接」的判断正确**：postgres 容器重建、连接被 GC、服务端掐断等任何来源的关闭都能自愈，把"该线程永久报废"降级成"这一次操作多一次重连"。
   - 健康检查放在**拿 `self.lock` 之前**（避免与 `_cursor` 内部的锁互锁）；`_ensure_conn` 里对连接池（`hasattr(conn,"getconn")`）跳过自愈，绝不把池换成单连接。
   - 自愈时打 WARNING + 累计计数（`_note_heal`）——**用来观测"到底谁在关我们的连接"**。
   - 用的是 `AsyncPostgresSaver._cursor` / `.setup` 的**未绑定引用**，不用 zero-arg `super()`（延迟建类，避免非 PG 环境 import psycopg）。

## 四、验证

| 项 | 修前 | 修后 |
|---|---|---|
| 单条问数 | `main_run_status=error`，6s | **成功，6.5s** |
| 2 并发 × 2 轮 | 全 `error` | **4/4 成功**，p50 6.2s / max 8.1s |
| 重启后 `connection is closed` | 1150 次 / 数分钟 | **0 次** |
| 自愈日志 | — | **0 次** ⇒ 病因真的断了，兜底没被用到 |
| 能力检测（`CheckpointerCapabilities`） | `adelete_thread` 有、`adelete_for_runs`/`acopy_thread`/`aprune` 无 | **完全一致** ⇒ `DELETE /threads` 等不受影响 |

差分数据顺带拿到第一个实测量：**2 并发问数 → 峰值在跑 4.0 个 run（每问数 2 个）**；单并发某轮峰值 3.0（含自动续跑）⇒ 一次问数吃 **2~3** 个 run 槽。

## 五、残余与后续（如实记）

1. **未固化**：本次是 `docker cp` 式热修（`/app/src` 不是 bind mount，只影响这一个容器）+ 本地工作区改动，**未提交**。下次发版必须带上这份 `checkpointer_factory.py`（整包 src 会带，别丢）。
2. **同类模式排查未做**：仓库里凡是「模块级单例 + 每线程/每会话创建」的地方都值得扫一遍（MCP 工具会话、langfuse client、wren 引擎缓存…），看还有没有"共享可变槽位"。
3. **老运维注记仍成立**：postgres 容器重建后重启 langgraph-api 依然是对的（自愈现在也能兜住，但重启仍是首选）。
4. `_adapter.py:274` 的"只建不改"是上游设计（我们无法改）；**本文件的两层修法必须在**，否则同类关闭事件一律永久报废。
5. 排查方法论（值得复用）：`Exit=0`+`OOM=false` 说明不是崩溃；**先分「可达性」还是「数据」**；报错文本（`the connection is closed`）比"服务不可用"这类表象可靠得多；**"重启无效"是"缓存了死资源"的强信号**。
