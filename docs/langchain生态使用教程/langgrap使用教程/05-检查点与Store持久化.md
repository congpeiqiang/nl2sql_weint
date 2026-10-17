# LangGraph 检查点与 Store 持久化教程

> 本教程基于本机 `.venv` 实际安装的源码逐文件精读写成：
> **langgraph 1.2.9** / langchain-core 1.4.9 / langgraph-checkpoint 4.1.1 / langgraph-checkpoint-sqlite 3.1.1 / langgraph-checkpoint-postgres 3.1.2 / langgraph_api 0.11.1。
>
> 阅读的源码文件（均位于 `.venv/Lib/site-packages/langgraph/`）：
> - 检查点基础：`checkpoint/base/__init__.py`、`checkpoint/base/id.py`
> - 内存实现：`checkpoint/memory/__init__.py`
> - SQLite 实现：`checkpoint/sqlite/__init__.py`、`checkpoint/sqlite/aio.py`、`checkpoint/sqlite/utils.py`、`checkpoint/sqlite/_delta.py`
> - Postgres 实现：`checkpoint/postgres/__init__.py`、`checkpoint/postgres/aio.py`、`checkpoint/postgres/base.py`、`checkpoint/postgres/shallow.py`
> - 序列化：`checkpoint/serde/__init__.py`（空文件）、`serde/base.py`、`serde/jsonplus.py`、`serde/_msgpack.py`、`serde/encrypted.py`、`serde/event_hooks.py`、`serde/types.py`
> - Store：`store/base/__init__.py`、`store/base/batch.py`、`store/base/embed.py`、`store/memory/__init__.py`、`store/sqlite/__init__.py`、`store/sqlite/base.py`、`store/sqlite/aio.py`、`store/postgres/__init__.py`、`store/postgres/base.py`、`store/postgres/aio.py`
> - 快照类型：`types.py`（StateSnapshot）、`pregel/main.py`（get_state / get_state_history）、`runtime.py`（Runtime.store）
> - 项目实战：`D:/code_work_space/llm/nl2sql/src/agent/checkpoint/checkpointer_factory.py`

---

## 目录

1. [Checkpoint 系统概述](#1-checkpoint-系统概述)
2. [BaseCheckpointSaver 基类](#2-basecheckpointsaver-基类)
3. [MemorySaver（InMemorySaver）](#3-memorysaverinmemorysaver)
4. [SqliteSaver / AsyncSqliteSaver](#4-sqlitesaver--asyncsqlitesaver)
5. [PostgresSaver / AsyncPostgresSaver / ShallowPostgresSaver](#5-postgressaver--asyncpostgressaver--shallowpostgressaver)
6. [序列化系统（serde）](#6-序列化系统serde)
7. [Checkpoint 数据结构详解](#7-checkpoint-数据结构详解)
8. [StateSnapshot 与时间旅行](#8-statesnapshot-与时间旅行)
9. [Store 键值存储系统（BaseStore）](#9-store-键值存储系统basestore)
10. [InMemoryStore](#10-inmemorystore)
11. [SqliteStore / PostgresStore](#11-sqlitestore--postgresstore)
12. [Store 与 Checkpoint 的区别](#12-store-与-checkpoint-的区别)
13. [项目实战：本项目的 checkpoint 实现](#13-项目实战本项目的-checkpoint-实现)
14. [完整代码示例（4 个场景）](#14-完整代码示例4-个场景)
15. [总结与最佳实践](#15-总结与最佳实践)

---

## 1. Checkpoint 系统概述

### 1.1 为什么需要检查点

LangGraph 的执行引擎（Pregel 循环）把一次 `graph.invoke()` 拆成若干个 **super-step（超步）**：每一步里，被触发的节点并行执行，把结果写回 channel，然后引擎决定下一步触发谁。**检查点（Checkpoint）就是每个超步结束时对整个图状态做的一次持久化快照**。有了它才有下面这些能力：

| 能力 | 依赖的机制 |
|---|---|
| **多轮会话记忆** | 同一 `thread_id` 下每次 invoke 从上一个 checkpoint 恢复 state |
| **崩溃恢复 / durable execution** | 进程重启后从最近 checkpoint + pending writes 续跑 |
| **Human-in-the-loop（interrupt/resume）** | `interrupt()` 时状态已落盘，恢复时读回并注入 `Command(resume=...)` |
| **时间旅行（time travel）** | checkpoint 按 `checkpoint_id` 全量保留，可回到任意历史点重放/分叉 |
| **手动改状态** | `update_state()` 以某个 checkpoint 为父本写出 `source="update"` 的新 checkpoint |

### 1.2 设计哲学

读完全部实现后，可以提炼出四条设计主线：

1. **存储与执行彻底解耦**。执行引擎只依赖 `BaseCheckpointSaver` 的 5 个核心动作（`get_tuple / list / put / put_writes / get_next_version`），Memory / SQLite / Postgres 三个后端各自实现，接口完全一致。换后端 = 换 `compile(checkpointer=...)` 的一个参数。
2. **channel 值按「版本号」去重存储**。checkpoint 本体只存 `channel_versions`（轻量映射），channel 的实际值以 `(thread_id, ns, channel, version)` 为键单独存 blob。没变的 channel 不重写，这是三个后端共同的存储模型（见 §3/§4/§5）。
3. **写分两段：pending writes 先落，checkpoint 后落**。节点执行完但超步还没结束时，结果先以 `put_writes` 存成「待应用写入」；超步收尾才 `put` 新 checkpoint。崩溃后恢复时 pending writes 会被重放，保证节点级不丢结果。
4. **序列化协议可插拔**。所有后端都通过 `serde.dumps_typed() -> (type_tag, bytes)` 存取，默认 `JsonPlusSerializer`（msgpack 为主编码），可替换成加密序列化器（§6）。

### 1.3 整体架构

```
graph.invoke(input, config={"configurable": {"thread_id": "t1"}})
        │
        ▼
Pregel 循环 ──每个超步──► checkpointer.put(config, checkpoint, metadata, new_versions)
        │                        │
   节点完成即写                  ▼
        └──► checkpointer.put_writes(...)   InMemorySaver / SqliteSaver / PostgresSaver
                                                 │
                                                 ▼
                                          serde.dumps_typed()  ← JsonPlusSerializer / EncryptedSerializer
```

---

## 2. BaseCheckpointSaver 基类

源码：`langgraph/checkpoint/base/__init__.py`（约 860 行）。

### 2.1 接口定义

> ⚠️ 先澄清一点（很多教程写错）：**`BaseCheckpointSaver` 没有 `search` 方法**。`search`（按内容/向量检索）是 `BaseStore` 的接口（§9）；checkpoint 侧对应的查询接口是 `list()`（按 metadata 过滤 + 分页列出历史 checkpoint），删除接口是 `delete_thread()`。

```python
class BaseCheckpointSaver(Generic[V]):
    serde: SerializerProtocol = JsonPlusSerializer()

    def __init__(self, *, serde: SerializerProtocol | None = None) -> None:
        self.serde = maybe_add_typed_methods(serde or self.serde)
```

核心方法（每个都有 `a*` 异步镜像，基类默认用 `asyncio.get_running_loop().run_in_executor(None, ...)` 把同步实现丢线程池）：

| 方法 | 语义 |
|---|---|
| `get_tuple(config) -> CheckpointTuple \| None` | 取一个 checkpoint。config 带 `checkpoint_id` 就精确取，不带就取该 thread 最新的 |
| `get(config) -> Checkpoint \| None` | `get_tuple` 的简化版，只返回 checkpoint 本体 |
| `list(config, *, filter=None, before=None, limit=None)` | 按 metadata 过滤、按 `checkpoint_id` 游标分页，倒序列出历史（时间旅行的底层） |
| `put(config, checkpoint, metadata, new_versions) -> RunnableConfig` | 保存新 checkpoint，返回指向它的 config（成为下一轮的 parent） |
| `put_writes(config, writes, task_id, task_path="")` | 保存某任务的待应用写入（含错误/中断等特殊写入） |
| `delete_thread(thread_id)` | 删掉一个线程的全部 checkpoint 与 writes |
| `get_next_version(current, channel) -> V` | 生成下一个 channel 版本号（泛型 V，默认 int+1） |

`get_next_version` 默认实现（源码 692 行起）：

```python
def get_next_version(self, current: V | None, channel: None) -> V:
    if isinstance(current, str):
        raise NotImplementedError
    elif current is None:
        return 1
    else:
        return cast(V, current + 1)
```

解析：基类只认 int 版本；**所有落盘后端都覆盖了这个方法**，返回 `f"{next_v:032}.{next_h:016}"` 形式的字符串——前段是逻辑版本、后段是该版本内的写入序号，且必须「单调递增」，因为恢复时靠字符串比较判断 channel 新旧。`channel` 参数已废弃，仅为向后兼容保留。

### 2.2 CheckpointTuple / PendingWrite / WRITES_IDX_MAP

```python
class CheckpointTuple(NamedTuple):
    config: RunnableConfig
    checkpoint: Checkpoint
    metadata: CheckpointMetadata
    parent_config: RunnableConfig | None = None
    pending_writes: list[PendingWrite] | None = None
```

`PendingWrite = tuple[str, str, Any]`，即 `(task_id, channel, value)`。特殊 channel 名走负数 idx：

```python
WRITES_IDX_MAP = {ERROR: -1, SCHEDULED: -2, INTERRUPT: -3, RESUME: -4}
```

解析：`__error__ / __scheduled__ / __interrupt__ / __resume__` 这四类写入用**负 idx 且总是覆盖写（upsert）**，因为它们表达的是「最新一次尝试」的可变状态；普通写入 idx ≥ 0，**幂等去重**（已存在同 `(task_id, idx)` 就跳过，见 §3 的 `put_writes` 摘录），保证崩溃重试时不会重复累计 reducer 型 channel（如 `operator.add` 的消息列表）。这一条是理解「为什么 LangGraph 节点重试不会导致消息翻倍」的关键。

### 2.3 CheckpointMetadata

`total=False` 的 TypedDict（源码 41 行起），关键字段：

- `source: Literal["input", "loop", "update", "fork"]` —— 谁产生了这个 checkpoint：invoke 入口输入 / pregel 循环内 / `update_state()` 手动更新 / 时间旅行分叉；
- `step: int` —— `-1` 是第一个 input checkpoint，`0` 是第一个 loop checkpoint，之后递增；
- `parents: dict[str, str]` —— checkpoint_ns → checkpoint_id 的父映射（子图追踪）；
- `run_id: str`；
- `counters_since_delta_snapshot` —— DeltaChannel（beta）用的每 channel 计数器，超过 `snapshot_frequency` 或全局 5000 超步上限就强制打快照，防止祖先链回溯无界增长。

`get_checkpoint_metadata(config, metadata)` 会剔除 `EXCLUDED_METADATA_KEYS`（thread_id、checkpoint_id 等 9 个内部键）并清洗 `\u0000`（Postgres JSONB 不接受 NUL 字符）。

---

## 3. MemorySaver（InMemorySaver）

源码：`langgraph/checkpoint/memory/__init__.py`。`MemorySaver = InMemorySaver` 只是别名。

### 3.1 存储结构：三个 dict

```python
self.storage: dict[thread_id][checkpoint_ns][checkpoint_id] -> (checkpoint_bytes, metadata_bytes, parent_id)
self.writes:  dict[(thread_id, ns, checkpoint_id)][(task_id, idx)] -> (task_id, channel, serialized, task_path)
self.blobs:   dict[(thread_id, ns, channel, version)] -> (type_tag, bytes)
```

「最新 checkpoint」= `max(storage[thread_id][ns].keys())` —— 直接对字符串取 max，这依赖 `checkpoint_id` 是**时间可排序的 uuid6**（§7.3）。

### 3.2 put：channel 值拆出去按版本存

```python
def put(self, config, checkpoint, metadata, new_versions) -> RunnableConfig:
    c = checkpoint.copy()
    thread_id = config["configurable"]["thread_id"]
    checkpoint_ns = config["configurable"]["checkpoint_ns"]
    values: dict[str, Any] = c.pop("channel_values")
    for k, v in new_versions.items():
        self.blobs[(thread_id, checkpoint_ns, k, v)] = (
            self.serde.dumps_typed(values[k]) if k in values else ("empty", b"")
        )
    self.storage[thread_id][checkpoint_ns].update(
        {
            checkpoint["id"]: (
                self.serde.dumps_typed(c),
                self.serde.dumps_typed(get_checkpoint_metadata(config, metadata)),
                config["configurable"].get("checkpoint_id"),  # parent
            )
        }
    )
    return {"configurable": {"thread_id": ..., "checkpoint_ns": ..., "checkpoint_id": checkpoint["id"]}}
```

解析（这段是全部后端的「原型」）：

1. `c.pop("channel_values")` —— checkpoint 本体**不含** channel 值，值被拆到 `blobs`，键是 `(thread, ns, channel, version)`。同一 channel 值没变（版本没进 `new_versions`）就不重新序列化、不重新存，历史 checkpoint 共享同一份 blob —— 内存后端也做了和 Postgres 一样的去重。
2. `("empty", b"")` 哨兵：channel 在该版本被清空时存一个空标记，`_load_blobs` 读到 `vv[0] == "empty"` 就跳过。
3. **即使是内存实现，存的也是序列化后的 bytes**（走同一个 serde），保证行为与磁盘后端逐字节一致——你在内存 saver 上测通的自定义类型序列化，换 Postgres 不会变。

### 3.3 put_writes：幂等去重

```python
for idx, (c, v) in enumerate(writes):
    inner_key = (task_id, WRITES_IDX_MAP.get(c, idx))
    if inner_key[1] >= 0 and outer_writes_ and inner_key in outer_writes_:
        continue
```

解析：普通写入（idx ≥ 0）已存在即跳过（幂等），特殊写入（idx < 0）总是覆盖——正是 §2.2 说的语义在内存后端的落地。

### 3.4 线程安全与适用场景

- **没有显式锁**。单事件循环内 dict 读写靠 GIL 原子性基本安全；但**多线程并发写同一 saver 不保证一致**（`storage[thread][ns].update` 与 `blobs` 写不是原子的）。异步方法（`aget_tuple` 等）就是同步方法的直接包装，没有额外并发控制。
- 进程退出即全丢。适用：单元测试、原型、一次性脚本、langgraph dev 内存模式。
- 提供 `PersistentDict`（pickle 落盘 + tmp 文件原子替换）作为可选 storage 工厂，可做「伪持久化」，但生产不推荐——直接上 SqliteSaver/PostgresSaver。

---

## 4. SqliteSaver / AsyncSqliteSaver

源码：`langgraph/checkpoint/sqlite/__init__.py`、`aio.py`、`utils.py`、`_delta.py`。

### 4.1 表结构与 WAL

`setup()` 的 DDL（同步版 129 行起，异步版 aio.py 305 行起，两者完全一致）：

```sql
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint BLOB,
    metadata BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE IF NOT EXISTS writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    value BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
```

解析：

- **WAL 模式**（Write-Ahead Logging）：写不阻塞读、读写可并发，是 SQLite 服务端场景的必开项；`PRAGMA` 在 `executescript` 里随建表一起执行，`setup()` 由首次 `cursor()` 懒触发（`is_setup` 标志防重入）。
- 两张表主键都以 `thread_id` 打头，`delete_thread` 因此可以走主键前缀高效删除。
- 注意 SQLite 后端**没有** Postgres 的 `checkpoint_blobs` 表——channel 值不单独拆表，整个 checkpoint（含 `channel_values`）序列化后塞进 `checkpoint BLOB` 列。这是与 Postgres 后端最大的存储模型差异（SQLite 版牺牲跨 checkpoint 的 blob 去重，换取单文件实现的简单性）。
- metadata 列虽声明 BLOB，实际存 **JSON 文本**（读回用 `json.loads`），因为 `list()` 过滤要用 SQLite 的 `json_extract`。

### 4.2 并发控制

同步 `SqliteSaver`：连接用 `check_same_thread=False` 打开，外面包一把 `threading.Lock`，所有读写经 `cursor()` 上下文管理器 **先拿锁再执行、退出即 commit** —— 即「全库串行化」，简单粗暴但对 SQLite 是正确选择（SQLite 本身单写者）。

### 4.3 metadata 过滤与 SQL 注入防护（utils.py）

`list(filter=...)` 把 `{"source": "loop"}` 这类过滤字典翻译成 `json_extract(CAST(metadata AS TEXT), '$.key') = ?` 谓词。用户可控的 key 先过白名单正则：

```python
_FILTER_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+$")
```

不匹配的 key 直接 `raise ValueError`——因为 `json_extract` 的路径没法参数绑定，只能拼串，正则白名单是防注入闸。

### 4.4 AsyncSqliteSaver

- 基于 **aiosqlite**（SQLite 连接的后台线程封装），锁换成 `asyncio.Lock`；
- 同步方法（`get_tuple` 等）没被删掉，而是桥接到异步实现：`asyncio.run_coroutine_threadsafe(coro, self.loop).result()`，且**只允许从非 loop 线程调用**——从主事件循环线程调同步方法会先检测 `asyncio.get_running_loop() is self.loop` 然后抛 `InvalidStateError`，明确报错而不是死锁；
- `from_conn_string()` 是 `@asynccontextmanager`，退出时关连接。**必须让它正常退出（或显式 close），否则 aiosqlite 的后台线程不结束，进程挂住不退出**——这是最常见的踩坑点；
- `_delta.py` 为 DeltaChannel（beta）提供 `aget_delta_channel_history`：stage1 用流式 SQL 沿祖先链（`checkpoint_id <= ?` DESC）定位每个 channel 最近的非空 blob，stage2 按 channel `UNION ALL` 收集途中的 pending writes，全程**一次只反序列化一个 checkpoint**，控制内存峰值。

### 4.5 适用场景

单机、单进程、中小流量（含 langgraph dev / 本项目开发模式，见 §13）。多进程共享同一 SQLite 文件时 WAL 也只允许一个写者，写并发高就换 Postgres。

---

## 5. PostgresSaver / AsyncPostgresSaver / ShallowPostgresSaver

源码：`langgraph/checkpoint/postgres/__init__.py`、`base.py`、`aio.py`、`shallow.py`。

### 5.1 迁移体系（base.py）

`MIGRATIONS` 是一个 **10 项的 SQL 列表，版本号 = 列表下标**；`setup()` 读 `checkpoint_migrations` 表里已应用的最大 v，逐条执行缺失的迁移并记账。**`setup()` 必须由用户显式调用**（与 SQLite 的懒 setup 不同），官方 docstring 原话是 "It MUST be called directly by the user"。前四条迁移建出核心表：

```sql
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    type TEXT NOT NULL,
    blob BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
CREATE TABLE IF NOT EXISTS checkpoint_writes ( ... PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx) );
```

后续迁移包括：`blob` 列放开 NOT NULL（存空 channel）、一条 no-op（修正历史空迁移导致的版本错位）、三张表的 `thread_id` 索引（`CREATE INDEX CONCURRENTLY`，不锁生产表）、`checkpoint_writes.task_path` 列。

### 5.2 put：原始值内联、复杂值拆 blob

`PostgresSaver.put()`（`__init__.py` 263 行起）的核心分支：

```python
# inline primitive values in checkpoint table
# others are stored in blobs table
blob_values = {}
for k, v in checkpoint["channel_values"].items():
    if isinstance(v, _DeltaSnapshot):
        blob_values[k] = copy["channel_values"].pop(k)
        copy["channel_values"][k] = True
    elif v is None or isinstance(v, (str, int, float, bool)):
        pass
    else:
        blob_values[k] = copy["channel_values"].pop(k)
```

解析：

- `None/str/int/float/bool` 这五种**原始值直接内联**在 checkpoint JSONB 里（读的时候不用 join blob 表）；其余一切（消息列表、dict、自定义对象）都 pop 出去，按 `(thread_id, ns, channel, version)` 写 `checkpoint_blobs`。
- blob 的 upsert 语句是 `ON CONFLICT (thread_id, checkpoint_ns, channel, version) DO NOTHING` —— 版本不可变，同版本 blob 永不重写。**没变的 channel 连 INSERT 都不发生**（`put` 只对 `new_versions ∩ blob_values` 执行 `executemany`），这是长会话下存储量与写放大的关键优化。
- `_DeltaSnapshot` 内联成 `True` 标记，真实快照进 blob 表——DeltaChannel 的「增量 + 周期快照」模型。
- 读取侧 `SELECT_SQL`（base.py 93 行起）用 `jsonb_each_text(checkpoint->'channel_versions') INNER JOIN checkpoint_blobs` 的 `array_agg` 子查询，一条 SQL 同时取回 checkpoint、全部 channel blob、全部 pending writes；`_load_checkpoint_tuple` 再把内联原始值与 blob 合并回 `channel_values`。
- `pending_sends` 在 v2 格式里不再存 checkpoint 本体，而是作为 `TASKS` channel 的写入存 `checkpoint_writes`（`SELECT_PENDING_SENDS_SQL` 按 `task_path, task_id, idx` 排序取回）。

### 5.3 metadata 过滤

Postgres 的 `list(filter=...)` 不需要 json_extract，直接 `metadata @> %s`（JSONB 包含运算符）+ GIN 语义，性能和表达力都优于 SQLite 版。

### 5.4 连接管理与 _cursor

`from_conn_string()` 建连接固定三件套：`autocommit=True, prepare_threshold=0, row_factory=dict_row`（prepare_threshold=0 是关 prepared statement 缓存，兼容 pgbouncer 等连接代理）；所有游标 `binary=True`。`_cursor(pipeline=)` 的取锁策略：

- 传了 `pipeline=True` 且服务器支持 → `conn.pipeline()`（批量往返，单连接可多协程共用，但同一时刻只能有一个游标）；
- 不支持 pipeline → 退化成 `conn.transaction()`；
- 单连接模式下所有 `_cursor` 共享一把 `asyncio.Lock`/`threading.Lock` 串行化；**连接池模式下每次 `_cursor` 从池里 checkout 独立连接，不加共享锁**（池保证不并发发同一条连接）。

### 5.5 AsyncPostgresSaver

结构与同步版镜像，差别在：`aput` 里把纯 CPU/序列化的 `_dump_blobs`、`_dump_writes` 用 `asyncio.to_thread` 丢线程池，避免大 state 序列化阻塞事件循环；游标操作本身是 psycopg async 原生。

### 5.6 ShallowPostgresSaver（已废弃）

`shallow.py` 的表以 `(thread_id, checkpoint_ns)` 为主键——**每个线程只存最新一个 checkpoint**，不支持时间旅行/历史。类加载即发 `DeprecationWarning`：

> deprecated as of version 2.0.20 and will be removed in 3.0.0. Use PostgresSaver instead, and invoke the graph with `graph.invoke(..., durability='exit')`.

解析：shallow 的卖点是「少写盘」，官方结论是这个诉求应该用**持久化模式参数**解决而不是砍存储能力——`Durability = Literal["sync", "async", "exit"]`（`types.py`）：`sync` 每步同步落盘（默认，最安全）、`async` 后台落盘（不阻塞下一步）、`exit` 只在图退出时落一次盘（等价于 shallow 的效果但保留完整实现）。新项目不要再碰 ShallowPostgresSaver。

---

## 6. 序列化系统（serde）

源码：`langgraph/checkpoint/serde/`。注意 `serde/__init__.py` 是**空文件**，所有符号从子模块导入。

### 6.1 SerializerProtocol（base.py）

协议只有两个方法：

```python
dumps_typed(obj) -> tuple[str, bytes]   # (type_tag, payload)
loads_typed(data: tuple[str, bytes]) -> Any
```

「typed」指载荷自带类型标签，存储列里的 `type TEXT` 列存的就是这个 tag。`maybe_add_typed_methods` 兼容只有旧式 `dumps/loads` 的自定义序列化器。

### 6.2 JsonPlusSerializer（jsonplus.py）——默认实现

```python
def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
    if obj is None:
        return "null", EMPTY_BYTES
    elif isinstance(obj, bytes):
        return "bytes", obj
    elif isinstance(obj, bytearray):
        return "bytearray", obj
    else:
        try:
            return "msgpack", _msgpack_enc(obj)
        except ormsgpack.MsgpackEncodeError as exc:
            if self.pickle_fallback:
                return "pickle", pickle.dumps(obj)
            raise exc
```

解析：

- 类型标签共六种：`"null" / "bytes" / "bytearray" / "msgpack"`（主编码，走 Rust 实现的 **ormsgpack**）/ `"json"`（历史数据兼容）/ `"pickle"`（显式开启 `pickle_fallback=True` 才有）。
- **1.2.9 里没有独立的 `MsgPackSerializer` 类**。msgpack 不是「另一个序列化器」，而是 JsonPlusSerializer 的主编码；`_msgpack.py` 是它的**安全策略模块**（见 6.4）。「JsonPlus」的含义 = JSON 超集：msgpack 打底 + ext 扩展类型复活任意 Python 对象。
- 遇到无法编码的对象，默认**直接抛错**（fail-fast），只有显式开了 pickle_fallback 才退化到 pickle（反序列化任意 pickle 是 RCE 面，生产禁用）。

`_msgpack_default` 用 msgpack 的 **ext 扩展码**处理标准 msgpack 不认识的类型，ext code 表（jsonplus.py 295 行起）：

```python
EXT_CONSTRUCTOR_SINGLE_ARG = 0   # 单参数构造器复活（namedtuple 等）
EXT_CONSTRUCTOR_POS_ARGS = 1     # 位置参数构造
EXT_CONSTRUCTOR_KW_ARGS = 2      # 关键字参数构造
EXT_METHOD_SINGLE_ARG = 3        # 类方法复活（如 datetime.fromtimestamp）
EXT_PYDANTIC_V1 = 4              # pydantic v1 模型
EXT_PYDANTIC_V2 = 5              # pydantic v2 模型
EXT_NUMPY_ARRAY = 6              # numpy 数组（zero-copy）
EXT_DELTA_SNAPSHOT = 7           # DeltaChannel 快照标记
```

被逐一特殊照顾的类型包括：pydantic v1/v2 模型（存 module+class+dump 数据，复活时重新 import 类）、`SecretStr`、namedtuple、`pathlib.Path`、`re.Pattern`、`UUID`、`Decimal`、`set/frozenset/deque`、`ipaddress` 全家、`datetime/date/time/timedelta`（带时区 `ZoneInfo`）、`Enum`、LangGraph 自己的 `Send`、dataclass、Store 的 `Item`、numpy 数组（走 buffer 零拷贝）、以及 **BaseException → 存 repr 字符串**（异常对象图太深不可靠，恢复成文本）。

反序列化时若目标类 import 不到（比如你改了类名/模块路径），blocked types 会「复活成原始 payload」而不是崩——历史 checkpoint 可读性优先。

### 6.3 EncryptedSerializer（encrypted.py）

装饰器模式：包一个 cipher + 内层 serde，`dumps_typed` 先内层序列化再整体加密，类型标签变成 `f"{内层tag}+{cipher名}"`（如 `"msgpack+aes"`）；`loads_typed` 看到没有 `+` 的 tag 就**明文直通**（旧数据兼容/渐进加密）。

内置工厂 `from_pycryptodome_aes()`：密钥来自环境变量 **`LANGGRAPH_AES_KEY`**（16/24/32 字节），算法 AES **MODE_EAX**（带认证的加密），载荷布局 `nonce(16) + tag(16) + ciphertext`。适用于「checkpoint 落库但 DBA 不应可读」的合规场景，代价是每次读写多一次全量加解密。

### 6.4 _msgpack.py：安全策略

`SAFE_MSGPACK_TYPES` 是「不需要 import 任意类就能复活」的安全类型白名单。设 **`LANGGRAPH_STRICT_MSGPACK=true`** 后，ext hook 只接受白名单内类型，其余拒绝复活；默认（不设）是宽松模式 + 打一次 warning。多租户/不可信 checkpoint 来源建议开严格模式。

### 6.5 event_hooks.py / types.py

- `event_hooks.py`：`register_serde_event_listener()` 注册回调，每次序列化/反序列化发事件（类型、字节数、耗时），用于观测「哪个 channel 的 state 膨胀了」——纯观测设施，不改行为。
- `types.py`：`_DeltaSnapshot`（DeltaChannel 快照的值包装，msgpack ext 7）、`ERROR/SCHEDULED/INTERRUPT/RESUME/TASKS` 等特殊 channel 名常量的 serde 侧定义。

---

## 7. Checkpoint 数据结构详解

源码：`checkpoint/base/__init__.py` 92 行起。

```python
class Checkpoint(TypedDict):
    v: int                              # 格式版本，当前 LATEST_VERSION = 2
    id: str                             # uuid6：唯一且单调递增 → 字符串排序 = 时间排序
    ts: str                             # ISO 8601 时间戳
    channel_values: dict[str, Any]      # channel 名 → 反序列化后的值（存储层会拆走）
    channel_versions: ChannelVersions   # channel 名 → 版本（str|int|float，单调递增）
    versions_seen: dict[str, ChannelVersions]  # 节点 → {channel: 已见版本}
    updated_channels: list[str] | None  # 本 checkpoint 更新过的 channel（增量提示）
```

逐字段解析：

- **`channel_values`**：图的完整状态（state 各键映射到 channel 后的值）。逻辑上属于 checkpoint，物理上三个后端都把它按版本拆出去存（§3.2/§5.2），所以 `put` 的调用方拿到的是「已经 pop 掉值」的骨架。
- **`channel_versions`**：每个 channel 当前值的版本号。写入 channel 时引擎调 `get_next_version` 生成新版本；它同时是 **blob 存储的键**和**调度判断的输入**。
- **`versions_seen`**：`{节点名: {channel 名: 该节点上次执行时见到的版本}}`。**这是整个调度器持久化的灵魂**：超步开始时，引擎对每个节点比较 `channel_versions[ch] > versions_seen[node][ch]`，有任一触发 channel 的版本比节点「见过」的新，该节点就要执行。恢复 = 读回这两个映射，重算触发集，无需额外记「执行到哪了」。
- **`updated_channels`**：本步写过哪些 channel，供 `list()`/快照做增量提示（可为 None）。
- **`pending_sends`**：v1 格式遗留在 `copy_checkpoint` 里（`checkpoint.get("pending_sends", []).copy()`），v2 已迁到 `TASKS` channel 的 writes（§5.2）；新代码不要依赖该字段。

### 7.3 id 为什么用 uuid6（base/id.py）

`uuid6()` 是「时间重排的 UUID」：60 bit 的 **100 纳秒级时间戳（自 1582-10-15 起，常量 `0x01B21DD213814000`）** 放在高位，14 bit clock_seq + 48 bit 随机 node 放低位；模块级 `_last_v6_timestamp` 保证同机单调（时钟回拨也不回退）。因此：

1. `ORDER BY checkpoint_id` = 按时间排序，`max()` = 最新（MemorySaver 直接靠这个取最新）；
2. `create_checkpoint(..., id=str(uuid6(clock_seq=step)))` 把**超步号编进 clock_seq**，同一毫秒内的多个 checkpoint 也能按 step 排序；
3. `empty_checkpoint()` 用 `clock_seq=-2`，保证比任何真实 checkpoint 都小。

---

## 8. StateSnapshot 与时间旅行

源码：`langgraph/types.py` 643 行、`langgraph/pregel/main.py` 1380 行起。

### 8.1 StateSnapshot

```python
class StateSnapshot(NamedTuple):
    values: dict[str, Any] | Any        # 当前 channel 值（即图的 state）
    next: tuple[str, ...]               # 下一步将执行的节点名（每任务一个）
    config: RunnableConfig              # 取该快照用的 config（含 checkpoint_id）
    metadata: CheckpointMetadata | None
    created_at: str | None
    parent_config: RunnableConfig | None  # 父快照 config → 串成历史链
    tasks: tuple[PregelTask, ...]       # 本步任务详情；已尝试过的可能带 error/interrupts
    interrupts: tuple[Interrupt, ...]   # 本步待处理的中断
```

`StateSnapshot` 是 **checkpoint 的用户视角包装**：`get_state(config)` 内部 `checkpointer.get_tuple()` → `_prepare_state_snapshot()` 把裸 checkpoint 翻译成上面这个带 `next`/`tasks` 的结构。`next` 非空 = 图停在半途（interrupt 或崩溃），空 = 该 thread 已跑完。

`pregel/main.py` 里两个值得注意的实现细节：

- `get_state` 在没设 checkpointer 时抛 `ValueError("No checkpointer set")`；config 里 `checkpoint_ns` 非空时会**委托给子图**处理（子图状态要用子图对象取）。
- `apply_pending_writes` 的条件是 `CONFIG_KEY_CHECKPOINT_ID not in config`：**只有取「最新」状态时才把 pending writes 应用上去**；你显式指定了历史 `checkpoint_id`，看到的就是当时的纯净快照，不混入后来未应用的写入。

### 8.2 get_state_history

```python
history = list(graph.get_state_history(config))  # 内部 eagerly consume
```

源码注释明说：`list(checkpointer.list(...))` **一次性吃完再返回**，避免生成器长期占着数据库游标（Postgres 服务端游标不关会锁资源）。返回按时间倒序的 `StateSnapshot` 迭代，`parent_config` 把链条串起来。

### 8.3 时间旅行三板斧

```python
# ① 回到历史某点重放（fork）：把 checkpoint_id 塞进 config 再 invoke
graph.invoke(None, {"configurable": {"thread_id": "t1", "checkpoint_id": target_id}})
#    → 产生 source="fork" 的新 checkpoint，原历史不被改写（分支而非覆盖）

# ② 手改状态：以指定 checkpoint 为父本写一条 source="update" 的新 checkpoint
graph.update_state(config, {"messages": [AIMessage(content="改过的回答")]}, as_node="agent")

# ③ 查历史：graph.get_state_history(config) 逐个 StateSnapshot 检查
```

`invoke(None, ...)` 传 None 表示「不带新输入，纯从该 checkpoint 续跑」——配合 `next` 非空的快照，就是「崩溃/中断点续跑」的标准姿势。完整可运行示例见 §14 场景三。

---

## 9. Store 键值存储系统（BaseStore）

源码：`store/base/__init__.py`（1314 行）、`store/base/batch.py`、`store/base/embed.py`。

Checkpoint 解决「一个线程内」的状态持久化；**Store 解决「跨线程、跨会话」的长期记忆**：按命名空间组织的 KV + 可选语义检索 + 可选 TTL。

### 9.1 Item / SearchItem

```python
class Item:
    namespace: tuple[str, ...]   # 层级命名空间，如 ("users", "u123", "memories")
    key: str                     # 命名空间内唯一键
    value: dict[str, Any]        # 必须是 JSON 兼容 dict
    created_at / updated_at: datetime

class SearchItem(Item):          # 多一个相似度分
    score: float | None
```

命名空间校验 `_validate_namespace`：**非空 tuple、每段非空字符串、段内不许有 `.`（点号是存储层的分隔符）、根段不许叫 `"langgraph"`（内部保留）**。

### 9.2 batch-first 架构：Ops

BaseStore 的抽象方法只有 `batch/abatch` 两个；`get/put/search/delete/list_namespaces` 全是「把单个操作包成 Op 列表 → batch → 取第 0 个结果」的语法糖：

```python
GetOp(namespace, key)
PutOp(namespace, key, value, index)      # value=None 即删除；index: False|None|list[str]
SearchOp(namespace_prefix, query, filter, limit, offset, score_threshold)
ListNamespacesOp(match_pattern, max_depth, limit, offset)
```

解析：batch-first 不是炫技——异步服务端一次工具调用往往并发发出十几个 store 读写，`AsyncBatchedBaseStore`（batch.py）把它们**合并在同一个事件循环 tick 内**打成一个 `abatch`，Postgres 端就能一条 SQL 取 N 个 key。batch.py 的三个要点：

1. 后台 `_run` 任务从 `asyncio.Queue` 拉 op，同 tick 的 op 攒成一批；
2. `_dedupe_ops`：读操作按相等性去重（10 个协程 get 同一个 key 只查一次），PutOp 去重取「最后写入者胜」；
3. `_check_loop`：**在 store 自己的事件循环线程上调同步方法直接抛 `InvalidStateError`**——同步方法会 `.result()` 等后台任务，而后台任务就在这个 loop 上，必死锁，所以提前报错。对 store 持弱引用避免泄漏。

`embed.py` 提供 `ensure_embeddings`（`"openai:text-embedding-3-small"` 字符串 → `init_embeddings` 实例）、`get_text_at_path`（`"$"`=整个 value 序列化文本；支持 `text`、`arr[0]`、`arr[*]`、`arr[-1]`、`{a,b}` 路径语法）——决定 value 的哪些字段被嵌入。

### 9.3 IndexConfig / TTLConfig / filter 操作符

```python
IndexConfig = {
    "dims": 1536,
    "embed": Embeddings | Callable | str,     # 嵌入模型
    "fields": ["$"],                          # 默认整个 value；可指定 ["text"] 等
}
TTLConfig = {
    "default_ttl": 60,            # 分钟；put 时可逐条覆盖
    "refresh_on_read": True,      # 读即续期
    "sweep_interval_minutes": 5,  # 后台清扫间隔
}
```

`search(filter=...)` 支持 Mongo 风格操作符：`{"age": {"$gt": 18}}`，`$eq/$ne/$gt/$gte/$lt/$lte`；`namespace` 匹配支持 `*` 单段通配（`("users", "*")`）。**不配 `index` 时一切语义检索静默无效**（`asearch(query=...)` 退化为普通扫描，`put(index=...)` 参数被忽略）——官方 docstring 明确提示了这一点。

---

## 10. InMemoryStore

源码：`store/memory/__init__.py`。

存储双 dict：`_data[namespace][key] -> Item`、`_vectors[namespace][key][field_path] -> embedding`。`batch()` 的流水线是理解它的最佳路径：`_prepare_ops`（校验 + 收集需要嵌入的文本）→ 嵌入查询 → `_batch_search` → `_extract_texts`（按 IndexConfig.fields 抽文本）→ `embed_documents` → `_apply_put_ops`。

要点：

- **相似度 = 余弦**；有 numpy 走向量化实现，没有则纯 Python 循环并 warn 一次。
- 一个 Item 多个字段被嵌入时，检索得分做 **max-pooling**（取字段间最大相似度），且**按 item 去重**——和 SQL 后端的 `DISTINCT ON`/`ROW_NUMBER` 语义对齐。
- 带 `query` 的搜索里，**没有向量的 item 会填充在向量命中之后**补足 limit（不至于因为没嵌入就彻底搜不到）。
- `_does_match` 实现命名空间前缀/后缀匹配，`*` 恰好匹配一段。
- `_compare_values` 刻意**模拟 Postgres JSONB 的比较语义**（数字统一按 float 比、字符串按字典序、跨类型有确定序）——又是「内存实现与磁盘实现行为逐位对齐」的设计，测试用 InMemoryStore 验过的 filter，上 Postgres 不变。

适用：单测/原型/进程内长期记忆（配合 langgraph dev）。同样**无锁**，跨线程并发写不保证。

---

## 11. SqliteStore / PostgresStore

### 11.1 SqliteStore（store/sqlite/base.py + aio.py）

- 两张表：`store`（`prefix` = namespace 点号拼接、`key`、`value` TEXT(orjson)、`created_at/updated_at`、`expires_at/ttl_minutes`）+ `store_vectors`（`embedding BLOB`，外键 CASCADE 随 item 删除）。
- 向量检索靠 **sqlite-vec 扩展**（`import sqlite_vec`，`setup()` 里加载），提供 `vec_distance_cosine/L2/L1`；打分 CTE + `ROW_NUMBER() ... max-pooling` 与内存版语义对齐。
- 命名空间匹配用 **GLOB 而不是 LIKE**（源码注释：GLOB 大小写敏感，与 Postgres 的 `=` 行为一致），且注册了自定义 SQL 函数 `_langgraph_namespace_match`（把含 `*` 的模式编译成「段感知」的正则，`*` 不跨 `.`）。
- `list_namespaces` 的 max_depth 截断用 `WITH RECURSIVE` 拆段实现。
- 连接 `isolation_level=None`（autocommit）+ 手动 `BEGIN/COMMIT`，外面一把锁；**`supports_ttl = True`**，带后台清扫线程（SQLite 后端是三个 Store 里唯一默认支持 TTL 的轻量选择）。
- `AsyncSqliteStore`（aio.py）= `AsyncBatchedBaseStore + BaseSqliteStore`，aiosqlite + `asyncio.Lock`，TTL 清扫换成 asyncio task。

### 11.2 PostgresStore / AsyncPostgresStore（store/postgres/base.py + aio.py）

- `value` 列是 **jsonb**（可直接 SQL 查询/建 GIN 索引），迁移表 `store_migrations` + 向量迁移表 `vector_migrations` 双轨。
- 向量能力靠 **pgvector**：`VECTOR_MIGRATIONS` 依次 `CREATE EXTENSION vector`、建 `vector`/`halfvec` 列（`vector_type` 只接受这两个值，非法即 `ValueError`）、建索引（`index_type` 只接受 `hnsw`（参数 m/ef_construction）或 `ivfflat`（nlist），不指定则 flat 全扫）。
- 批量 GET 是一条 SQL：`unnest(%s::text[])` 展开 key 数组一次取回；配了 TTL 且 `refresh_on_read` 时用 **CTE 先 `UPDATE expires_at ... RETURNING`** ——「读即续期」在同一条语句里原子完成。
- 向量搜索 CTE + `DISTINCT ON (prefix, key)`（每 item 只留最高分字段，即 max-pooling），并且 `expanded_limit = limit × 估计向量数 × 2 + 1` 预扩容，去重后仍够 limit 条。
- `PoolConfig(min_size, max_size, kwargs)` 切连接池；池模式 `_cursor` 不加共享锁（§5.4 同款逻辑）。
- TTL 清扫：`sweep_ttl()` 一条 `DELETE ... WHERE expires_at < NOW()`，`start_ttl_sweeper()` 起线程/task 周期执行（默认 5 分钟），`stop_ttl_sweeper()` 带超时优雅停。
- `AsyncPostgresStore`（aio.py 42 行起）= `AsyncBatchedBaseStore + BasePostgresStore`，`abatch` 按 Op 类型分组后在**一个** `_cursor(pipeline=True)` 里成批执行——这就是 §9.2 batch-first 设计在真实后端的收益兑现。

---

## 12. Store 与 Checkpoint 的区别

| 维度 | Checkpoint（BaseCheckpointSaver） | Store（BaseStore） |
|---|---|---|
| **存什么** | 图执行状态：channel 值、版本、调度信息、pending writes | 业务 KV：用户偏好、长期记忆、跨会话事实 |
| **作用域** | `thread_id`（+ 子图 `checkpoint_ns`），**线程内** | namespace 树，**天然跨线程** |
| **生命周期** | 一次 run 的每个超步都写；线程删除即清空 | 应用主动 put/delete；可配 TTL 自动过期 |
| **谁在写** | 引擎自动写（你不直接调 put） | 你的节点/工具代码显式读写 |
| **查询能力** | `list()` 按 metadata 过滤历史；时间旅行 | `get`/`search`（**语义向量检索**）/`list_namespaces` |
| **典型问题** | 「这个会话上一轮说了什么」「从中断处继续」 | 「这个用户以前告诉过我什么」「所有用户的偏好」 |
| **接线方式** | `compile(checkpointer=saver)` | `compile(store=store)`，节点内经 `runtime.store` 访问 |
| **组合语义** | 有 checkpointer 无 store：会话内记忆，跨会话失忆 | 有 store 无 checkpointer：跨会话记忆，但单会话不能续跑/时间旅行 |

选型口诀：**「会话怎么继续」归 Checkpoint，「用户/世界是什么样」归 Store**。生产系统通常两个都要：PostgresSaver + PostgresStore（同一套 PG，表互不干扰：`checkpoints*` vs `store*`）。

---

## 13. 项目实战：本项目的 checkpoint 实现

源码：`src/agent/checkpoint/checkpointer_factory.py`（227 行；`src/agent/checkpoint/__init__.py` 为空文件）。

### 13.1 它在架构里的位置

本项目后端以 **langgraph_api（LangGraph Server）** 形态运行，checkpointer 不是业务代码 `compile()` 时传的，而是通过 `graph.json / langgraph.json` 的 `{"checkpointer": {"backend": "custom", "path": "...:checkpointer"}}`（`LANGGRAPH_CHECKPOINTER`）交给 langgraph_api 加载。langgraph_api 内部（`_checkpointer/_adapter.py`）对每个工作线程 `async with` 一次这个对象拿到 saver，并**线程本地缓存、只建不改**（`CHECKPOINTER_STACK = threading.local()` + `if not hasattr(..., "inner")` 守卫）。这个「永不重建」的缓存策略是本文件两次生产事故的舞台。

### 13.2 双模式选择（模块级分支）

```python
_PG_URI = os.environ.get("CHECKPOINT_DB_URI", "")

if _PG_URI.startswith("postgresql://"):
    checkpointer = _PostgresCheckpointer(_PG_URI)   # 生产：AsyncPostgresSaver
else:
    checkpointer = _DynamicCheckpointer()            # 开发：AsyncSqliteSaver
```

SQLite 模式的库文件在**全局共享目录** `src/agent/shared/checkpoint/checkpoints.sqlite`（可被 `SHARED_RESOURCES_PATH` 覆盖），2026-08-27 拍板：checkpoint 全局共享、不随工作区切换，切工作区不丢历史会话。`_DynamicCheckpointer.__aenter__` 每次**重新解析路径**（`_resolve_checkpoint_path()`），让配置变更免重启生效。

### 13.3 两次事故与两层修法（本文件最有价值的部分）

**事故一（2026-09-08）**：langgraph_api 跨请求复用 PG 连接，postgres 容器 recreate 掐断旧连接后，所有 run 与 `GET /threads/{id}/state` 齐报 `psycopg.OperationalError: the connection is closed`。当时结论「重启 langgraph-api 即可」——后被证伪。

**事故二（2026-09-24）**：重启**治不了本**，因为线程缓存永不重建，任何一次连接被关 = 该线程**永久报废**（一条问数约 6s 即 error、state 接口瞬时 500、重启后第一波 run 再次中毒）。而关连接的元凶是旧版自己的代码：`checkpointer` 是模块级单例，`self._cm` 是**一个共享可变槽位**，但 `get_checkpointer()` 每线程各调一次——多线程 `__aenter__` 互相覆盖 `_cm`，任何一次 `__aexit__` 关掉的都是「最后一个」cm，即**别的线程正在用的活连接**。

两层修法（模块 docstring 原话：缺一不可）：

```python
# ① 连接上下文按线程存放：各线程各管各的，绝不闭别人的连接
class _PostgresCheckpointer:
    def __init__(self, uri: str) -> None:
        self._uri = uri
        self._local = threading.local()

    async def __aenter__(self):
        cm = _pg_saver_cls().from_conn_string(self._uri)
        saver = await cm.__aenter__()
        saver._pg_uri = self._uri      # 供自愈重连
        self._local.cm = cm            # 先登记再 setup：setup 失败也不留半开上下文
        try:
            await saver.setup()        # 幂等建表（新库首次使用即自动初始化）
        except Exception:
            self._local.cm = None
            await cm.__aexit__(None, None, None)
            raise
        return saver
```

```python
# ② _SelfHealingPostgresSaver：不依赖「谁关了连接」的判断正确，用前体检、死了就重连
class _SelfHealingPostgresSaver(AsyncPostgresSaver):
    _pg_uri: str = ""

    async def _ensure_conn(self, where: str) -> None:
        conn = getattr(self, "conn", None)
        if conn is not None and not getattr(conn, "closed", True):
            return  # 连接还活着，零成本快路径
        ...
        self.conn = await AsyncConnection.connect(
            uri, autocommit=True, prepare_threshold=0, row_factory=dict_row
        )
        await _base_setup(self)   # 幂等建表（重连后同样需要）
        _note_heal(where)         # WARNING + 计数，观测「到底谁在关连接」

    @asynccontextmanager
    async def _cursor(self, *, pipeline: bool = False):
        await self._ensure_conn("_cursor")   # 在拿锁之前做，避免与 self.lock 互锁
        async with _base_cursor(self, pipeline=pipeline) as cur:
            yield cur
```

解析（对应回本教程前文的知识点）：

- 重连参数 `autocommit=True, prepare_threshold=0, row_factory=dict_row` **逐字复刻 `from_conn_string` 的三件套**（§5.4）——自建连接如果不一致，行为会和官方路径漂移。
- `_ensure_conn` 挂在 `_cursor` 上是因为**所有**读写（get_tuple/list/put/put_writes/setup）最终都过 `_cursor`，一个卡点覆盖全部路径；放在**拿锁之前**是刻意的：若在锁内重连，重连本身的协程调度可能与 `self.lock` 形成互锁。
- 重连后必须再 `setup()`：迁移表在库侧是持久的，所以这基本是 no-op，但「新库 + 首次连接即断」的边角情形下它能自愈建表——幂等设计的红利。
- ①只防「自己人误关」，②兜住一切外因（PG 重启、网络闪断、GC）；`_note_heal` 的 WARNING + 全局计数是观测钩子，用来反推连接到底被谁关掉。
- 子类里不用 zero-arg `super()`，而是提前绑定 `_base_cursor = AsyncPostgresSaver._cursor`、`_base_setup = AsyncPostgresSaver.setup` 的**未绑定引用**——`_cursor` 已被 `@asynccontextmanager` 包装，闭包内的 super() 名字解析容易踩坑，显式引用最稳。
- 延迟构造（`_pg_saver_cls()`）：非 PG 环境根本不 import psycopg，开发机没装 PG 驱动也能跑 SQLite 模式。

**两个 Windows 细节**：print 不打 emoji（宿主机 GBK 控制台会 `UnicodeEncodeError` 直接炸 import）；URI 只打 `@` 后半段（不泄密码）。

### 13.4 与 langgraph_api 缓存策略的适配关系

本项目没有 fork langgraph_api，而是**顺着它的「线程本地 + 只建不改」约束设计自己的 saver**：既然框架层保证不了连接常活，就在 saver 层保证「连接死了也能活」。这是对接第三方运行时缓存策略时的通用思路：**改不了缓存策略，就让被缓存的对象自愈**。

---

## 14. 完整代码示例（4 个场景）

### 场景一：MemorySaver 基础多轮会话

```python
"""场景一：MemorySaver —— 同一 thread_id 累积会话状态。"""
from typing import Annotated, TypedDict
import operator

from langgraph.checkpoint.memory import InMemorySaver  # MemorySaver 是其别名
from langgraph.graph import END, START, StateGraph


class State(TypedDict):
    history: Annotated[list[str], operator.add]  # reducer：追加合并


def note(state: State) -> dict:
    last = state["history"][-1]
    return {"history": [f"echo: {last}"]}


builder = StateGraph(State)
builder.add_node("note", note)
builder.add_edge(START, "note")
builder.add_edge("note", END)

checkpointer = InMemorySaver()          # 换成 SqliteSaver/PostgresSaver 只改这一行
graph = builder.compile(checkpointer=checkpointer)

cfg = {"configurable": {"thread_id": "user-42"}}

graph.invoke({"history": ["你好"]}, cfg)
graph.invoke({"history": ["再见"]}, cfg)

state = graph.get_state(cfg)
print(state.values["history"])
# ['你好', 'echo: 你好', '再见', 'echo: 再见']  ← 两轮 invoke 的状态在同一线程里累积

# 换一个 thread_id 就是全新会话
other = graph.invoke({"history": ["hi"]}, {"configurable": {"thread_id": "user-43"}})
print(other["history"])   # ['hi', 'echo: hi']
```

要点：`thread_id` 是记忆的唯一边界；reducer（`operator.add`）+ pending writes 幂等去重（§2.2）保证重试不重复追加。

### 场景二：SqliteSaver / AsyncSqliteSaver 磁盘持久化 + 重启恢复

```python
"""场景二：AsyncSqliteSaver —— 进程重启后从磁盘恢复会话（模拟两次独立运行）。"""
import asyncio
from typing import Annotated, TypedDict
import operator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

DB_PATH = "checkpoints_demo.sqlite"   # 生产上放共享盘/数据卷；本项目放 src/agent/shared/checkpoint/


class State(TypedDict):
    log: Annotated[list[str], operator.add]


def work(state: State) -> dict:
    return {"log": [f"step-{len(state['log'])}"]}


def build_graph():
    b = StateGraph(State)
    b.add_node("work", work)
    b.add_edge(START, "work")
    b.add_edge("work", END)
    return b


async def main():
    cfg = {"configurable": {"thread_id": "durable-1"}}

    # ── 第一次运行：写 3 步 ──
    async with AsyncSqliteSaver.from_conn_string(DB_PATH) as saver:  # setup() 懒触发，自动建表 + WAL
        g = build_graph().compile(checkpointer=saver)
        for _ in range(3):
            await g.ainvoke({"log": []}, cfg)
        print((await g.aget_state(cfg)).values["log"])
        # ['step-0', 'step-1', 'step-2']
    # ⚠ 必须走出 async with：aiosqlite 后台线程不关，进程会挂着不退出

    # ── 「重启」：新事件循环、新 saver 实例、同一个文件 ──
    async with AsyncSqliteSaver.from_conn_string(DB_PATH) as saver:
        g = build_graph().compile(checkpointer=saver)
        state = await g.aget_state(cfg)          # 历史完整回来了
        print(state.values["log"], state.next)   # ['step-0', 'step-1', 'step-2'] ()
        await g.ainvoke({"log": []}, cfg)        # 在旧状态上继续
        print((await g.aget_state(cfg)).values["log"])
        # ['step-0', 'step-1', 'step-2', 'step-3']


if __name__ == "__main__":
    asyncio.run(main())
```

同步版对照（脚本/测试场景）：

```python
import sqlite3
from langgraph.checkpoint.sqlite import SqliteSaver

conn = sqlite3.connect("checkpoints_demo.sqlite", check_same_thread=False)
with SqliteSaver(conn) as saver:        # 内部 threading.Lock 串行化全部读写
    graph = build_graph().compile(checkpointer=saver)
    graph.invoke({"log": []}, {"configurable": {"thread_id": "sync-1"}})
```

### 场景三：时间旅行（get_state_history + fork 重放 + update_state）

```python
"""场景三：回到历史 checkpoint 分叉重放，以及手动改写状态。"""
from typing import Annotated, TypedDict
import operator

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph


class State(TypedDict):
    nums: Annotated[list[int], operator.add]


def add_one(state: State) -> dict:
    return {"nums": [state["nums"][-1] + 1]}


b = StateGraph(State)
b.add_node("add_one", add_one)
b.add_edge(START, "add_one")
b.add_edge("add_one", END)
graph = b.compile(checkpointer=InMemorySaver())

cfg = {"configurable": {"thread_id": "tt-1"}}
graph.invoke({"nums": [0]}, cfg)   # 第一轮：输入追加 0，节点追加 1 → [0, 1]
graph.invoke({"nums": []}, cfg)    # 第二轮：空输入不追加，从旧状态续跑，节点追加 2 → [0, 1, 2]

# ① 列出全部历史（倒序）
history = list(graph.get_state_history(cfg))
for snap in history:
    print(snap.metadata["step"], snap.metadata["source"], snap.values["nums"], snap.config["configurable"]["checkpoint_id"])

# ② 挑一个中间快照（比如 nums == [0, 1] 的那个），从它分叉重放
target = next(s for s in history if s.values["nums"] == [0, 1])
fork_cfg = target.config                      # 关键：带上 checkpoint_id
result = graph.invoke(None, fork_cfg)         # None = 不带新输入，纯续跑 → source="fork"
print(result["nums"])

# ③ 手动改写状态：假装 add_one 输出的是 100
#    update_state 返回指向「新 checkpoint」的 config（原快照不被改写）
new_cfg = graph.update_state(fork_cfg, {"nums": [100]}, as_node="add_one")
print(graph.get_state(new_cfg).values["nums"])   # [0, 1, 100]
print(graph.get_state(fork_cfg).values["nums"])  # [0, 1] ← 历史快照原样保留

# ④ 快照的 next 为空 = 该线程已跑完；非空 = 停在中断/半途，可 invoke(None, cfg) 续跑
print(graph.get_state(new_cfg).next)   # ()
```

要点：fork **不改写**原历史（新 checkpoint 的 parent 指向目标点，形成分支）；`get_state_history` 内部一次性 `list()` 吃完避免占游标（§8.2）；显式指定 `checkpoint_id` 时**不会**混入 pending writes（§8.1）。

### 场景四：Store 长期记忆（跨线程 + 语义检索）

```python
"""场景四：InMemoryStore 做跨会话用户记忆；节点内通过 runtime.store 读写。"""
from typing import Annotated, Any, TypedDict
import operator

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime          # langgraph 1.x：节点第二参数注入 Runtime
from langgraph.store.memory import InMemoryStore


# ── 假嵌入器：演示用（生产换成 init_embeddings("openai:text-embedding-3-small")）──
class FakeEmbeddings:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._one(text)

    def _one(self, t: str) -> list[float]:
        # 极简「词袋哈希」向量，只为跑通流程
        v = [0.0] * 16
        for ch in t:
            v[ord(ch) % 16] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]


store = InMemoryStore(index={"dims": 16, "embed": FakeEmbeddings(), "fields": ["text"]})

# 直接 API（跨线程共享，不需要 thread_id）
store.put(("users", "u1", "memories"), "m1", {"text": "用户喜欢 PostgreSQL，讨厌 ORM"})
store.put(("users", "u1", "memories"), "m2", {"text": "用户住在杭州，时区 Asia/Shanghai"})
print(store.get(("users", "u1", "memories"), "m1").value)

hits = store.search(("users", "u1", "memories"), query="数据库偏好", limit=1)
print(hits[0].key, round(hits[0].score, 3))    # 语义最近的一条 + 相似度分

print(store.list_namespaces(prefix=("users",)))   # [('users', 'u1', 'memories')]


# ── 在图里用：compile(store=...)，节点经 Runtime 访问 ──
class State(TypedDict):
    question: str
    context: Annotated[list[str], operator.add]
    answer: str


def recall(state: State, runtime: Runtime) -> dict:
    hits = runtime.store.search(("users", "u1", "memories"), query=state["question"], limit=2)
    return {"context": [h.value["text"] for h in hits]}


def answer(state: State, runtime: Runtime) -> dict:
    # 把有价值的新事实写回长期记忆（下一个会话、另一个 thread 也能搜到）
    runtime.store.put(
        ("users", "u1", "memories"),
        f"q-{len(state['context'])}",
        {"text": f"问过：{state['question']}"},
    )
    return {"answer": f"基于记忆回答：{state['question']} | 参考: {state['context']}"}


b = StateGraph(State)
b.add_node("recall", recall)
b.add_node("answer", answer)
b.add_edge(START, "recall")
b.add_edge("recall", "answer")
b.add_edge("answer", END)
graph = b.compile(checkpointer=InMemorySaver(), store=store)   # 两套持久化各司其职

# 两个不同 thread：checkpoint 互相隔离，store 记忆共享
out = graph.invoke({"question": "我常用什么数据库", "context": []},
                   {"configurable": {"thread_id": "session-A"}})
print(out["answer"])
out2 = graph.invoke({"question": "我常用什么数据库", "context": []},
                    {"configurable": {"thread_id": "session-B"}})
print("session-B 也搜到了 session-A 写入的记忆:", len(out2["context"]) > 0)
```

生产版替换清单：`InMemoryStore` → `PostgresStore`（pgvector）或 `SqliteStore`（sqlite-vec），嵌入器 → 真实模型；异步图里用 `await store.aput/asearch`（AsyncPostgresStore 会自动把同一 tick 的多个操作合批，§9.2）。带 TTL 的写法：

```python
from langgraph.store.postgres import AsyncPostgresStore

async with AsyncPostgresStore.from_conn_string(
    PG_URI,
    index={"dims": 1024, "embed": "openai:text-embedding-3-small", "fields": ["text"]},
    ttl={"default_ttl": 60 * 24 * 7, "refresh_on_read": True},   # 7 天，读即续期
) as store:
    await store.setup()            # 必须显式调用（含 pgvector 扩展与索引迁移）
    await store.start_ttl_sweeper()
    await store.aput(("users", "u1"), "m1", {"text": "..."}, ttl=60)  # 单条覆盖为 60 分钟
```

### 附：给 checkpoint 加密（EncryptedSerializer）

```python
import os
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.encrypted import from_pycryptodome_aes

os.environ["LANGGRAPH_AES_KEY"] = "0" * 32          # 16/24/32 字节；生产从密管系统注入
saver = InMemorySaver(serde=from_pycryptodome_aes())  # 任意后端都可换 serde
# 落盘的 type 列会变成 "msgpack+aes"；无 "+" 的旧数据明文直通
```

---

## 15. 总结与最佳实践

1. **选型**：单测/原型 → `InMemorySaver`/`InMemoryStore`；单机开发 → `SqliteSaver`/`SqliteStore`（记得让 `from_conn_string` 的上下文正常退出）；生产 → `AsyncPostgresSaver` + `AsyncPostgresStore`，并且**显式调用 `setup()`**（Postgres 两个体系都不懒建表）。
2. **ShallowPostgresSaver 不要用**（2.0.20 废弃、3.0.0 移除），少写盘的诉求用 `graph.invoke(..., durability="exit")` 表达。
3. **thread_id 是 checkpoint 的唯一边界，namespace 是 store 的唯一边界**；「会话续跑」找 checkpointer，「跨会话记忆」找 store，两个都配才是完整形态（§12 表）。
4. **序列化**：默认 JsonPlusSerializer（msgpack + ext 复活）够用；处理不可信来源的 checkpoint 时设 `LANGGRAPH_STRICT_MSGPACK=true`，合规场景套 `EncryptedSerializer`；**永远不要在生产开 `pickle_fallback`**。
5. **连接生命周期是最大的坑源**：框架层缓存 + 长连接 + 数据库重启三者相遇时，「重启应用」往往治不了本。参考本项目 §13 的双层解法——上下文按线程隔离（不误关别人的连接）+ 使用前体检自愈（`conn.closed` → 重连 + 幂等 setup），并用 WARNING 计数观测「谁在关连接」。
6. **幂等性是内建的，别破坏它**：普通 writes 按 `(task_id, idx)` 去重、blob 按版本 `DO NOTHING`、迁移按版本号记账——自定义 saver 时必须保持同样的幂等语义，否则崩溃重试会造成状态重复累加。
7. **性能细节**：长会话的存储量由「channel 值按版本去重」控制，把易变大对象放进独立 channel/state 键能减少重写；Postgres 批量操作用 pipeline/连接池（`_cursor` 自动选择）；Store 的语义检索**必须配 `index` 才生效**，否则 `query=` 静默退化。

---

## 附：术语速查

| 术语 | 含义 |
|---|---|
| super-step（超步） | Pregel 循环的一轮：触发节点并行执行 → 写回 channel → 存 checkpoint |
| thread_id | 会话/线程主键，checkpoint 的隔离边界 |
| checkpoint_ns | 命名空间，主图为 `""`，子图为其路径（父子 checkpoint 由此关联） |
| pending writes | 节点已产出但超步未收尾的写入，崩溃恢复时重放 |
| channel_versions / versions_seen | 调度状态的持久化形式：谁新写了、谁见过什么版本 |
| uuid6 | 时间可排序 UUID，checkpoint_id 由此实现「字符串排序 = 时间排序」 |
| WAL | SQLite 预写日志模式，读写并发不互斥 |
| durability | `"sync"/"async"/"exit"`，写盘时机策略（替代 ShallowSaver 的正解） |
| Op / batch | Store 的批处理原语，异步后端同 tick 合并、去重 |
| TTL sweeper | Store 过期项后台清扫器（SQLite 线程 / PG 线程或 asyncio task） |
