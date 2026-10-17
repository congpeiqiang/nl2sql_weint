# 04 - Pregel 执行引擎（LangGraph 1.2.9 源码级教程）

> 本教程基于本项目 `.venv/Lib/site-packages/langgraph/`（**langgraph 1.2.9**）中 `pregel/` 子包的 20 个源码文件逐一完整阅读整理而成。
> Pregel 是 LangGraph 的**运行时心脏**：无论你用 `StateGraph.compile()`、`@entrypoint` 函数式 API，还是 LangGraph Server 部署，最终执行的都是这里的「超步循环」（superstep loop）。
>
> 涉及源码文件（均在 `langgraph/pregel/` 下）：
> `__init__.py`、`main.py`、`_loop.py`、`_algo.py`、`_runner.py`、`_executor.py`、`_call.py`、`_io.py`、_read.py`、`_write.py`、`_validate.py`、`_checkpoint.py`、`_retry.py`、`_tools.py`、`_config.py`、`_log.py`、`_messages.py`、`types.py`、`protocol.py`、`debug.py`

---

## 目录

1. [Pregel 架构概述：BSP 模型与 superstep](#一pregel-架构概述bsp-模型与-superstep)
2. [Pregel 主类（main.py）](#二pregel-主类mainpy)
3. [invoke / ainvoke / stream / astream 四种调用模式](#三invoke--ainvoke--stream--astream-四种调用模式)
4. [_loop.py：执行主循环](#四_looppy执行主循环)
5. [_algo.py：调度算法](#五_algopy调度算法)
6. [_runner.py：节点运行器](#六_runnerpy节点运行器)
7. [_executor.py：线程池执行器](#七_executorpy线程池执行器)
8. [_call.py：节点调用与函数式 API](#八_callpy节点调用与函数式-api)
9. [_io.py：输入输出映射](#九_iopy输入输出映射)
10. [_read.py / _write.py：通道读写机制](#十_readpy--_writepy通道读写机制)
11. [_validate.py：图验证](#十一_validatepy图验证)
12. [_retry.py：重试与超时机制](#十二_retrypy重试与超时机制)
13. [_checkpoint.py：检查点集成](#十三_checkpointpy检查点集成)
14. [recursion_limit 与 GraphRecursionError](#十四recursion_limit-与-graphrecursionerror)
15. [辅助模块：debug / messages / tools / protocol / types / log / config](#十五辅助模块)
16. [完整代码示例](#十六完整代码示例)
17. [总结与排错速查](#十七总结与排错速查)

---

## 一、Pregel 架构概述：BSP 模型与 superstep

### 1.1 名字从何而来

「Pregel」直接来自 Google 2010 年论文 *Pregel: A System for Large-Scale Graph Processing*，其核心是 **BSP（Bulk Synchronous Parallel，整体同步并行）** 计算模型。LangGraph 借用该模型组织 Agent 工作流的执行。`main.py` 中 `Pregel` 类的 docstring 是官方最权威的描述（450–703 行，节选）：

```python
# main.py — Pregel 类 docstring（节选，原文为英文，此处保留原文并附中文解释）
# Pregel manages the runtime behavior for LangGraph applications.
#
# Each step consists of three phases:
#
#   Plan:     select the actors (PregelNode) that subscribed to channels
#             updated in the previous step
#   Execution: execute all selected actors concurrently, until all finish,
#             or one fails, or a timeout is hit. Channel updates are NOT
#             visible to other actors within the same step.
#   Update:   apply the writes produced by actors to the channels,
#             atomically, at the end of the step.
```

翻译并展开这三个阶段：

| 阶段 | 中文 | 做什么 | 对应源码 |
|---|---|---|---|
| **Plan** | 规划 | 找出「订阅了上一步被更新通道」的节点（actor） | `_algo.prepare_next_tasks()` |
| **Execution** | 执行 | 所有选中节点**并发**执行，直到全部完成/一个失败/超时；**同一超步内通道更新对其他节点不可见** | `_runner.PregelRunner.tick()` + `_executor.BackgroundExecutor` |
| **Update** | 更新 | 把本步所有节点的写入**原子性**地批量应用到通道 | `_algo.apply_writes()`（在 `_loop.after_tick()` 中调用） |

一次「Plan → Execution → Update」称为一个 **superstep（超步）**。BSP 的关键不变量是：

1. **步内隔离**：step N 中节点 A 的写入，节点 B 在同一步内读不到 —— 这就是「bulk synchronous」的含义，同步点（barrier）在步末。
2. **步间可见**：step N 的写入在 step N+1 开始时对所有节点可见。
3. **确定性重放**：给定同一 checkpoint 和同一输入，任务调度顺序、任务 ID 都完全可复现（task_id 由内容哈希决定，见 §5.4）。

### 1.2 核心概念对照表

| 概念 | 类/结构 | 说明 |
|---|---|---|
| Actor | `PregelNode`（`_read.py`） | 编译后图中的一个节点：订阅哪些通道（triggers）、读哪些通道（channels）、执行什么逻辑（bound）、写哪些通道（writers） |
| 状态 | Channel（`langgraph/channels/`） | `LastValue`（默认）、`Topic`（累积/Send 队列）、`BinaryOperatorAggregate`（reducer）、`EphemeralValue`（读后清空）、`UntrackedValue`、`DeltaChannel` 等 |
| 任务 | `PregelExecutableTask`（`langgraph/types.py`） | 一次节点执行实例：`(name, input, proc, writes, config, triggers, retry_policy, cache_key, id, path, ...)` |
| 超步循环 | `PregelLoop`（`_loop.py`） | 驱动 `tick()/after_tick()`，管理 checkpoint、流、pending writes |
| 运行器 | `PregelRunner`（`_runner.py`） | 在一个超步内并发跑任务、提交写入、处理错误/中断 |
| 调度 | `_algo.prepare_next_tasks / apply_writes` | 版本比较决定谁被触发；步末统一提交写入 |
| 快照 | `Checkpoint`（`_checkpoint.py`） | `channel_values + channel_versions + versions_seen`，配合 checkpointer 持久化 |

### 1.3 版本驱动的触发机制（最重要的设计）

LangGraph 不用「边」来驱动执行，而是用**通道版本比较**：

- `checkpoint.channel_versions[chan]`：通道 chan 的最新版本号（每次被写入就 +1）。
- `checkpoint.versions_seen[node][chan]`：节点 node 上次被调度时「看到」的 chan 的版本号。

`_algo._triggers()` 的判定逻辑：**只要存在一个订阅通道 `chan` 满足 `channel_versions[chan] > versions_seen[node][chan]`，节点就被触发**。这样：

- 环（cycle）天然支持：节点写回自己订阅的通道，版本推高，下一步再次触发；
- 并行 fan-out 天然支持：一步内写入 N 个通道，下一步订阅这些通道的 N 个节点全部触发、并发执行；
- 重放幂等：`apply_writes` 执行时会把每个任务的 triggers 版本记入 `versions_seen`，同一任务不会被重复触发。

### 1.4 模块地图（20 个文件职责一览）

```text
langgraph/pregel/
├── __init__.py      # 3 行：from langgraph.pregel.main import NodeBuilder, Pregel
├── main.py          # ★ Pregel 主类（4364 行）：构造/validate/stream/astream/invoke/ainvoke/update_state
├── _loop.py         # ★ PregelLoop（1988 行）：超步主循环、checkpoint 落盘、流输出、interrupt 抑制
├── _algo.py         # ★ 调度算法（1460 行）：prepare_next_tasks / prepare_single_task / apply_writes
├── _runner.py       # ★ PregelRunner（941 行）：单超步内并发执行任务、commit、panic
├── _executor.py     # BackgroundExecutor / AsyncBackgroundExecutor：线程池与 asyncio 任务管理
├── _retry.py        # run_with_retry / arun_with_retry：RetryPolicy 退避 + TimeoutPolicy 看门狗
├── _call.py         # 函数式 API：call() / get_runnable_for_task / SyncAsyncFuture
├── _io.py           # map_input / map_command / map_output_values / map_output_updates
├── _read.py         # ChannelRead + PregelNode（actor 容器）
├── _write.py        # ChannelWrite / ChannelWriteEntry：写入通道的 Runnable
├── _validate.py     # validate_graph / validate_keys：编译期合法性检查
├── _checkpoint.py   # empty_checkpoint / create_checkpoint / channels_from_checkpoint
├── _tools.py        # StreamToolCallHandler：stream_mode="tools"
├── _messages.py     # StreamMessagesHandler：stream_mode="messages"（LLM 逐 token）
├── debug.py         # stream_mode="debug"/"checkpoints"/"tasks" 的事件负载构造
├── protocol.py      # PregelProtocol 抽象接口 + StreamProtocol/StreamChunk
├── types.py         # 纯 deprecation re-export（类型已搬到 langgraph.types）
├── _log.py          # 3 行：logger = logging.getLogger("langgraph")
└── _config.py       # 空文件（内容已搬到 langgraph/_internal/_config.py）
```

### 1.5 一次执行的宏观时序

```text
graph.invoke(input, config)
  └─ Pregel.stream(...)                        # main.py —— invoke 只是 stream 的消费者
       ├─ SyncPregelLoop.__enter__()           # _loop.py —— 加载 checkpoint、建通道、算 step/stop
       │    └─ loop._first(input)              # 把输入写进通道（INPUT 超步，step=-1 语义）
       ├─ while loop.tick():                   # ← 每个 superstep 一轮
       │    │   # tick(): prepare_next_tasks → self.tasks
       │    ├─ loop.match_cached_writes()      # 节点缓存命中直接复用写入
       │    ├─ for _ in runner.tick(tasks):    # _runner.py —— 并发执行本步任务
       │    │      yield from _output(...)     # 把队列里的流事件吐给调用方
       │    │      # 任务内部: run_with_retry → PregelNode.invoke
       │    │      #            → ChannelRead(读) → bound(逻辑) → ChannelWrite(写 deque)
       │    │      # 每个任务完成 → runner.commit → loop.put_writes（落盘+发流）
       │    └─ loop.after_tick()               # apply_writes（barrier！）→ _put_checkpoint
       ├─ loop.__exit__() → _suppress_interrupt # 顶层吞掉 GraphInterrupt、产出 loop.output
       └─ status == "out_of_steps" → raise GraphRecursionError
```

---

## 二、Pregel 主类（main.py）

`main.py` 共 4364 行，包含两个公开类：`NodeBuilder`（低层建图 DSL）和 `Pregel`（运行时本体）。

### 2.1 NodeBuilder：低层节点构建器（206–377 行）

`NodeBuilder` 是一个 fluent builder，`StateGraph.compile()` 内部最终就是为每个节点产出一个 `PregelNode`。它的 API：

| 方法 | 作用 |
|---|---|
| `subscribe_only(channel)` | 输入 = 单个通道的值；该通道同时是 trigger |
| `subscribe_to(*channels, read=True)` | 输入 = `{channel: value}` dict；这些通道都是 trigger |
| `read_from(*channels)` | 追加**只读不触发**的通道 |
| `do(node)` | 设置节点逻辑（`coerce_to_runnable` 包装；多次调用串成 `RunnableSeq`） |
| `write_to(*channels, **kwargs)` | 输出写往哪些通道；`kwargs` 可指定 `channel=mapper` 或固定值 |
| `meta(*tags, **metadata)` | 标签与元数据 |
| `add_retry_policies(policy)` / `add_cache_policy(policy)` / `set_timeout(t)` | 重试/缓存/超时 |
| `build()` | 产出 `PregelNode(channels, triggers, tags, metadata, writers=[ChannelWrite(...)], bound, retry_policy, cache_policy, timeout)` |

### 2.2 Pregel 类关键属性（705–836 行）

```python
# main.py — Pregel 属性（节选）
class Pregel(
    ...,
    PregelProtocol[StateT, ContextT, InputT, OutputT],
):
    nodes: dict[str, PregelNode]
    """图的节点集合：name -> PregelNode（actor）。"""

    channels: dict[str, BaseChannel]
    """图的通道集合：name -> channel 实例。__init__ 里强制注入保留通道 TASKS。"""

    stream_mode: StreamMode = "values"
    """默认流模式。可选 values/updates/messages/custom/debug/checkpoints/tasks。"""

    stream_eager: bool = False
    """True 时任务一提交就发流（不等完成）。"""

    input_channels: str | Sequence[str]
    output_channels: str | Sequence[str]
    stream_channels: str | Sequence[str] | None

    interrupt_after_nodes: Sequence[str]
    interrupt_before_nodes: Sequence[str]

    step_timeout: float | None = None
    """整个超步的墙钟超时（传给 runner.tick 的 timeout）。"""

    debug: bool = False
    checkpointer: CheckpointerT | Literal[True] | None = None
    store: BaseStore | None = None
    cache: BaseCache | None = None
    retry_policy: RetryPolicy | None = None     # 图级默认重试策略
    cache_policy: CachePolicy | None = None     # 图级默认缓存策略
    context_schema: type[ContextT] | None = None
    config: RunnableConfig | None = None
    name: str = "LangGraph"
    trigger_to_nodes: dict[str, tuple[str, ...]] | None = None
    """trigger 通道 -> 订阅它的节点名列表，调度加速用的倒排索引。"""
    node_error_handler_map: ...                 # 节点级错误处理器映射
```

`__init__` 中两个关键动作：

```python
# main.py — __init__（节选）
def __init__(self, *, nodes, channels, ..., auto_validate: bool = True, ...):
    # 1) NodeBuilder 自动 build()
    self.nodes = {
        k: v.build() if isinstance(v, NodeBuilder) else v
        for k, v in nodes.items()
    }
    # 2) 强制注入保留通道 TASKS —— Send() 动态任务都排队在这个 Topic 里
    self.channels = {**channels, TASKS: Topic(Send, accumulate=False)}
    ...
    if auto_validate:
        self.validate()
```

要点：

- **`TASKS` 是保留通道**：类型 `Topic(Send, accumulate=False)`。所有 `Send(...)` 对象（以及函数式 API 的 `Call`）都被写进这个通道，下一步（或本步内，经 `accept_push`）被展开为 PUSH 任务。用户通道叫 `TASKS` 会在 `validate_graph` 阶段直接报错。
- **`trigger_to_nodes`**：由 `_trigger_to_nodes(nodes)`（4175 行）构建的倒排索引 `{trigger通道: (节点名, ...)}`，让 `prepare_next_tasks` 不必全量扫描节点（见 §5.2）。

### 2.3 validate()：编译期验证入口（933 行起）

```python
# main.py — validate()（节选）
def validate(self) -> Self:
    # 每个带 timeout 的节点先检查「是否支持超时」
    for node in self.nodes.values():
        validate_timeout_supported(node)
    validate_graph(
        nodes=self.nodes,
        channels=self.channels,
        managed=...,
        input_channels=self.input_channels,
        output_channels=self.output_channels,
        stream_channels=self.stream_channels_asis,
        interrupt_after_nodes=self.interrupt_after_nodes,
        interrupt_before_nodes=self.interrupt_before_nodes,
    )
    return self
```

`validate_graph` 的具体规则见 §11。

### 2.4 _defaults()：运行参数收敛（2543–2613 行）

每次 `stream/invoke` 开始时，`_defaults(config, ...)` 把「实例属性 / config / 调用参数」三个来源收敛成一份运行参数。源码中的关键判定：

```python
# main.py — _defaults（节选 + 中文注释）
def _defaults(self, config, *, stream_mode=None, output_keys=None,
              interrupt_before=None, interrupt_after=None,
              checkpointer=None, store=None, cache=None,
              durability=None, subgraphs=False, ...):
    config = ensure_config(self.config, config)

    # recursion_limit 必须 >= 1，否则 ValueError
    if config["recursion_limit"] < 1:
        raise ValueError("recursion_limit must be at least 1")

    # output_keys 缺省 = stream_channels_asis
    output_keys = output_keys or self.stream_channels_asis

    # checkpointer 三态解析：
    #   False        -> None（显式关闭）
    #   config 里有 CONFIG_KEY_CHECKPOINTER -> 用之（子图继承父图的通道）
    #   True         -> 只允许作为子图使用，否则 RuntimeError
    # 有 checkpointer 但 config 无 thread_id/checkpoint_ns/checkpoint_id：
    if checkpointer and not config.get(CONF):
        raise ValueError(
            "Checkpointer requires one or more of the following "
            "'configurable' keys: thread_id, checkpoint_ns, checkpoint_id"
        )

    # durability 缺省 "async"
    durability = durability or "async"
    ...
```

**durability 三模式**（贯穿 `_loop.py`）：

| 值 | 含义 | 使用场景 |
|---|---|---|
| `"sync"` | 每次 checkpoint **阻塞等待**落盘完成 | 需要强持久化保证；stream() 末尾会 `loop._put_checkpoint_fut.result()` |
| `"async"` | **默认**。checkpoint 提交给后台执行器异步落盘，主循环不等 | 常规运行 |
| `"exit"` | 只在图退出时保存一次 checkpoint | 顶层图无 checkpointer、或一次性运行 |

---

## 三、invoke / ainvoke / stream / astream 四种调用模式

### 3.1 结论先行：stream 才是本体

四种模式的真实关系是——**`stream()` 是唯一实现了完整超步循环的入口，`invoke()` 只是它的一个「收集器」**：

```python
# main.py — invoke()（3836–3957 行，节选 + 注释）
def invoke(self, input, config=None, *, stream_mode="values",
           output_keys=None, interrupt_before=None, interrupt_after=None,
           version="v1", **kwargs):
    output_keys = output_keys or self.output_channels

    if version == "v1":
        # 消费自己的 stream：values 模式下同时订 updates+values 两种流
        chunks = []
        interrupts = []
        latest = None
        stream_modes = (
            ["updates", "values"] if stream_mode == "values" else stream_mode
        )
        for chunk in self.stream(input, config, stream_mode=stream_modes,
                                 interrupt_before=interrupt_before,
                                 interrupt_after=interrupt_after, **kwargs):
            if stream_mode == "values":
                # chunk 是 (mode, payload) 或 (ns, mode, payload)
                ...
                if mode == "updates":
                    # 收集 __interrupt__ 键 -> interrupts
                    if isinstance(payload, dict) and INTERRUPT in payload:
                        interrupts.append(payload[INTERRUPT])
                elif mode == "values":
                    latest = payload      # 记住最新完整状态
            else:
                chunks.append(chunk)

        if stream_mode == "values":
            # 有中断：把中断附加进最终输出
            if interrupts:
                return (
                    {**latest, INTERRUPT: interrupts}
                    if isinstance(latest, dict)
                    else {INTERRUPT: interrupts}
                )
            return latest                 # 正常：返回最后一个 values 快照
        return chunks                     # 非 values 模式：返回 chunk 列表
    else:
        # v2：返回 GraphOutput(value=latest, interrupts=tuple(interrupts))
        ...
```

这解释了三个常见「玄学」现象：

1. **`invoke` 遇到 `interrupt()` 不抛异常**，而是返回 `{..., "__interrupt__": (Interrupt(...),)}` —— 因为 `GraphInterrupt` 在 `loop._suppress_interrupt` 就被顶层吞掉了（§4.8），invoke 只是把流里的 INTERRUPT 更新收集起来。
2. **invoke 的返回值是「最后一次 values 快照」**，不是某节点的返回值 —— 节点的返回值只是写进通道的更新。
3. **invoke 传 `stream_mode="updates"` 时返回的是 chunk 列表**而不是状态 dict。

`ainvoke()`（4013–4134 行）与 `invoke` 完全对称：`async for chunk in self.astream(...)`，收集逻辑一字不差。

### 3.2 stream() 完整流程解析（2655–3022 行）

`stream()` 是一个生成器，按执行顺序分七段：

**① 建流队列与回调**

```python
# main.py — stream()（节选）
stream: SyncQueue[StreamChunk] = SyncQueue()
config = ensure_config(self.config, config)
...
callback_manager = CallbackManager.configure(...)
run_manager = callback_manager.on_chain_start(...)
```

`SyncQueue` 是线程安全队列。**所有流事件（节点内部任意深度）都通过 `stream.put((ns, mode, payload))` 汇入这一个队列**，生成器主线程只负责从队列取出并 yield —— 这是「节点执行」与「流输出」解耦的关键。

**② 按 stream_mode 挂载处理器**

```python
# main.py — stream()（节选 + 注释）
if "messages" in stream_modes:
    # LLM 逐 token：把 StreamMessagesHandler 挂进 inheritable_handlers，
    # 随 config 传播到每个 chat model 调用
    handlers.append(StreamMessagesHandler(stream.put, subgraphs, parent_ns))
if "tools" in stream_modes:
    handlers.append(StreamToolCallHandler(stream.put, subgraphs, parent_ns))
if "custom" in stream_modes:
    # get_stream_writer() 拿到的就是这个闭包
    def stream_writer(c): stream.put((ns, "custom", c))
```

**③ 构造 Runtime 并塞进 config**

```python
runtime = Runtime(context=..., store=..., stream=stream_writer, ...)
config[CONF][CONFIG_KEY_RUNTIME] = runtime.merge(parent_runtime)
```

节点函数签名的 `runtime: Runtime` 参数注入（本项目 MEMORY 里强调的 `ToolRuntime` 注入机制）就源于此。

**④ 进入 SyncPregelLoop 上下文**

```python
# main.py — stream()（节选）
with SyncPregelLoop(
    input,
    stream=StreamProtocol(stream.put, stream_modes),
    config=config, store=store, cache=cache, checkpointer=checkpointer,
    nodes=self.nodes, specs=self.channels,
    output_keys=output_keys,
    input_keys=self.input_channels,
    stream_keys=self.stream_channels_asis,
    interrupt_before=interrupt_before, interrupt_after=interrupt_after,
    manager=run_manager, durability=durability,
    trigger_to_nodes=self.trigger_to_nodes,
    retry_policy=retry_policy, cache_policy=cache_policy, ...
) as loop:
```

`StreamProtocol`（`protocol.py` 275–288 行）就是一个「带 modes 过滤的 callable」：

```python
# protocol.py
StreamChunk = tuple[tuple[str, ...], str, Any]   # (namespace, mode, payload)

class StreamProtocol:
    __slots__ = ("modes", "__call__")
    def __init__(self, __call__, modes: set[StreamMode]) -> None:
        self.__call__ = ...   # 即 stream.put
        self.modes = modes
```

**⑤ 创建 PregelRunner**

```python
# main.py — stream()（节选）
runner = PregelRunner(
    submit=config[CONF].get(CONFIG_KEY_RUNNER_SUBMIT,
                            weakref.WeakMethod(loop.submit)),
    put_writes=weakref.WeakMethod(loop.put_writes),
    node_finished=node_finished,
    node_error_handler_map=self.node_error_handler_map,
    schedule_error_handler=loop.schedule_error_handler,
)
```

注意 `submit` 和 `put_writes` 都用 **weakref.WeakMethod** 传递 —— runner 不持有 loop 的强引用，避免「流被中途放弃时 loop/executor 泄漏」。

**⑥ 超步主循环（全文最核心的 10 行）**

```python
# main.py — stream()（节选，逐行注释）
while loop.tick():                                  # Plan：算出本步任务；False=结束
    for task in loop.match_cached_writes():          # 节点缓存命中：直接复用写入
        loop.output_writes(task.id, task.writes, cached=True)
    for _ in runner.tick(                            # Execution：并发跑任务
        [t for t in loop.tasks.values() if not t.writes],  # 跳过已有写入的（重放/缓存）
        timeout=self.step_timeout,                   # 整步墙钟超时
        get_waiter=get_waiter,                       # 并发流式的“哨兵”future
        schedule_task=loop.accept_push,              # Send/call() 动态插入任务的钩子
    ):
        # runner 每 yield 一次 = 有任务完成/有流事件 -> 立刻排空队列
        yield from _output(stream_mode, print_mode, subgraphs,
                           stream.get, queue.Empty, version,
                           _output_mapper, _state_mapper)
    loop.after_tick()                                # Update：apply_writes + checkpoint
    emit_graph_lifecycle_events(loop)
    if durability_ == "sync":
        loop._put_checkpoint_fut.result()            # sync 模式：阻塞等落盘
```

`runner.tick` 是**生成器**而非普通函数 —— 这是为了让 `stream()` 能在任务执行间隙把已产生的流事件及时 yield 出去（否则要等整个超步跑完才能输出，流式就失效了）。详见 §6.1。

**⑦ 收尾**

```python
# main.py — stream()（节选）
# 循环结束后把队列里剩余事件排空
yield from _output(...)
if loop.status == "out_of_steps":
    raise GraphRecursionError(
        f"Recursion limit of {config['recursion_limit']} reached "
        "without hitting a stop condition. You can increase the limit "
        "by setting the `recursion_limit` config key."
    )
if loop.status == "draining":
    raise GraphDrained(...)
run_manager.on_chain_end(loop.output)
```

### 3.3 _output()：队列 → 用户可见的 chunk 形态（4184–4243 行）

```python
# main.py — _output()（节选 + 注释）
def _output(stream_mode, print_mode, stream_subgraphs, getter, empty_exc,
            version, output_mapper, state_mapper):
    while True:
        try:
            ns, mode, payload = getter()      # 从 SyncQueue 取一个 StreamChunk
        except empty_exc:                     # queue.Empty -> 队列已排空
            break
        if print_mode:                        # stream_mode 含 "debug" 且 print
            print(get_colored_text(...), payload)
        if mode not in stream_mode_set:       # 用户没订阅这个 mode -> 丢弃
            continue
        if version == "v2":
            yield {"type": mode, "ns": ns, "data": payload, ...}
        else:
            if isinstance(stream_mode, list):
                if stream_subgraphs:
                    yield (ns, mode, payload)  # 多模式 + 子图：三元组
                else:
                    yield (mode, payload)      # 多模式：二元组
            elif stream_subgraphs:
                yield (ns, payload)            # 单模式 + 子图
            else:
                yield payload                  # 单模式：直接是 payload
```

这解释了为什么 `stream_mode` 传 list 时 chunk 变成元组、`subgraphs=True` 时又多个 namespace 前缀 —— 都是这一个函数的分支。

### 3.4 四种调用模式对比表

| 维度 | `invoke` | `ainvoke` | `stream` | `astream` |
|---|---|---|---|---|
| 本质 | 消费 `stream` 收集最终值 | 消费 `astream` 收集最终值 | **超步循环本体（同步）** | **超步循环本体（异步）** |
| 返回 | 最终状态 dict（或 chunk 列表） | 同左（await） | 生成器，逐 chunk | 异步生成器，逐 chunk |
| 事件循环 | `loop.tick()` + `runner.tick()` + `concurrent.futures.wait` | `loop.tick()` + `runner.atick()` + `asyncio.wait` | 同 invoke | 同 ainvoke |
| Loop 类 | `SyncPregelLoop` | `AsyncPregelLoop` | `SyncPregelLoop` | `AsyncPregelLoop` |
| 执行器 | `BackgroundExecutor`（线程池） | `AsyncBackgroundExecutor`（asyncio task） | 同左 | 同左 |
| 节点执行 | `task.proc.invoke` | `task.proc.ainvoke`/`astream` | 同左 | 同左 |
| 流队列 | `SyncQueue` + `stream.get` | `asyncio.Queue` + `get_nowait` | 同左 | 同左 |
| 中断表现 | 返回值里带 `__interrupt__` 键 | 同左 | 流里出现 `__interrupt__` 更新后正常结束 | 同左 |
| 超步超时 | 不支持节点级 TimeoutPolicy（sync 节点报错） | 支持 `run_timeout/idle_timeout` 看门狗 | 同 invoke | 同 ainvoke |
| 典型场景 | 一次性拿结果（脚本、测试） | FastAPI/异步服务 | CLI 进度、SSE 前身 | **生产推荐**：SSE/WebSocket 流式 |

> 经验法则（与本项目实践一致）：异步 Web 服务里**永远优先 `astream`**；`ainvoke` 用于不关心中间过程的内部调用；同步 `stream/invoke` 会占住一个线程跑整个图，注意本项目 MEMORY 中「P1-14 同步阻塞搬离事件循环」的教训 —— 同步入口放进线程池而不是直接跑在 asyncio 事件循环上。

---

## 四、_loop.py：执行主循环

`_loop.py`（1988 行）定义 `PregelLoop` 基类与 `SyncPregelLoop`/`AsyncPregelLoop` 两个子类，是整个引擎的「操作系统内核」。

### 4.1 PregelLoop 关键字段

```python
# _loop.py — PregelLoop.__init__（节选 + 注释）
class PregelLoop:
    step: int                      # 当前超步号（-1 表示 INPUT 步）
    stop: int                      # 允许的最大步号（recursion_limit 决定）
    status: Literal["input", "pending", "done", "draining",
                    "interrupt_before", "interrupt_after", "out_of_steps"]
    tasks: dict[str, PregelExecutableTask]   # 本步任务表 task_id -> task
    checkpoint: Checkpoint         # 当前检查点（channel_values/versions/versions_seen）
    checkpoint_pending_writes: list[PendingWrite]  # 已持久化但未 apply 的写入
    channels: dict[str, BaseChannel]
    updated_channels: set[str]      # 上一步被更新的通道（调度加速用）
    output: Any                    # 图最终输出（__exit__ 时读 output_keys）
    is_nested: bool                # CONFIG_KEY_TASK_ID in config[CONF] -> 是否作为子图运行
    is_replaying: bool             # CONFIG_KEY_CHECKPOINT_ID in config[CONF] -> 是否时间旅行重放
    durability: Literal["exit", "async", "sync"]
    control: RunControl            # 外部控制信号（drain 请求等）
```

### 4.2 SyncPregelLoop.__enter__：加载状态、算出 step/stop

```python
# _loop.py — SyncPregelLoop.__enter__（节选 + 注释）
def __enter__(self) -> PregelLoop:
    # 三分支加载 checkpoint：
    #   a) config 显式带 checkpoint_id -> checkpointer.get_tuple(该 id)（时间旅行）
    #   b) 子图且父图给了 replay_state -> 用父图算好的状态
    #   c) 默认 -> checkpointer.get_tuple(latest)
    if self.checkpointer:
        saved = self.checkpointer.get_tuple(self.config)
    ...
    self._has_persisted_parent = saved is not None
    if saved is None:
        # 全新线程：空 checkpoint，metadata.step = -2
        checkpoint_tuple = CheckpointTuple(
            self.config, empty_checkpoint(), {"step": -2}, None, [])
    ...
    # 建后台执行器（线程池），self.submit 从此可用
    self.submit = self.stack.enter_context(BackgroundExecutor(self.config))
    # 由 checkpoint 里的 channel_values 水合出通道对象
    self.channels, self.managed = channels_from_checkpoint(
        self.specs, self.checkpoint, saver=..., config=...)
    # __exit__ 时的异常抑制器（顶层吞 GraphInterrupt）
    self.stack.push(self._suppress_interrupt)
    self.status = "input"
    # ★ 步号与上限：
    self.step = self.checkpoint_metadata["step"] + 1     # 新线程 = -2+1 = -1
    self.stop = self.step + self.config["recursion_limit"] + 1
    # 处理输入（写入通道 / resume / Command / 时间旅行 fork）
    self._first(input_keys=self.input_keys,
                updated_channels=set(self.channels))
    return self
```

注意 `step = -1` 起步：`_first` 处理输入后 `_put_checkpoint({"source": "input"})` 内 `self.step += 1`，所以第一个真正的超步是 step 0。`stop = step + recursion_limit + 1` 就是 §14 里 recursion_limit 的实现根源。

### 4.3 _first()：输入处理与恢复语义

`_first`（源码在 PregelLoop 基类）要区分四种开局：

1. **正常新输入**：`map_input(input_keys, chunk)` 把输入逐通道写进 channels（不在 input_keys 里的 key 会 `logger.warning` 丢弃）；然后

   ```python
   # _loop.py — _first（节选）
   # 先算出「旧 checkpoint 上未完成任务」作为 discard_tasks 一并 apply，
   # 保证新输入不与半途状态纠缠
   discard_tasks = prepare_next_tasks(..., for_execution=False) or ()
   updated_channels = self.apply_writes(
       self.checkpoint, self.channels,
       [*discard_tasks, PregelTaskWrites((), INPUT, input_writes, [])],
       self.get_next_version, self.trigger_to_nodes)
   self._put_checkpoint({"source": "input", "writes": ...})
   ```

   输入为空且不是恢复 → `raise EmptyInputError`。

2. **恢复中断（resume）**：判定条件是 `channel_versions 非空` 且（config 带 `CONFIG_KEY_RESUMING` / input 是 None / input 是 Command / 同一 run_id）。恢复时：

   ```python
   # _loop.py — _first（节选）：把 INTERRUPT 伪节点的 versions_seen 推到当前，
   # 表示“中断点之前的更新都已看见”，避免 should_interrupt 再次命中同一批节点
   self.checkpoint["versions_seen"].setdefault(INTERRUPT, {})
   for k in self.checkpoint["channel_versions"]:
       self.checkpoint["versions_seen"][INTERRUPT][k] = \
           self.checkpoint["channel_versions"][k]
   ```

3. **Command 输入**：`map_command(cmd)` 把 `Command(goto=..., update=..., resume=...)` 翻译成 pending writes（见 §9.2），按任务分组 `put_writes`。resume 值若是 `{interrupt_id: value}` 形式的 dict 则进 `CONFIG_KEY_RESUME_MAP`（多中断并行恢复）；存在多个 pending interrupt 但 resume 没带 id → `RuntimeError`。

4. **时间旅行（is_time_traveling）**：丢弃 RESUME 写入、清掉 INTERRUPT 写入，并 `_put_checkpoint({"source": "fork"})` —— 在历史 checkpoint 上分叉出新分支。

### 4.4 tick()：每个超步的 Plan 阶段（逐行解析）

```python
# _loop.py — PregelLoop.tick()（节选 + 逐段注释）
def tick(self) -> bool:
    # ① 步数闸：超过 stop 直接终止（外层据此抛 GraphRecursionError）
    if self.step > self.stop:
        self.status = "out_of_steps"
        return False

    # ② Plan：算出本步要执行的任务
    self.tasks = prepare_next_tasks(
        self.checkpoint, self.checkpoint_pending_writes, self.processes,
        self.channels, self.managed, self.config, self.step, self.stop,
        for_execution=True, store=self.store, checkpointer=self.checkpointer,
        manager=self.manager, trigger_to_nodes=self.trigger_to_nodes,
        updated_channels=self.updated_channels,
        retry_policy=self.retry_policy, cache_policy=self.cache_policy)

    # ③ 发 "checkpoints" 流事件（stream_mode=debug/checkpoints）
    self._emit("checkpoints", map_debug_checkpoint(...))

    # ④ 没有任务 = 图跑完了
    if not self.tasks:
        self.status = "done"
        return False

    # ⑤ 外部请求排空（优雅停机）：不再调度新任务
    if self.control.drain_requested:
        self.status = "draining"
        return False

    # ⑥ 崩溃恢复/重放：checkpoint 里存过 pending writes 的任务，
    #    把写入重新灌回 task.writes —— 该任务将被 runner 跳过（不重跑）
    if not self.is_replaying and self.checkpoint_pending_writes:
        self._reapply_writes_to_succeeded_nodes()
        self._resume_error_handlers_if_applicable()

    # ⑦ interrupt_before 闸：命中则抛 GraphInterrupt 终止本次运行
    if self.interrupt_before_nodes and (
        to_interrupt := should_interrupt(self.checkpoint,
                                         self.interrupt_before_nodes,
                                         self.tasks)):
        self.status = "interrupt_before"
        raise GraphInterrupt([t.interrupts[0] for t in to_interrupt ...])

    # ⑧ 发 "tasks" 流事件
    self._emit("tasks", map_debug_tasks(self.tasks.values()))

    # ⑨ 已有写入的任务（缓存命中或重放恢复）立即发输出
    for tid, task in self.tasks.items():
        if task.writes:
            self.output_writes(tid, task.writes, cached=True)

    return True
```

返回 True 后，`main.stream()` 把 `loop.tasks` 交给 `runner.tick()` 并发执行 —— Execution 阶段。

### 4.5 after_tick()：Update 阶段（barrier 所在）

```python
# _loop.py — PregelLoop.after_tick()（节选 + 注释）
def after_tick(self) -> None:
    # 收集本步全部任务的写入
    writes = [t.writes for t in self.tasks.values() if t.writes]
    # ★ barrier：一次性原子应用到通道，得到 updated_channels
    self.updated_channels = self.apply_writes(
        self.checkpoint, self.channels, self.tasks.values(),
        self.get_next_version, self.trigger_to_nodes)
    # 输出通道被更新 -> 发 "values" 流事件（完整状态快照）
    if self.updated_channels.intersection(self.output_keys_set):
        self._emit("values", read_channels(self.channels, self.output_keys))
    # 本步写入已并入 checkpoint，清掉 pending
    self.checkpoint_pending_writes.clear()
    self.is_replaying = False
    # 落一个 source="loop" 的 checkpoint（内部 step += 1）
    self._put_checkpoint({"source": "loop", "writes": ...})
    # interrupt_after 闸
    if self.interrupt_after_nodes and (
        to_interrupt := should_interrupt(...)):
        self.status = "interrupt_after"
        raise GraphInterrupt(...)
    # 弹出 RESUMING 标志（只对第一步生效）
    self.config[CONF].pop(CONFIG_KEY_RESUMING, None)
```

**「步内不可见、步末统一提交」正是在这里实现的**：所有任务执行期间写入都堆在各自的 `task.writes` deque 里，`after_tick` 一次 `apply_writes` 全部生效。

### 4.6 put_writes()：任务级写入的持久化与发流

任务每写一批（`ChannelWrite.do_write` → `CONFIG_KEY_SEND` = `writes.extend`），runner 的 `commit` 就调用 `loop.put_writes(task_id, writes)`：

```python
# _loop.py — PregelLoop.put_writes（节选 + 注释）
def put_writes(self, task_id: str, writes: Sequence[tuple[str, Any]]) -> None:
    # 1) 特殊通道（ERROR/INTERRUPT/RESUME/RETURN...见 WRITES_IDX_MAP）
    #    按通道去重，last-write-wins；普通写入按 task_id 追加进 pending
    # 2) NULL_TASK_ID 的写入累积（Command 等“无主”写入），其余按 task_id 替换
    # 3) UntrackedValue 通道过滤（包括 Send.arg 里嵌的键，sanitize_untracked_values_in_send）
    # 4) DeltaChannel 的写入先 ensure_message_ids(v)：
    #    给 id=None 的消息补稳定 ID，防止「reducer 内赋 UUID」与
    #    「后台线程序列化」竞态导致重放时 UUID 漂移
    # 5) durability != "exit" 时，后台异步持久化：
    self.submit(checkpointer_put_writes, self.config, writes_to_save,
                task_id, [task_path], __reraise_on_exit__=False)
    # 6) 立即发流（updates/tasks 模式的事件在这里产生）
    self.output_writes(task_id, writes)
```

要点：**pending writes 的持久化粒度是「任务」而不是「超步」**。这是崩溃恢复的基础 —— 进程在第 N 步中途挂掉，重启后 `tick()` 的第⑥步会把已成功任务的写入灌回去，只重跑没写完的任务。

### 4.7 accept_push()：超步内动态任务（Send / call() 的快车道）

标准 BSP 里，Send 要等下一超步才执行。但函数式 API 的 `call()` 语义是「调用并等待结果」，等不起一步。`accept_push` 让 PUSH 任务**在当前超步内立即插入调度**：

```python
# _loop.py — PregelLoop.accept_push（节选）
def accept_push(self, task: PregelExecutableTask, write_idx: int,
                call: Call) -> PregelExecutableTask:
    # 用 (PUSH, 父任务path, write_idx, 父task.id, call) 构造确定性 task path
    next_task = prepare_single_task(
        (PUSH, task.path, write_idx, task.id, call),
        ..., checkpoint=self.checkpoint, ...)
    self._emit("tasks", map_debug_tasks([next_task]))
    self.tasks[next_task.id] = next_task
    # 非重放时把该任务已持久化的写入灌回（幂等）
    ...
    return next_task
```

`runner._call/_acall`（§6.6）就是拿它当 `schedule_task` 用的。

### 4.8 _suppress_interrupt()：顶层吞中断、产出 output

```python
# _loop.py — PregelLoop._suppress_interrupt（节选 + 注释）
def _suppress_interrupt(self, exc_type, exc_value, traceback) -> bool:
    # durability=="exit" 的场合（顶层无 checkpointer / 嵌套图带异常等）
    # 在退出前把 delta 写入 + checkpoint + pending writes 补落盘
    exiting = ...
    if exiting:
        self._put_exit_delta_writes(); self._put_checkpoint(self.checkpoint_metadata)
        self._put_pending_writes()

    if exc_type is GraphInterrupt and not self.is_nested:
        # ★ 顶层图：中断不是错误！
        #   1) 发最后一次 values + INTERRUPT updates 流事件
        #   2) self.output = read_channels(channels, output_keys)
        #   3) return True -> 抑制异常，stream/invoke 正常收尾
        return True
    elif exc_type is None:
        # 正常结束也要读 output
        self.output = read_channels(self.channels, self.output_keys)
    ...
```

`is_nested`（config 里有 `CONFIG_KEY_TASK_ID`）决定了**子图的 GraphInterrupt 会继续向上冒泡**（变成父任务的 interrupt 写入），只有最外层图才吞。这也是 MEMORY 中「HITL interrupt 被 top-level graph 抑制」条目的源码依据：**检测审批必须读 `state.tasks[].interrupts`，别指望 catch 到异常**。

### 4.9 _put_checkpoint 与写入顺序保证

```python
# _loop.py — _put_checkpoint（节选）
def _put_checkpoint(self, metadata: CheckpointMetadata) -> None:
    exiting = metadata is self.checkpoint_metadata   # 对象同一性判断“退出保存”
    # DeltaChannel 簿记（updates/supersteps 计数决定是否触发全量快照）
    ...
    checkpoint = create_checkpoint(self.checkpoint, self.channels, self.step, ...)
    do_checkpoint = self.checkpointer and (exiting or self.durability != "exit")
    if do_checkpoint:
        # ★ 串行化：等上一个 checkpoint future 完成再提交下一个，
        #   保证 checkpointer 收到的 (thread, step) 严格有序
        self._put_checkpoint_fut = self.submit(
            self._checkpointer_put_after_previous,
            getattr(self, "_put_checkpoint_fut", None),
            self.config, copy_checkpoint(checkpoint), metadata, new_versions)
    if not exiting:
        self.step += 1
```

`_checkpointer_put_after_previous(prev, ...)` 里先 drain delta 写入 futures、再 `prev.result()`、最后 `checkpointer.put(...)`，且用 try/finally 保证「链条不断」—— 即使某次 put 抛错，后续 checkpoint 仍会提交。

### 4.10 AsyncPregelLoop 的差异

`AsyncPregelLoop` 与 Sync 版结构完全对称，差异只有：

- `__aenter__` 用 `checkpointer.aget_tuple` / `achannels_from_checkpoint`；
- 执行器是 `AsyncBackgroundExecutor`（asyncio task + 信号量限流）；
- `put_writes` 用 `checkpointer.aput_writes`，checkpoint 用 `aput`；
- `__aexit__` 把退出栈包装成 `asyncio.create_task(stack.__aexit__(...))`；遇到 `CancelledError` 时执行 `e.args = (*e.args, exit_task)` —— **把「善后任务」塞进异常参数**，让上层（LangGraph Server）取消后仍有机会 await 落盘。这是异步取消安全的一个精妙手法。

---

## 五、_algo.py：调度算法

`_algo.py`（1460 行）是 Pregel 的**大脑**：它不执行任何节点，只负责「谁该跑」「跑什么输入」「跑完把写入放哪」。核心导出三类函数：

| 函数 | 行号 | 作用 | 调用方 |
|---|---|---|---|
| `prepare_next_tasks` | 392–513 | 规划一步：产出 `{task_id: task}`（PUSH + PULL 的并集） | `_loop.PregelLoop.tick()` |
| `prepare_single_task` | 524–761 | 按 task_path 前缀分派到三条子构造路径 | `prepare_next_tasks` |
| `apply_writes` | 232–345 | barrier：把一步的写入原子性并入 checkpoint/channels | `_loop.after_tick()` |
| `local_read` | 188–224 | 注入 `CONFIG_KEY_READ`，条件边/工具读「含本节点写入」的状态 | 任务 config |
| `should_interrupt` | 155–185 | `interrupt_before/after` 的判定 | `_loop` |
| `_triggers` | 1260–1277 | 版本比较：节点是否被触发 | `prepare_single_task` |
| `sanitize_untracked_values_in_send` | 1442–1460 | 把 `UntrackedValue` 通道的键从 `Send.arg` 里剔掉，避免污染 checkpoint | `_write` / `_loop` |

### 5.1 两个「协议/载体」结构

```python
# _algo.py:93-117 — WritesProtocol 与 PregelTaskWrites
class WritesProtocol(Protocol):
    """任何「携带写入、可被 apply_writes 消费」的对象都要满足的鸭子类型。"""
    path: tuple[str | int | tuple, ...]     # 任务路径（前 3 段参与排序）
    name: str                               # 节点名（决定 versions_seen 的表项）
    writes: Sequence[tuple[str, Any]]       # (channel, value) 写入序列
    triggers: Sequence[str]                 # 触发本任务的通道

class PregelTaskWrites(NamedTuple):
    """最简实现：给「不是来自 runnable 任务的写入」用
    （图输入、update_state、Send.arg 等）。"""
    path: tuple[str | int | tuple, ...]
    name: str
    writes: Sequence[tuple[str, Any]]
    triggers: Sequence[str]
```

`PregelExecutableTask`（不只是 `PregelTaskWrites`）也满足这个协议，所以 `apply_writes` 对「真任务」和「人造写入」一视同仁。

```python
# _algo.py:120-152 — Call：函数式 API 的「待办任务」描述
class Call:
    """一个 call() 调用被封装成 Call，写进 TASKS 通道，下一步展开成 PUSH 任务。"""
    __slots__ = ("func", "input", "retry_policy", "cache_policy", "callbacks", "timeout")
    func: Callable
    input: tuple[tuple[Any, ...], dict[str, Any]]   # (args, kwargs)
    retry_policy: Sequence[RetryPolicy] | None
    cache_policy: CachePolicy | None
    callbacks: Callbacks
    timeout: TimeoutPolicy | None
```

要点：**`Call` 是函数式 API（`@task` / `call()`）与图式 API 的接缝**。`call()` 里 `func, input` 就是用户函数与其参数，其余是透传的运行时选项（见 §8）。`prepare_single_task` 见到 `task_path[-1]` 是 `Call` 就切到 `prepare_push_task_functional`（§5.7）。

### 5.2 prepare_next_tasks：一步选出谁该跑

这是「Plan 阶段」的唯一实现。输入是当前 checkpoint/通道/节点表，输出是 `dict[task_id, task]`。它由两段组成：**先处理 PUSH（Send/Call），再处理 PULL（被边触发的节点）**。

```python
# _algo.py:437-513 — prepare_next_tasks（节选 + 中文注释）
def prepare_next_tasks(checkpoint, pending_writes, processes, channels, managed,
                       config, step, stop, *, for_execution, store=None,
                       checkpointer=None, manager=None,
                       trigger_to_nodes=None, updated_channels=None,
                       retry_policy=(), cache_policy=None):
    input_cache: dict[INPUT_CACHE_KEY_TYPE, Any] = {}
    checkpoint_id_bytes = binascii.unhexlify(checkpoint["id"].replace("-", ""))
    null_version = checkpoint_null_version(checkpoint)
    tasks: list[PregelTask | PregelExecutableTask] = []

    # ① PUSH 段：TASKS 通道里排队着上一超步产生的 Send 对象
    tasks_channel = cast(Topic[Send] | None, channels.get(TASKS))
    if tasks_channel and tasks_channel.is_available():
        for idx, _ in enumerate(tasks_channel.get()):
            # 逐条展开：(PUSH, idx) -> 一个 PUSH 任务
            if task := prepare_single_task((PUSH, idx), None, ...):
                tasks.append(task)

    # ② 倒排索引加速：只在「上一步被更新的通道」的订阅者里找候选节点
    if updated_channels and trigger_to_nodes:
        triggered_nodes: set[str] = set()
        for channel in updated_channels:
            if node_ids := trigger_to_nodes.get(channel):
                triggered_nodes.update(node_ids)
        candidate_nodes: Iterable[str] = sorted(triggered_nodes)   # 排序保证确定性
    elif not checkpoint["channel_versions"]:
        candidate_nodes = ()          # 空图/首步无版本 -> 没有 PULL 候选
    else:
        candidate_nodes = processes.keys()   # 退化路径：全量扫描

    # ③ PULL 段：对每个候选节点 prepare_single_task，返回非 None 的才进 tasks
    for name in candidate_nodes:
        if task := prepare_single_task((PULL, name), None, ...):
            tasks.append(task)
    return {t.id: t for t in tasks}
```

三条关键设计：

1. **加速不是「正确性」，只是「省扫描」**。`updated_channels` + `trigger_to_nodes` 只是把「候选节点」从「全部节点」缩小为「订阅了上一步被更新通道的节点」。真正的触发判定仍在 `prepare_single_task` 里用版本比较完成（§5.4）。所以即便倒排索引给多了候选，也不会多触发；给少了才会漏——而它不会给少，因为 `trigger_to_nodes` 由 `main._trigger_to_nodes()` 从 `PregelNode.triggers` 静态构造。
2. **PUSH 先于 PULL**：Send 任务与边触发是被合并进同一步的。`Task` 的解包顺序也决定了 `apply_writes` 里的确定性排序（§5.5）。
3. **`input_cache`**：一次 `prepare_next_tasks` 内所有任务共享，命中 `proc.input_cache_key` 时直接浅拷贝输入，避免同一节点被多个 task 触发时重复读通道（`_proc_input`，§5.8）。

### 5.3 prepare_single_task：三条构造路径

```python
# _algo.py:548-761 — prepare_single_task（结构概览）
def prepare_single_task(task_path, task_id_checksum, *, ...):
    configurable = config.get(CONF, {})
    parent_ns = configurable.get(CONFIG_KEY_CHECKPOINT_NS, "")
    # 1.x 之前用 uuid5，1.x 起用 xxhash128（更快，且仍是内容寻址）
    task_id_func = _xxhash_str if checkpoint["v"] > 1 else _uuid5_str

    if task_path[0] == PUSH and isinstance(task_path[-1], Call):
        return prepare_push_task_functional(...)      # ⑤.7 函数式
    elif task_path[0] == PUSH:
        return prepare_push_task_send(...)            # ⑤.6 Send
    elif task_path[0] == PULL:
        name = task_path[1]                           # (PULL, 节点名)
        # …… PULL 路径（见下）
```

**PULL 路径的核心判定（`_algo.py:597-761`）**：

```python
# _algo.py:599-652 — PULL 任务：先判触发，再取输入
name = cast(str, task_path[1])
if name not in processes:
    return                                   # 未知节点：静默跳过
proc = processes[name]
if checkpoint_null_version is None:
    return                                   # 还没有任何版本 -> 首步不触发
# ★ 版本驱动触发：任一订阅通道版本的「新度」超过节点上次所见
if _triggers(channels, checkpoint["channel_versions"],
             checkpoint["versions_seen"].get(name), checkpoint_null_version, proc):
    triggers = tuple(sorted(proc.triggers))
    checkpoint_ns = f"{parent_ns}{NS_SEP}{name}" if parent_ns else name
    task_id = task_id_func(checkpoint_id_bytes, checkpoint_ns,
                           str(step), name, PULL, *triggers)
    task_checkpoint_ns = f"{checkpoint_ns}{NS_END}{task_id}"
    scratchpad = _scratchpad(config[CONF].get(CONFIG_KEY_SCRATCHPAD),
                             pending_writes, task_id,
                             xxh3_128_hexdigest(task_checkpoint_ns.encode()),
                             config[CONF].get(CONFIG_KEY_RESUME_MAP), step, stop)
    try:
        val = _proc_input(proc, managed, channels, for_execution=for_execution,
                          input_cache=input_cache, scratchpad=scratchpad)
        if val is MISSING:
            return                           # 输入不可用（通道空）-> 不跑
    except Exception as exc:
        if SUPPORTS_EXC_NOTES:
            exc.add_note(f"Before task with name '{name}' and path '{task_path[:3]}'")
        raise
```

随后组装 `metadata`（`langgraph_step/node/triggers/path/checkpoint_ns`），再分叉：

- `for_execution=False`（**只规划、不执行**，用于 `get_state()` 的 next/tasks 展示）：返回轻量 `PregelTask(task_id, name, task_path[:3])`。
- `for_execution=True`：构造 `PregelExecutableTask`，把 `CONFIG_KEY_TASK_ID`、`CONFIG_KEY_SEND`（= `writes.extend`，**deque.extend 线程安全**）、`CONFIG_KEY_READ`（= `partial(local_read, ...)`）、`CONFIG_KEY_CHECKPOINTER`、`CONFIG_KEY_CHECKPOINT_MAP`、`CONFIG_KEY_CHECKPOINT_NS`、`CONFIG_KEY_SCRATCHPAD`、`CONFIG_KEY_RUNTIME` 全部塞进 `patch_config` 的 configurable。

**`for_execution` 的两副面孔**是一处重要设计：`get_state()`、`aget_state()` 需要知道「下一步谁会跑、跑号是多少」，但绝不能真的构造可执行任务（会重复触发副作用、且 `manager`/`checkpointer` 可能为空）。所以同一套规划代码用参数开关走两条分支。

### 5.4 版本驱动触发与 task_id 的内容哈希

`_triggers` 是 §1.3 所述「版本驱动」的落地实现：

```python
# _algo.py:1260-1277 — _triggers
def _triggers(channels, versions, seen, null_version, proc) -> bool:
    if seen is None:
        # 该节点从未被触发过：只要任一订阅通道 available 就触发
        for chan in proc.triggers:
            if channels[chan].is_available():
                return True
    else:
        for chan in proc.triggers:
            # ★ 严格大于：通道版本 > 节点所见版本
            if channels[chan].is_available() and \
               versions.get(chan, null_version) > seen.get(chan, null_version):
                return True
    return False
```

两条不变量：

- **严格大于（`>`）而非大于等于**：这正是「环」能工作而不会自激的原因。节点写入通道后 `versions_seen` 被更新到当前版本，下一轮同一版本不会再次触发；只有更高版本才会。
- **`is_available()` 前置**：通道为空（如 `EphemeralValue` 被读后清空）时即便版本更高也不触发——避免「空输入任务」。

**task_id 是内容寻址的**（1.1 的「确定性重放」依据）：

```python
# _algo.py:1395-1409 — 两种确定性 ID 生成器
def _uuid5_str(namespace: bytes, *parts: str | bytes) -> str:
    """SHA-1 版（checkpoint v1 兼容）。"""
    sha = sha1(namespace, usedforsecurity=False)
    sha.update(b"".join(p.encode() if isinstance(p, str) else p for p in parts))
    hex = sha.hexdigest()
    return f"{hex[:8]}-{hex[8:12]}-{hex[12:16]}-{hex[16:20]}-{hex[20:32]}"

def _xxhash_str(namespace: bytes, *parts: str | bytes) -> str:
    """XXH3-128 版（checkpoint v>1，更快）。"""
    hex = xxh3_128_hexdigest(namespace + b"".join(
        p.encode() if isinstance(p, str) else p for p in parts))
    return f"{hex[:8]}-{hex[8:12]}-{hex[12:16]}-{hex[16:20]}-{hex[20:32]}"
```

PULL 任务的 ID = `hash(checkpoint_id_bytes, checkpoint_ns, step, name, PULL, *triggers)`；PUSH 任务的 ID = `hash(checkpoint_id_bytes, checkpoint_ns, step, node, PUSH, idx)`。**同样的 (checkpoint, step, 触发集) 永远得到同一个 ID**——这是「重放幂等」「崩溃恢复只补差量」「pending_writes 按 task_id 归并」三件事的共同地基。`task_id_checksum` 参数（`_algo.py:661-662`）就是恢复时的自检：拿到已持久化的任务 ID 时断言重新计算出的 ID 与之一致。

### 5.5 apply_writes：barrier 汇聚（Update 阶段）

超步末，所有任务的写入一次性「原子」并入通道与 checkpoint。这是 BSP 的同步点，也是整个引擎里最需要小心的函数之一。

```python
# _algo.py:232-345 — apply_writes（完整结构 + 中文注释）
def apply_writes(checkpoint, channels, tasks, get_next_version, trigger_to_nodes) -> set[str]:
    # ① 确定性排序：按 task_path 前 3 段排（第 3 段后的 task_id 不参与，
    #    因为 ID 是哈希、用于排序不稳定）
    tasks = sorted(tasks, key=lambda t: task_path_str(t.path[:3]))
    # 只要有一个任务带 triggers，就是一个「真超步」，需要步进
    bump_step = any(t.triggers for t in tasks)

    # ② 记录 versions_seen：把每个任务订阅通道的「当前版本」记为该节点「已见」
    for task in tasks:
        checkpoint["versions_seen"].setdefault(task.name, {}).update(
            {chan: checkpoint["channel_versions"][chan]
             for chan in task.triggers if chan in checkpoint["channel_versions"]})

    # ③ 计算这一步的统一新版本号（所有本步被写的通道共享它）
    if get_next_version is None:
        next_version = None
    else:
        next_version = get_next_version(
            max(checkpoint["channel_versions"].values())
            if checkpoint["channel_versions"] else None, None)

    # ④ consume：被读取的通道可能清空（EphemeralValue 语义），若清空则版本推高
    for chan in {chan for task in tasks for chan in task.triggers
                 if chan not in RESERVED and chan in channels}:
        if channels[chan].consume() and next_version is not None:
            checkpoint["channel_versions"][chan] = next_version

    # ⑤ 按通道分组写入（跳过保留通道与未知通道）
    pending_writes_by_channel: dict[str, list[Any]] = defaultdict(list)
    for task in tasks:
        for chan, val in task.writes:
            if chan in (NO_WRITES, PUSH, RESUME, INTERRUPT, RETURN, ERROR,
                        ERROR_SOURCE_NODE):
                pass                                   # 控制类写入不进通道
            elif chan in channels:
                pending_writes_by_channel[chan].append(val)
            else:
                logger.warning(f"Task {task.name} with path {task.path} wrote to "
                               f"unknown channel {chan}, ignoring it.")

    # ⑥ 落写入：channel.update(vals) 返回 True 表示「真的变了」才推版本
    updated_channels: set[str] = set()
    for chan, vals in pending_writes_by_channel.items():
        if chan in channels:
            if channels[chan].update(vals) and next_version is not None:
                checkpoint["channel_versions"][chan] = next_version
                if channels[chan].is_available():       # 不可用通道不触发
                    updated_channels.add(chan)

    # ⑦ 「未更新」的通道也要被通知「进入了新一步」（barrier 语义）
    if bump_step:
        for chan in channels:
            if channels[chan].is_available() and chan not in updated_channels:
                if channels[chan].update(EMPTY_SEQ) and next_version is not None:
                    checkpoint["channel_versions"][chan] = next_version
                    if channels[chan].is_available():
                        updated_channels.add(chan)

    # ⑧ 疑似最后一步：通知所有通道 finish（有些通道在 finish 时 flush）
    if bump_step and updated_channels.isdisjoint(trigger_to_nodes):
        for chan in channels:
            if channels[chan].finish() and next_version is not None:
                checkpoint["channel_versions"][chan] = next_version
                if channels[chan].is_available():
                    updated_channels.add(chan)

    return updated_channels       # 返回给 loop，作为下一步的 updated_channels 加速输入
```

要点解读：

| 步骤 | 为什么必须这样做 |
|---|---|
| ① 排序 | 同一超步内多个任务写同一通道时，reducer 的合并顺序必须与「任务路径」绑定，否则重放结果不稳定 |
| ② versions_seen | 保证同一任务同一版本不被重复触发（环的安全阀） |
| ③ 共享 next_version | 本步所有被写通道拿到**同一个**版本号，下一步 `_triggers` 用 `>` 一次即可判定「本步发生过」 |
| ④ consume | `EphemeralValue`/`Topic` 的「读后清空」在此发生；清空也算一次版本变更，从而能触发下游 |
| ⑤ 跳过控制通道 | `RESUME/INTERRUPT/ERROR/RETURN/PUSH/NO_WRITES` 是「元数据写入」，由 checkpointer 处理，不是状态通道 |
| ⑥ 只在真变化时推版本 | `update()` 返回 False 表示写入被 reducer 吸收但值没变，此时不推版本——避免无谓触发 |
| ⑦ bump_step 通知 | 「未被写入的通道」也要走一次 `update(EMPTY_SEQ)`，让它们感知新一步（例如 `LastValue` 会借此更新内部缓存） |
| ⑧ finish | `updated_channels.isdisjoint(trigger_to_nodes)` 说明「本步更新的通道没有一个能再触发节点」⇒ 大概率是最后一步，让通道做收尾 |

### 5.6 Send 分发：TASKS Topic → PUSH 任务

`Send` 是「动态 fan-out」的载体。它不直接被执行，而是被**写进保留通道 `TASKS`**（一个 `Topic(Send, accumulate=False)`，见 §2.2），下一步由 `prepare_next_tasks` 的 PUSH 段展开。

```python
# _algo.py:938-1007 — prepare_push_task_send（节选）
def prepare_push_task_send(task_path, task_id_checksum, *, ...):
    if len(task_path) == 2:
        # (PUSH, idx of pending send)
        idx = cast(int, task_path[1])
        if not channels[TASKS].is_available():
            return
        sends: Sequence[Send] = channels[TASKS].get()
        if idx < 0 or idx >= len(sends):
            return
        packet = sends[idx]
        if not isinstance(packet, Send):
            logger.warning(f"Ignoring invalid packet type {type(packet)} in pending sends")
            return
        if packet.node not in processes:
            logger.warning(f"Ignoring unknown node name {packet.node} in pending sends")
            return
        proc = processes[packet.node]           # Send.node 指向的目标节点
        triggers = PUSH_TRIGGER                  # ("__publish__",) —— 动态任务无「边触发」
        checkpoint_ns = f"{parent_ns}{NS_SEP}{packet.node}" if parent_ns else packet.node
        task_id = task_id_func(checkpoint_id_bytes, checkpoint_ns,
                               str(step), packet.node, PUSH, str(idx))
```

与 PULL 的差异：**输入是 `packet.arg` 而不是从通道读**，`triggers` 固定为 `PUSH_TRIGGER`，且任务的 `timeout` 由 `packet.timeout`（Send 自带）覆盖节点默认。注意 `translated_task_path = (*task_path[:3], False)`——**尾部追加 `False` 表示「这不是 call() 任务，Interrupt 要正常返回」**（对应函数式 Call 追加 `True`，见 §5.7）。

`Send.arg` 里的 `UntrackedValue` 键会被 `sanitize_untracked_values_in_send`（`_algo.py:1442-1460`）剔除：

```python
# _algo.py:1442-1460 — sanitize_untracked_values_in_send
def sanitize_untracked_values_in_send(packet: Send, channels) -> Send:
    """Send 常被用来把「要传给目标节点的状态」一起打包，arg 的顶层键即通道名。
    若某个键对应 UntrackedValue 通道，它的值不应被检查点化，这里先剔掉。"""
    if not isinstance(packet.arg, dict):
        return packet                    # Command 形态，跳过
    sanitized_arg = {k: v for k, v in packet.arg.items()
                     if not isinstance(channels.get(k), UntrackedValue)}
    return Send(node=packet.node, arg=sanitized_arg, timeout=packet.timeout)
```

### 5.7 函数式 PUSH：prepare_push_task_functional

`@task`/`call()` 的任务走这条路，与 Send 的区别是它**携带一个 `Call`**（而非 `Send`），且路径 4 元组更长：

```python
# _algo.py:800-935 — prepare_push_task_functional（节选）
def prepare_push_task_functional(task_path, task_id_checksum, *, ...):
    # (PUSH, 父任务path, PUSH写入的idx, 父任务id, Call)
    call = task_path[-1]
    proc_ = get_runnable_for_task(call.func)     # 见 §8.3：加 ChannelWrite(RETURN)
    name = proc_.name
    if name is None:
        raise ValueError("`call` functions must have a `__name__` attribute")
    triggers: Sequence[str] = PUSH_TRIGGER
    task_id = task_id_func(checkpoint_id_bytes, checkpoint_ns, str(step),
                           name, PUSH, task_path_str(task_path[1]), str(task_path[2]))
    # ★ 路径尾追加 True：表示「正在 call」，该任务的 Interrupt 不由自己返回，
    #   而由父任务负责（见 _runner 的 _call 链）
    in_progress_task_path = (*task_path[:3], True)
    ...
    return PregelExecutableTask(name, call.input, proc_, writes, <config>,
                                triggers, call.retry_policy or retry_policy,
                                cache_key, task_id, in_progress_task_path,
                                timeout=call.timeout)
```

**`writes.deque` 与 `CONFIG_KEY_SEND = writes.extend`**：这是「任务内写入不即时生效」的物理实现——写入只是追加到该任务私有的 deque，直到 `runner.commit` 才让 `loop.put_writes` 看到（§6.5）。`deque.extend` 是原子操作，因此多个线程/协程写同一 deque 无需锁。

### 5.8 local_read 与 _proc_input

**`local_read`（`_algo.py:188-224`）**：条件边（`add_conditional_edges`）和工具需要在「看见本节点刚写的值」的基础上读状态——BSP 规定步内写入不可见，但同一节点自己的写入应当可见：

```python
# _algo.py:188-224 — local_read（节选）
def local_read(scratchpad, channels, managed, task, select, fresh=False):
    updated: dict[str, list[Any]] = defaultdict(list)
    if isinstance(select, str):
        for c, v in task.writes:
            if c == select:
                updated[c].append(v)
    else:
        managed_keys = [k for k in select if k in managed]
        select = [k for k in select if k not in managed]
        for c, v in task.writes:
            if c in select:
                updated[c].append(v)
    if fresh:
        # 把本任务的写入叠加到通道副本上再读（fresh=True）
        local_channels: dict[str, BaseChannel] = {}
        for k in channels:
            cc = channels[k].copy()
            cc.update(updated[k])
            local_channels[k] = cc
        values = read_channels(local_channels, select)
    else:
        values = read_channels(channels, select)     # 否则读「步初快照」
    if managed_keys:
        values.update({k: managed[k].get(scratchpad) for k in managed_keys})
    return values
```

**`_proc_input`（`_algo.py:1348-1392`）**：决定 PULL 任务的输入形态。

```python
# _algo.py:1358-1392 — _proc_input（节选）
if input_cache is not None and proc.input_cache_key in input_cache:
    return copy(input_cache[proc.input_cache_key])          # 命中缓存：浅拷贝
if isinstance(proc.channels, list):
    val: dict[str, Any] = {}
    for chan in proc.channels:
        if chan in channels:
            if channels[chan].is_available():
                val[chan] = channels[chan].get()            # dict 输入
        else:
            val[chan] = managed[chan].get(scratchpad)       # managed value
elif isinstance(proc.channels, str):
    if proc.channels in channels:
        if channels[proc.channels].is_available():
            val = channels[proc.channels].get()             # 单值输入
        else:
            return MISSING                                  # 空 -> 不跑
    else:
        return MISSING
if for_execution and proc.mapper is not None:
    val = proc.mapper(val)                                  # 输入映射器
if input_cache is not None:
    input_cache[proc.input_cache_key] = val
return val
```

这解释了 `StateGraph` 里两种节点签名的差异：`subscribe_only(channel)` → 单通道 → 节点收到**值**；`subscribe_to(*channels)` → 多通道 → 节点收到**字典**。`MISSING` 是「通道为空」的哨兵，让 `prepare_single_task` 直接跳过该任务。

### 5.9 scratchpad：resume 值与计数器

```python
# _algo.py:1280-1345 — _scratchpad（节选）
def _scratchpad(parent_scratchpad, pending_writes, task_id, namespace_hash,
                resume_map, step, stop) -> PregelScratchpad:
    if len(pending_writes) > 0:
        # 全局 resume 值（NULL_TASK_ID 上的 RESUME 写入）
        for w in pending_writes:
            if w[0] == NULL_TASK_ID and w[1] == RESUME:
                null_resume_write = w; break
        else:
            null_resume_write = None
        # 任务级 resume 值（该 task_id 上的 RESUME 写入，可能多条 -> 列表）
        for w in pending_writes:
            if w[0] == task_id and w[1] == RESUME:
                task_resume_write = w[2]
                if not isinstance(task_resume_write, list):
                    task_resume_write = [task_resume_write]
                break
        else:
            task_resume_write = []
        # resume_map：按 namespace hash 映射的 resume 值（HTTP 场景）
        if resume_map and namespace_hash in resume_map:
            task_resume_write.append(resume_map[namespace_hash])

    def get_null_resume(consume: bool = False) -> Any:
        if null_resume_write is None:
            if parent_scratchpad is not None:
                return parent_scratchpad.get_null_resume(consume)   # 向上冒泡
            return None
        if consume:
            try:                                  # consume=True 时「取走」该写入
                pending_writes.remove(null_resume_write); return null_resume_write[2]
            except ValueError:
                return None
        return null_resume_write[2]

    return PregelScratchpad(
        step=step, stop=stop,
        call_counter=LazyAtomicCounter(),          # 函数式 call 的序号（原子）
        interrupt_counter=LazyAtomicCounter(),     # interrupt 的序号（原子）
        resume=task_resume_write,
        get_null_resume=get_null_resume,
        subgraph_counter=LazyAtomicCounter(),
    )
```

`LazyAtomicCounter`（`_algo.py:1423-1439`）用「锁 + 惰性初始化 `itertools.count().__next__`」实现线程安全的原子计数——**`+= 1` 不是线程安全的**，所以中断/调用序号必须走它。`scratchpad` 是 interrupt/resume 机制的核心状态（`interrupt()` 写入 counter，`Command(resume=...)` 按 counter 对齐恢复值）。

### 5.10 节点级错误处理器任务

LangGraph 1.2.9 新增了「节点级错误处理器」（`add_node(..., error_handler=...)`）。当节点失败且配置了处理器时，`prepare_node_error_handler_task`（`_algo.py:1110-1248`）就地构造一个 PUSH 任务：

```python
# _algo.py:1144-1160 — prepare_node_error_handler_task（节选）
task_id = task_id_func(checkpoint_id_bytes, checkpoint_ns, str(step),
                       handler_node_name, PUSH, "node_error_handler", failed_task.id)
translated_task_path = (*failed_task.path[:3], "node_error_handler", False)
metadata = {"langgraph_step": step, "langgraph_node": handler_node_name,
            "langgraph_triggers": PUSH_TRIGGER, "langgraph_path": translated_task_path,
            "langgraph_checkpoint_ns": task_checkpoint_ns}
```

并在 configurable 里注入失败上下文：

```python
# _algo.py:1236-1238 — 注入 NodeError
CONFIG_KEY_NODE_ERROR: NodeError(node=failed_task.name, error=failed_error),
```

路由决策在 `_runner.PregelRunner._should_route_to_error_handler`（§6.8）：异常被「标记为已处理」后不会 panic 整个 run，而是执行处理器任务。处理器函数可声明 `def handler(state, error: NodeError)` 拿到失败节点名与异常对象（`errors.py:148-165`）。

### 5.11 task_path 的确定性字符串化

```python
# _algo.py:1412-1420 — task_path_str
def task_path_str(tup: str | int | tuple) -> str:
    """把嵌套路径展平成可比较字符串：整数补零对齐到 10 位，
    嵌套元组递归加 '~' 前缀，其余 str()。"""
    return (f"~{', '.join(task_path_str(x) for x in tup)}" if isinstance(tup, (tuple, list))
            else f"{tup:010d}" if isinstance(tup, int) else str(tup))
```

补零到 10 位是关键：`"0000000002" < "0000000010"` 的字典序才等于数值序。`apply_writes` 的任务排序与 `prepare_push_task_functional` 的 task_id 都依赖它，从而保证「嵌套子图 + 并行 Send」场景下写入应用顺序可复现。

---

---

## 六、_runner.py：节点运行器

`_runner.py`（941 行）实现「Execution 阶段」：把一个超步的 `PregelExecutableTask` 集合**并发**执行、收集写入、遇到失败/中断时正确收尾。它是一个**生成器**（`tick`/`atick` 里有多个 `yield`），每个 `yield` 都是「把控制权交回调用方去吐流事件」的时机。

| 成员 | 行号 | 作用 |
|---|---|---|
| `PregelRunner` | 135–613 | 运行器本体 |
| `PregelRunner.tick` | 176–358 | 同步执行一个超步 |
| `PregelRunner.atick` | 360–572 | 异步执行一个超步 |
| `PregelRunner.commit` | 574–613 | 任务结束回调：写入落盘 + 发流 |
| `FuturesDict` | 75–132 | future→task 映射 + 完成计数 + 唤醒事件 |
| `_call` / `_acall` / `_acall_impl` | 700–941 | 函数式 `call()` 的任务调度 |
| `_should_stop_others` | 616–634 | 「有任务失败就停别的」判定 |
| `_panic_or_proceed` | 650–697 | 超步末统一抛错/抛出中断/超时 |
| `_exception` | 637–647 | 读 future 异常但不抛 `CancelledError` |

### 6.1 PregelRunner 的依赖注入

```python
# _runner.py:140-165 — PregelRunner.__init__（节选）
def __init__(self, *, submit: weakref.ref[Submit],
             put_writes: weakref.ref[Callable[[str, Sequence[tuple[str, Any]]], None]],
             use_astream: bool = False,
             node_finished: Callable[[str], None] | None = None,
             node_error_handler_map: Mapping[str, str] | None = None,
             schedule_error_handler=None, aschedule_error_handler=None):
    self.submit = submit        # weakref 到 _executor 的 submit（线程池/事件循环）
    self.put_writes = put_writes  # weakref 到 loop.put_writes
    self.use_astream = use_astream
    ...
    self.error_handler_nodes = set(self.node_error_handler_map.values())
    self._handled_exception_ids: set[int] = set()   # 已路由给错误处理器的异常 id
```

两个 weakref 是刻意的：**运行器不持有 loop 与 executor 的强引用**，避免循环引用与「任务跑完 executor 仍不释放」的问题。所有依赖在 `tick` 调用期间通过 `self.submit()` / `self.put_writes()` 解引用。

### 6.2 FuturesDict：永不变的完成计数

```python
# _runner.py:75-132 — FuturesDict（节选 + 注释）
class FuturesDict(Generic[F, E], dict[F, PregelExecutableTask | None]):
    """future -> task 的映射。value 为 None 表示「等待者任务」（waiter）。"""
    event: E                 # threading.Event 或 asyncio.Event
    should_stop: Callable[[set[F]], bool]
    counter: int             # 尚未完成的被跟踪 future 数
    done: set[F]
    lock: threading.Lock

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if value is not None:
            with self.lock:
                self.event.clear()     # 有新任务 -> 清掉「全完成」信号
                self.counter += 1
            key.add_done_callback(partial(self.on_done, value))

    def on_done(self, task, fut):
        try:
            if cb := self.callback():
                cb(task, _exception(fut))       # = runner.commit
        finally:
            with self.lock:
                self.done.add(fut)
                self.counter -= 1
                # 所有被跟踪 future 完成，或命中 runner 级 stop 条件 -> 唤醒
                if self.counter == 0 or self.should_stop(self.done):
                    self.event.set()
```

要点：**等待「全部完成」不用轮询，而用 `event.wait()`**。每个 future 完成时 `counter -= 1`，归零或触发 stop 条件就 `event.set()`。`VALUE=TASK` 的那一项是 `get_waiter()`（`stream_eager` 模式下用于「有输出就唤醒」的哨兵）。`done` 集合用于 `_panic_or_proceed` 判定。

### 6.3 tick()：同步超步主流程

```python
# _runner.py:176-358 — tick（结构 + 中文注释）
def tick(self, tasks, *, reraise=True, timeout=None, retry_policy=None,
         get_waiter=None, schedule_task) -> Iterator[None]:
    tasks = tuple(tasks)
    futures = FuturesDict(callback=weakref.WeakMethod(self.commit),
                          event=threading.Event(),
                          should_stop=partial(_should_stop_others, ...),
                          future_type=concurrent.futures.Future)
    yield                                    # ① 先把控制权交回 loop（吐流事件）

    # ② 快路径：单任务且无 timeout/waiter -> 就在当前线程直接跑
    if len(tasks) == 0:
        return
    elif len(tasks) == 1 and timeout is None and get_waiter is None:
        t = tasks[0]
        try:
            run_with_retry(t, retry_policy, configurable={
                CONFIG_KEY_CALL: partial(_call, weakref.ref(t), ...)})
            self.commit(t, None)
        except Exception as exc:
            self.commit(t, exc)
            # …… 错误处理器路由 / reraise（见 §6.8）
        ...
    # ③ 一般路径：提交到线程池，登记 future
    if get_waiter is not None:
        futures[get_waiter()] = None         # 放一个 waiter 哨兵
    for t in tasks:
        fut = self.submit()(run_with_retry, t, retry_policy,
                            configurable={CONFIG_KEY_CALL: partial(_call, weakref.ref(t), ...)},
                            __reraise_on_exit__=reraise)
        futures[fut] = t

    # ④ 主等待循环：FIRST_COMPLETED 逐批收割，每批后 yield 给 caller 吐流
    end_time = timeout + time.monotonic() if timeout else None
    while len(futures) > (1 if get_waiter is not None else 0):
        done, inflight = concurrent.futures.wait(
            futures, return_when=concurrent.futures.FIRST_COMPLETED,
            timeout=(max(0, end_time - time.monotonic()) if end_time else None))
        if not done:
            break                            # 超时
        done_for_stop = set()
        for fut in done:
            task = futures.pop(fut)
            if task is None:                 # waiter 完成 -> 补一个新的 waiter
                if inflight and get_waiter is not None:
                    futures[get_waiter()] = None
            elif (task_exc := _exception(fut)) and self._should_route_to_error_handler(task) ...:
                ...                          # 错误处理器路由（§6.8）
            else:
                done_for_stop.add(fut)
        if _should_stop_others(done_for_stop, ...):
            break                            # 有任务失败 -> 停止等其余
        yield                                # ★ 交回控制权：loop 用 _output 吐事件

    # ⑤ 等 done 回调全部跑完（确保 commit 已落盘）
    futures.event.wait(timeout=(max(0, end_time - time.monotonic()) if end_time else None))
    yield                                    # ⑥ 再交回一次（吐最后一批）
    # ⑦ 收尾：抛错 / 抛中断 / 超时
    _panic_or_proceed(futures.done.union(f for f, t in futures.items() if t is not None),
                      panic=reraise, handled_exception_ids=self._handled_exception_ids,
                      handled_futures=handled_futures)
```

**生成器的三个 `yield` 位置极其关键**，它们决定了流事件的时序：

1. 第一个 `yield`（提交任务**之前**）：让 loop 先把「本步有哪些任务」的 debug 事件吐出去；
2. 循环内的 `yield`（每批任务完成**之后**）：让 loop 吐出这批任务的 `updates/messages` 流；
3. 循环后 `yield`：等所有 `done` 回调（`commit` → `put_writes` → `output_writes`）跑完再吐最后一批，保证「流事件先于下一超步」。

这正是「`stream()` 能在节点完成的瞬间把 token/更新推给用户」的机制：**运行器不直接输出，它把控制权按批交回 loop，由 loop 统一 `_output`**。

### 6.4 atick()：异步超步的差异

`atick`（`_runner.py:360-572`）与 `tick` 结构镜像，差异集中在：

| 维度 | `tick`（同步） | `atick`（异步） |
|---|---|---|
| 事件 | `threading.Event()` | `asyncio.Event()` |
| future 类型 | `concurrent.futures.Future` | `asyncio.Future` |
| 提交 | `self.submit()(run_with_retry, ...)` | `self.submit()(arun_with_retry, t, ..., stream=self.use_astream, __cancel_on_exit__=True, ...)` |
| 等待 | `concurrent.futures.wait(..., FIRST_COMPLETED)` | `await asyncio.wait(..., FIRST_COMPLETED)` |
| 事件等待 | `futures.event.wait(timeout=...)` | `await asyncio.wait_for(futures.event.wait(), timeout=...)` |
| 超时异常 | `TimeoutError` | `asyncio.TimeoutError`（`_panic_or_proceed(timeout_exc_cls=asyncio.TimeoutError)`） |
| waiter 取消 | 无 | 循环后 `for fut in futures: fut.cancel()` |
| 单任务快路径 | `run_with_retry` | `await arun_with_retry(..., stream=self.use_astream)` |

异步版本还多了一层「先 `asyncio.set_event_loop(loop)`」（`_runner.py:373-377`）——因为 `atick` 可能在「新建事件循环」的上下文中被调用（如 `ainvoke` 从同步代码桥接）。

### 6.5 commit()：写入落盘与错误记录

每个任务结束时（成功或失败）`FuturesDict` 都会回调 `runner.commit(task, exception)`：

```python
# _runner.py:574-613 — commit（完整 + 中文注释）
def commit(self, task: PregelExecutableTask, exception: BaseException | None) -> None:
    if isinstance(exception, asyncio.CancelledError):
        # 取消的任务：把强类型错误也写进 task.writes，让 loop 能结束本超步
        task.writes.append((ERROR, exception))
        self.put_writes()(task.id, task.writes)
    elif exception:
        if isinstance(exception, GraphInterrupt):
            # ★ 中断：写 INTERRUPT 通道（args[0] 是 Interrupt 序列），
            #   并把该任务已产生的 RESUME 写入一起带走
            if exception.args[0]:
                writes = [(INTERRUPT, exception.args[0])]
                if resumes := [w for w in task.writes if w[0] == RESUME]:
                    writes.extend(resumes)
                self.put_writes()(task.id, writes)
        elif isinstance(exception, GraphBubbleUp):
            # 其它 BubbleUp（ParentCommand/GraphDrained）：不写，交由 _panic_or_proceed 抛出
            pass
        else:
            # 普通错误：写 ERROR 通道；若该节点有错误处理器，再写 ERROR_SOURCE_NODE
            task.writes.append((ERROR, exception))
            if self._should_route_to_error_handler(task) and not isinstance(exception, GraphBubbleUp):
                task.writes.append((ERROR_SOURCE_NODE, task.name))
                self._handled_exception_ids.add(id(exception))
            self.put_writes()(task.id, task.writes)
    else:
        # 成功：先通知「节点完成」（供心跳/唤醒），无写入则补 NO_WRITES 标记
        if self.node_finished and (task.config is None or TAG_HIDDEN not in task.config.get("tags", [])):
            self.node_finished(task.name)
        if not task.writes:
            task.writes.append((NO_WRITES, None))
        self.put_writes()(task.id, task.writes)
```

三类写入语义（`NO_WRITES`/`INTERRUPT`/`ERROR`）都被 `apply_writes` 在分组时跳过（§5.5 步骤⑤），它们只进 checkpointer 的 pending writes，供 `get_state()`/恢复使用。`ERROR` 写入是节点失败的**持久化证据**：即使进程随后崩溃，重启后 `_scratchpad` 与 `check_progress` 仍能从 pending writes 读到失败。

### 6.6 _call / _acall：函数式 API 的任务调度

`_call`（`_runner.py:700-786`）是注入 `CONFIG_KEY_CALL` 的实现——用户在节点里写 `await call(func, *args)` 时最终到这里。它把 `Call` 交给 `schedule_task`（即 `loop.accept_push`，见 §4.7）**在当前超步内插入新任务**，再返回一个 chained future：

```python
# _runner.py:718-786 — _call（节选 + 注释）
def _call(task, func, input, *, retry_policy=None, cache_policy=None, timeout=None,
          callbacks=None, futures, schedule_task, submit) -> concurrent.futures.Future[Any]:
    if inspect.iscoroutinefunction(func):
        raise RuntimeError("In an sync context async tasks cannot be called")
    scratchpad: PregelScratchpad = task().config[CONF][CONFIG_KEY_SCRATCHPAD]
    if next_task := schedule_task(task(), scratchpad.call_counter(),
                                  Call(func, input, retry_policy=..., cache_policy=...,
                                       callbacks=..., timeout=...)):
        # 情况 A：父任务被重试，子任务可能已在跑 -> 复用其 future
        if fut := next((f for f, t in list(futures().items())
                        if t is not None and t == next_task.id), None):
            pass
        # 情况 B：子任务已执行过 -> 直接从它的 writes 里取 RETURN/ERROR
        elif next_task.writes:
            fut = concurrent.futures.Future()
            ret = next((v for c, v in next_task.writes if c == RETURN), MISSING)
            if ret is not MISSING:
                fut.set_result(ret)
            elif exc := next((v for c, v in next_task.writes if c == ERROR), None):
                fut.set_exception(exc if isinstance(exc, BaseException) else Exception(exc))
            else:
                fut.set_result(None)
        # 情况 C：首次调度 -> 提交到执行器
        else:
            fut = submit()(run_with_retry, next_task, retry_policy,
                           configurable={CONFIG_KEY_CALL: partial(_call, weakref.ref(next_task), ...)},
                           __reraise_on_exit__=False,
                           __next_tick__=True)     # ★ 下一 tick 启动：先让本 tick 的更新落定/发流
            SKIP_RERAISE_SET.add(fut)              # call 任务的异常由父任务承担，不在 tick 末尾重抛
            futures()[fut] = next_task
    return chain_future(fut, concurrent.futures.Future())   # ★ 链式 future：保证 commit 先于结果
```

三个细节值得记：

- **`__next_tick__=True`**：子任务在「下一个 tick 边界」启动，确保父任务所在 tick 的更新先 `commit`/发流。这对应 `_executor.next_tick`（先 `time.sleep(0)` 让出控制）。
- **`SKIP_RERAISE_SET`**（`_runner.py:70-72`）：一个全局 `WeakSet[Future]`。登记其中的 future，其异常**不会**在 tick 末尾被重抛——因为 `call()` 的异常应当「抛进父任务」由父任务处理。
- **`chain_future(fut, destination)`**：`_internal/_future.py` 的工具，把源 future 的结果/异常转发到目标 future，且保证 `commit` 回调先于结果可见（维持流顺序）。

异步版 `_acall`（`_runner.py:789-841`）+ `_acall_impl`（844-941）逻辑等价，差别是：用 `asyncio.Future`、`run_coroutine_threadsafe` 把协程投到事件循环、`__cancel_on_exit__=True`。`_acall` 会根据「当前是否在 async 上下文」决定返回 `asyncio.Future` 还是 `concurrent.futures.Future`——这是为了让同一个 `call()` 在 sync/async 两种调用栈下都能 `await`（或 `.result()`）。

### 6.7 _should_stop_others / _exception / _panic_or_proceed

```python
# _runner.py:616-634 — _should_stop_others
def _should_stop_others(done, *, handled_exception_ids=None) -> bool:
    """有任务失败就取消其余任务。GraphInterrupt 不算失败。
    已被错误处理器接管（handled_exception_ids）或登记在 SKIP_RERAISE_SET 的也不算。"""
    for fut in done:
        if fut.cancelled():
            continue
        elif exc := fut.exception():
            if (id(exc) not in (handled_exception_ids or set())
                    and not isinstance(exc, GraphBubbleUp)
                    and fut not in SKIP_RERAISE_SET):
                return True
    return False

# _runner.py:637-647 — _exception：读异常但不抛 CancelledError
def _exception(fut):
    if fut.cancelled():
        return asyncio.CancelledError() if isinstance(fut, asyncio.Future) \
               else concurrent.futures.CancelledError()
    return fut.exception()

# _runner.py:650-697 — _panic_or_proceed（节选 + 注释）
def _panic_or_proceed(futs, *, timeout_exc_cls=TimeoutError, panic=True,
                      handled_exception_ids=None, handled_futures=None):
    done, inflight = set(), set()
    for fut in futs:
        if fut.cancelled(): continue
        elif fut.done(): done.add(fut)
        else: inflight.add(fut)
    interrupts: list[GraphInterrupt] = []
    while done:
        fut = done.pop()
        if exc := _exception(fut):
            if fut in (handled_futures or set()): continue      # 已接管
            if id(exc) in (handled_exception_ids or set()): continue
            while inflight: inflight.pop().cancel()             # 取消所有未完成
            if panic:
                if isinstance(exc, GraphInterrupt):
                    interrupts.append(exc)                      # 中断：收集，最后合并抛
                elif fut not in SKIP_RERAISE_SET:
                    raise exc                                   # 真失败：立即抛
    if interrupts:
        # ★ 多个并行的 GraphInterrupt 合并成一个
        raise GraphInterrupt(tuple(i for exc in interrupts for i in exc.args[0]))
    if inflight:
        while inflight: inflight.pop().cancel()
        raise timeout_exc_cls("Timed out")                      # 走到这 = 超时
```

三条收尾规则：
1. **失败即取消兄弟**：一个任务真失败，立刻 `cancel()` 其余未完成 future（对应 MEMORY 中「一个节点失败会掐断同超步其它节点」的行为）；
2. **中断合并**：同一步多个节点各自 `interrupt()` 时，合成一个含全部 `Interrupt` 的 `GraphInterrupt` 抛出；
3. **超时**：`inflight` 非空说明 `async wait`/`futures.wait` 超时返回，取消剩余并抛 `TimeoutError`。

### 6.8 节点级错误处理器路由

`_should_route_to_error_handler`（`_runner.py:171-174`）判定：「该任务名在 `node_error_handler_map` 里，且它本身不是处理器节点」。路由发生时：

- 记录 `self._handled_exception_ids.add(id(exc))`（用 `id()` 而非异常对象，避免 hash/比较开销）；
- `SKIP_RERAISE_SET.add(fut)`：让这个失败 future 不在 tick 末尾重抛；
- 调 `schedule_error_handler(task, exc)` 拿到处理器任务并提交执行。

这解释了为什么「配了 `error_handler` 的节点失败后，整张图不会崩」——异常被「标记为已处理」，`_should_stop_others` 与 `_panic_or_proceed` 都会跳过它。这也与 MEMORY 中「子任务假成功：失败信号机读化」条目呼应：**失败信号必须机读（`ERROR` 通道 / `NodeError` 注入），不能只靠异常**。

### 6.9 traceback 裁剪与 EXCLUDED_FRAME_FNAMES

```python
# _runner.py:58-68 — 需要从 traceback 里剔除的框架帧
EXCLUDED_FRAME_FNAMES = (
    "langgraph/pregel/retry.py", "langgraph/pregel/runner.py",
    "langgraph/pregel/executor.py", "langgraph/utils/runnable.py",
    "langchain_core/runnables/config.py",
    "concurrent/futures/thread.py", "concurrent/futures/_base.py",
)
```

`tick`/`atick` 抛错前会「从 traceback 头部不断剥掉框架帧」（`_runner.py:350-358`、`564-572`），只留下**用户节点的帧**——这样用户看到的堆栈是 `你们的 node 函数` 而不是「线程池 12 层 + RunnableSeq 8 层」。注意：只在「最后一个匹配帧」处剥（`while tb.tb_next is not None and ...`），保证真正抛错的用户帧不被误删。

---

---

## 七、_executor.py：线程池执行器

`_executor.py`（223 行）只做一件事：**给运行器提供「提交即后台跑」的能力**，并保证退出时「取消该取消的、等齐该等的、只重抛一个异常」。同步版用线程池，异步版用 asyncio 任务 + 信号量。

| 成员 | 行号 | 作用 |
|---|---|---|
| `Submit`（Protocol） | 27–37 | `submit(fn, *args, __name__, __cancel_on_exit__, __reraise_on_exit__, __next_tick__, **kwargs)` 的接口签名 |
| `BackgroundExecutor` | 40–119 | 同步：包 `ThreadPoolExecutor` |
| `AsyncBackgroundExecutor` | 122–211 | 异步：包 asyncio 任务 + `asyncio.Semaphore` |
| `gated` | 214–217 | 「先抢信号量再跑协程」的包装协程 |
| `next_tick` | 220–223 | `time.sleep(0)` 让出线程后再执行 |

### 7.1 BackgroundExecutor：同步线程池

```python
# _executor.py:40-75 — BackgroundExecutor（节选 + 注释）
class BackgroundExecutor(AbstractContextManager):
    def __init__(self, config: RunnableConfig) -> None:
        self.stack = ExitStack()
        # ★ 线程池来自 config；langchain_core 会按 max_concurrency 决定 worker 数
        self.executor = self.stack.enter_context(get_executor_for_config(config))
        # future -> (cancel_on_exit, reraise_on_exit)
        self.tasks: dict[concurrent.futures.Future, tuple[bool, bool]] = {}

    def submit(self, fn, *args, __name__=None, __cancel_on_exit__=False,
               __reraise_on_exit__=True, __next_tick__=False, **kwargs):
        ctx = copy_context()             # ★ 复制 contextvars（否则线程里丢 run/回调上下文）
        if __next_tick__:
            task = self.executor.submit(next_tick, ctx.run, fn, *args, **kwargs)
        else:
            task = self.executor.submit(ctx.run, fn, *args, **kwargs)
        self.tasks[task] = (__cancel_on_exit__, __reraise_on_exit__)
        task.add_done_callback(self.done)
        return task
```

三个 submit 关键字参数的含义：

| 参数 | 同步语义 | 何时用 |
|---|---|---|
| `__cancel_on_exit__` | 退出时**仅当未启动**才 `cancel()`（线程一旦跑起来无法打断） | 一般给 `False`；异步版给 `True` |
| `__reraise_on_exit__` | 退出时是否重抛该任务的异常 | `call()` 子任务给 `False` |
| `__next_tick__` | 用 `next_tick` 包一层，先 `time.sleep(0)` 让出线程再执行 | 函数式 `call()` 的子任务（先让本 tick 落定） |

`copy_context()` 是隐藏要点：**没有它，线程池里的任务就看不到父协程/调用方设置的 contextvars**（LangGraph 用 contextvar 传 config/回调）。

```python
# _executor.py:77-119 — done 回调与 __exit__（节选 + 注释）
def done(self, task):
    """future 完成时把它从 tasks 里摘掉（用于「不在 exit 时重复处理」）。"""
    try:
        task.result()
    except GraphBubbleUp:
        self.tasks.pop(task)      # 中断不是错误：不当成待重抛项
    except BaseException:
        pass                      # 其它异常：保留在 tasks 里，交给 __exit__ 重抛
    else:
        self.tasks.pop(task)

def __exit__(self, exc_type, exc_value, traceback):
    tasks = self.tasks.copy()     # done 回调会改 dict，先快照
    for task, (cancel, _) in tasks.items():
        if cancel:
            task.cancel()
    if pending := {t for t in tasks if not t.done()}:
        concurrent.futures.wait(pending)     # 等所有任务真正结束
    self.stack.__exit__(exc_type, exc_value, traceback)   # 关线程池
    if exc_type is None:
        # ★ 若外层已在抛错，就不再多抛一个；否则重抛第一个需要重抛的异常
        for task, (_, reraise) in tasks.items():
            if not reraise:
                continue
            try:
                task.result()
            except concurrent.futures.CancelledError:
                pass
```

要点：**`__exit__` 的「只重抛第一个」**保证了「一个任务失败」时不会因为「N 个兄弟任务也失败」而抛出 N 个异常；且当调用方本身在抛错（`exc_type is not None`）时，运行器的异常不会覆盖原始异常。

### 7.2 AsyncBackgroundExecutor：asyncio + 信号量限流

```python
# _executor.py:131-169 — AsyncBackgroundExecutor（节选 + 注释）
class AsyncBackgroundExecutor(AbstractAsyncContextManager):
    def __init__(self, config: RunnableConfig) -> None:
        self.tasks: dict[asyncio.Future, tuple[bool, bool]] = {}
        self.sentinel = object()
        self.loop = asyncio.get_running_loop()
        # ★ 唯一的并发闸：config 里的 max_concurrency（来自 RunnableConfig）
        if max_concurrency := config.get("max_concurrency"):
            self.semaphore: asyncio.Semaphore | None = asyncio.Semaphore(max_concurrency)
        else:
            self.semaphore = None

    def submit(self, fn, *args, __name__=None, __cancel_on_exit__=False,
               __reraise_on_exit__=True, __next_tick__=False, **kwargs):
        coro = cast(Coroutine, fn(*args, **kwargs))
        if self.semaphore:
            coro = gated(self.semaphore, coro)        # ★ 限流：抢到槽才跑
        if CONTEXT_NOT_SUPPORTED:
            task = run_coroutine_threadsafe(coro, self.loop, name=__name__, lazy=__next_tick__)
        else:
            task = run_coroutine_threadsafe(coro, self.loop, name=__name__,
                                            context=copy_context(), lazy=__next_tick__)
        self.tasks[task] = (__cancel_on_exit__, __reraise_on_exit__)
        task.add_done_callback(self.done)
        return task

async def gated(semaphore: asyncio.Semaphore, coro: Coroutine[None, None, T]) -> T:
    """先 async with semaphore 抢槽，再 await 协程。"""
    async with semaphore:
        return await coro
```

**`max_concurrency` 是图中「同时在跑的节点数」的闸门**。注意：这张「闸」只覆盖图内节点的并发，**不等于**「整个服务能接多少并发问题」——后者由应用层的全局 run 槽位（本项目的经验值是 10 个 run 槽，一次问数约 3 个 run ⇒ 约 3~4 并发）决定。二者是不同层级：`max_concurrency` 管「一个 run 内」，全局槽位管「多个 run 之间」。

```python
# _executor.py:171-211 — done 与 __aexit__（节选 + 注释）
def done(self, task: asyncio.Future) -> None:
    try:
        if exc := task.exception():
            if isinstance(exc, GraphBubbleUp):
                self.tasks.pop(task)     # 中断不是错误
        else:
            self.tasks.pop(task)
    except asyncio.CancelledError:
        self.tasks.pop(task)

async def __aexit__(self, exc_type, exc_value, traceback):
    tasks = self.tasks.copy()
    for task, (cancel, _) in tasks.items():
        if cancel:
            task.cancel(self.sentinel)   # ★ 传 sentinel：区分「框架取消」与「用户自己取消」
    if tasks:
        await asyncio.wait(tasks)
    if exc_type is None:
        for task, (_, reraise) in tasks.items():
            if not reraise: continue
            try:
                if exc := task.exception():
                    raise exc
            except asyncio.CancelledError:
                pass
```

`task.cancel(self.sentinel)` 传一个哨兵对象：事后可以通过 `CancelledError.args` 是否含该哨兵来判断「这次取消是 LangGraph 发起的」还是「用户/其它框架发起的」——这与 `_retry._is_user_raised_cancelled()`（§12.5）的健壮性一脉相承。

### 7.3 next_tick 与「下一个 tick 启动」

```python
# _executor.py:220-223 — next_tick
def next_tick(fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """先 time.sleep(0) 把 CPU 让给其它线程，再执行 fn。"""
    time.sleep(0)
    return fn(*args, **kwargs)
```

`time.sleep(0)` 是一次「让出 GIL 的调度点」。函数式 `call()` 的子任务用它实现「父任务所在超步先完成/发流，子任务下一超步启动」（§6.6 的 `__next_tick__=True`）。

---

## 八、_call.py：节点调用与函数式 API

`_call.py`（298 行）是**函数式 API 的入口层**：把用户写的普通函数包装成 `Runnable`，并把用户写的 `call()` 转成一个待调度任务。它同时负责「对象的稳定标识」（`identifier`）——缓存（`CachePolicy`）与 checkpoint 都靠它。

| 成员 | 行号 | 作用 |
|---|---|---|
| `identifier(obj, name=None)` | 84–110 | 产出一个对象的稳定「模块.名字」字符串，用于缓存键 |
| `_whichmodule` / `_lookup_module_and_qualname` / `_getattribute` | 37–154 | 从 cloudpickle 借来的「可序列化性」判定 |
| `get_runnable_for_entrypoint(func)` | 175–197 | `@entrypoint` 函数 → `RunnableCallable` |
| `get_runnable_for_task(func)` | 200–242 | `@task`/`call` 函数 → `RunnableSeq(run, ChannelWrite([RETURN]))` |
| `SyncAsyncFuture` | 253–255 | 既能 `.result()` 又能 `await` 的 future |
| `call(func, *args, ...)` | 258–273 | 用户 API：调度一个函数调用 |
| `_call_with_options` | 276–298 | 真正落地：从 config 取 `CONFIG_KEY_CALL` 并调用 |
| `CACHE` | 245 | `{(func, is_task): Runnable}` 全局缓存 |
| `_explode_args_trace_inputs` | 157–172 | 把 `(args, kwargs)` 展平成 dict，供 tracing 显示 |

### 8.1 identifier：稳定标识（缓存/检查点的地基）

```python
# _call.py:84-110 — identifier（节选 + 注释）
def identifier(obj: Any, name: str | None = None) -> str | None:
    """返回对象的「模块.qualname」。用于缓存键（CacheKey 的第 2 段）。"""
    from langgraph.pregel._read import PregelNode
    if isinstance(obj, PregelNode):
        obj = obj.bound                     # PregelNode -> 取其 bound
    if isinstance(obj, RunnableSeq):
        obj = obj.steps[0]                  # RunnableSeq -> 取第一步
    if isinstance(obj, RunnableCallable):
        obj = obj.func                      # RunnableCallable -> 取原始函数
    if name is None:
        name = getattr(obj, "__qualname__", None)
    if name is None:
        name = getattr(obj, "__name__", None)
    if name is None:
        return None
    module_name = getattr(obj, "__module__", None)
    if module_name is None:
        return None                         # 无 __module__ -> 视为「动态」，返回 None
    return f"{module_name}.{name}"
```

返回 `None` 是「动态函数（如 lambda、`__module__` 为 None）」的信号。缓存键里 `identifier(proc) or "__dynamic__"`（`_algo.py:674`）——动态函数统一落到 `"__dynamic__"` 桶，从而「两个内容相同的 lambda」也能命中同一缓存。**这就是「缓存 key 与函数身份挂钩」的实现**；它也是「升级 langgraph 后缓存失效」的常见原因（identifier 变则 key 变）。

### 8.2 get_runnable_for_task：@task 的包装

```python
# _call.py:200-242 — get_runnable_for_task（节选 + 注释）
def get_runnable_for_task(func: Callable[..., Any]) -> Runnable:
    key = (func, True)
    if key in CACHE:
        return CACHE[key]                    # 已包装过 -> 复用
    # 名字：优先 __name__ / func.func.__name__ / 类名
    if hasattr(func, "__name__"):
        name = func.__name__
    elif hasattr(func, "func"):
        name = func.func.__name__
    elif hasattr(func, "__class__"):
        name = func.__class__.__name__
    else:
        name = str(func)

    if is_async_callable(func):
        run = RunnableCallable(None, func, explode_args=True, name=name, trace=False, recurse=False)
    else:
        run = RunnableCallable(func,
                functools.wraps(func)(functools.partial(run_in_executor, None, func)),
                explode_args=True, name=name, trace=False, recurse=False)
    # ★ 关键：任务函数后面拼一个 ChannelWrite([ChannelWriteEntry(RETURN)])
    seq = RunnableSeq(run, ChannelWrite([ChannelWriteEntry(RETURN)]), name=name,
                      trace_inputs=functools.partial(_explode_args_trace_inputs,
                                                     inspect.signature(func)))
    if not _lookup_module_and_qualname(func):
        return seq                           # 无法定位模块 -> 不入缓存
    return CACHE.setdefault(key, seq)
```

要点：**`@task` 的返回值被写进保留通道 `RETURN`**（若同步函数，则通过 `run_in_executor` 丢到线程，避免阻塞事件循环）。`RETURN` 通道正是 §6.6 「情况 B：从 `next_task.writes` 里取 `RETURN`」的来源——这让「重试时子任务已跑过」可以直接取回结果而不重跑。

`_lookup_module_and_qualname`（`_call.py:113-154`）判断函数是否「可从模块路径重新导入」（即能否被 pickle）。**不能定位就不入缓存**：因为「同名不同定义」的函数混进缓存会导致错误复用。

### 8.3 SyncAsyncFuture 与 call()

```python
# _call.py:253-255 — SyncAsyncFuture：同步 future 但可 await
class SyncAsyncFuture(Generic[T], concurrent.futures.Future[T]):
    def __await__(self) -> Generator[T, None, T]:
        yield cast(T, ...)        # 交给事件循环调度（配合 run_coroutine_threadsafe 桥接）

# _call.py:258-298 — call() 与 _call_with_options（节选）
def call(func, *args, retry_policy=None, cache_policy=None,
         timeout=None, **kwargs) -> SyncAsyncFuture[T]:
    return _call_with_options(func, args, kwargs, retry_policy=retry_policy,
                              cache_policy=cache_policy, timeout=coerce_timeout_policy(timeout))

def _call_with_options(func, args, kwargs, *, retry_policy=None,
                       cache_policy=None, timeout=None) -> SyncAsyncFuture[T]:
    if timeout is not None and not is_async_callable(func):
        # 只有异步节点才支持 timeout（同步节点阻塞事件循环，超时看门狗不生效）
        raise sync_timeout_unsupported(getattr(func, "__name__", ...), kind="Task")
    config = get_config()
    impl = config[CONF][CONFIG_KEY_CALL]     # ← runner 注入的 _call（§6.6）
    fut = impl(func, (args, kwargs), retry_policy=retry_policy,
               cache_policy=cache_policy, callbacks=config["callbacks"], timeout=timeout)
    return fut
```

要点：

1. **`call()` 本身不执行任何东西**——它只是「从 config 里取出 runner 注入的 `CONFIG_KEY_CALL` 实现并调用」。所以 `call()` 只能在「一个 Pregel 任务的上下文里」使用（`get_config()` 需要运行中的 config）；在节点外裸调会 `KeyError`。
2. **`timeout` 只对异步函数有效**：同步函数会阻塞事件循环，看门狗无法打断（`sync_timeout_unsupported` 直接抛错）。这与 MEMORY 中「P1-14 同步阻塞搬离事件循环」的教训一致——同步工作必须 `run_in_executor` 到线程池。
3. `SyncAsyncFuture` 继承 `concurrent.futures.Future` 又重写 `__await__`，所以无论调用方是 `.result()` 还是 `await`，都能拿到同一个结果。

### 8.4 _call.py 与 _runner.py、_algo.py 的三角关系

一次 `await call(f, x)` 的完整链路：

```text
用户节点: await call(f, x)
  └─ _call.py: call() -> _call_with_options()
       └─ config[CONF][CONFIG_KEY_CALL]            # runner 注入
            └─ _runner.py: _call(task, f, (args,kwargs), ...)
                 └─ schedule_task(...)  = _loop.accept_push   # §4.7
                      └─ _algo.py: prepare_single_task((PUSH, ..., Call))  # §5.3
                           └─ prepare_push_task_functional(...)            # §5.7
                 └─ submit()(run_with_retry, next_task, __next_tick__=True)
                      └─ _executor: next_tick -> run_with_retry -> task.proc.invoke
                           └─ get_runnable_for_task(f) 的执行本体
                                └─ ChannelWrite([RETURN]) 把返回值写入任务 deque
                 └─ chain_future(...) 把子任务结果转发回 call() 的返回值
```

理解这条链，就理解了「为什么函数式 API 与图式 API 能共用同一套调度/并发/检查点机制」——**它们的差别只在 `Call` vs `Send` 这一个载体上**。

---

---

## 九、_io.py：输入输出映射

`_io.py`（174 行）是一组**纯函数**，负责把「图外部的输入/Command」翻译成「通道写入」，以及把「通道写入/任务结果」翻译成「`stream()` 吐出的输出块」。它是 `stream_mode` 语义的落点之一。

| 函数 | 行号 | 作用 |
|---|---|---|
| `read_channel` | 23–35 | 读单个通道，空通道返回 `None`（或按 `catch` 抛 `EmptyChannelError`） |
| `read_channels` | 38–53 | 读多个通道；`skip_empty=True` 时跳过空通道 |
| `map_input` | 81–97 | `invoke(input)` 的 input → 通道写入序列 |
| `map_command` | 56–78 | `Command(goto=..., resume=..., update=...)` → 通道写入序列 |
| `map_output_values` | 100–115 | pending writes → `stream_mode="values"` 的输出块（完整状态） |
| `map_output_updates` | 118–174 | task writes → `stream_mode="updates"` 的输出块（{节点: 增量}） |

### 9.1 read_channel / read_channels

```python
# _io.py:23-53 — 读通道（完整）
def read_channel(channels, chan, *, catch=True):
    try:
        return channels[chan].get()
    except EmptyChannelError:
        if catch:
            return None          # 默认：空通道 = None（调用方无需处理异常）
        else:
            raise                # catch=False：让 EmptyChannelError 冒泡（如 read_channels 的组合读）

def read_channels(channels, select, *, skip_empty=True):
    if isinstance(select, str):
        return read_channel(channels, select)      # 单通道：直接返回值
    else:
        values: dict[str, Any] = {}
        for k in select:
            try:
                values[k] = read_channel(channels, k, catch=not skip_empty)
            except EmptyChannelError:
                pass                               # 组合读：空通道整个跳过
        return values                              # 多通道：{chan: value}
```

**`read_channel` 默认把空通道读成 `None` 是 LangGraph 的「宽容语义」**：`graph.get_state()` 里某个 key 没被写时就是 `None`，而不是报错。但 `read_channels` 用 `catch=not skip_empty` 配合 `skip_empty=True`（默认）时又「整个跳过空通道」——即**多通道读的字典里不含空通道的键**。这个区分很重要：读「整个 state」时你希望看到所有被写过的键，而不是一堆 `None`。

### 9.2 map_input：输入如何变成通道写入

```python
# _io.py:81-97 — map_input（完整 + 注释）
def map_input(input_channels, chunk) -> Iterator[tuple[str, Any]]:
    """把 invoke 的 input 映射成 (channel, value) 写入序列。"""
    if chunk is None:
        return                                  # 无输入（如纯恢复）-> 不写
    elif isinstance(input_channels, str):
        yield (input_channels, chunk)           # 单输入通道：整个 chunk 就是它的值
    else:
        if not isinstance(chunk, dict):
            raise TypeError(f"Expected chunk to be a dict, got {type(chunk).__name__}")
        for k in chunk:
            if k in input_channels:
                yield (k, chunk[k])             # 多输入通道：chunk 的每个键对应一个通道
            else:
                logger.warning(f"Input channel {k} not found in {input_channels}")
```

`StateGraph(input_schema=...)` 编译后 `input_channels` 通常是「schema 的所有键」（一个列表），所以用户 `graph.invoke({"messages": [...], "foo": 1})` 就是「把 chunk 里每个键写成对应通道的写入」。**输入里出现了 schema 之外的键**只会 warning（被忽略），不会报错——这是「宽容输入」的设计。

### 9.3 map_command：Command 的三类指令

`Command` 是 LangGraph 表达「跳转/恢复/更新」的统一载体，`map_command` 把它拆成三种写入：

```python
# _io.py:56-78 — map_command（完整 + 注释）
def map_command(cmd: Command) -> Iterator[tuple[str, str, Any]]:
    """把 Command 映射成 (task_id, channel, value) 三元组的写入序列。
    这里的 task_id 恒为 NULL_TASK_ID（Command 是「无主写入」）。"""
    if cmd.graph == Command.PARENT:
        raise InvalidUpdateError("There is no parent graph")   # 顶层图没有父图
    if cmd.goto:
        sends = cmd.goto if isinstance(cmd.goto, (tuple, list)) else [cmd.goto]
        for send in sends:
            if isinstance(send, Send):
                yield (NULL_TASK_ID, TASKS, send)              # Send -> 排队到 TASKS
            elif isinstance(send, str):
                yield (NULL_TASK_ID, f"branch:to:{send}", START)  # 目标节点名 -> 触发它的通道
            else:
                raise TypeError(f"In Command.goto, expected Send/str, got {type(send).__name__}")
    if cmd.resume is not None:
        yield (NULL_TASK_ID, RESUME, cmd.resume)               # 恢复值 -> RESUME 通道
    if cmd.update:
        for k, v in cmd._update_as_tuples():
            yield (NULL_TASK_ID, k, v)                         # 状态更新 -> 各状态通道
```

关键点：**`Command(goto="node")` 被翻译成「往 `branch:to:node` 通道写 `START`」**。`branch:to:<名字>` 是编译器为每个节点预留的触发通道（`add_conditional_edges` 的落点也是它）。理解这一点就理解了「为什么条件边返回字符串就能决定下一步走谁」——那不是控制流，是「往一个特殊通道写值，让目标节点的触发器被满足」。

### 9.4 map_output_values 与 map_output_updates

```python
# _io.py:100-115 — map_output_values：stream_mode="values"（完整状态）
def map_output_values(output_channels, pending_writes, channels) -> Iterator:
    if isinstance(output_channels, str):
        if pending_writes is True or any(chan == output_channels for chan, _ in pending_writes):
            yield read_channel(channels, output_channels)      # 单通道：吐值
    else:
        if pending_writes is True or {c for c, _ in pending_writes if c in output_channels}:
            yield read_channels(channels, output_channels)     # 多通道：吐完整状态 dict
```

`stream_mode="values"` 每次吐的是**完整 state 快照**（不是增量）。`pending_writes is True` 表示「强制输出」（如本超步没有任何写入但也要让调用方看到当前状态）。

```python
# _io.py:118-174 — map_output_updates（节选 + 注释）
def map_output_updates(output_channels, tasks, cached=False) -> Iterator[dict]:
    # 过滤掉：隐藏任务（TAG_HIDDEN）、错误写入、中断写入
    output_tasks = [(t, ww) for t, ww in tasks
                    if (not t.config or TAG_HIDDEN not in t.config.get("tags", EMPTY_SEQ))
                    and ww[0][0] != ERROR and ww[0][0] != INTERRUPT]
    if not output_tasks:
        return
    updated: list[tuple[str, Any]] = []
    for task, writes in output_tasks:
        # RETURN 优先：函数式任务的返回值直接作为其结果
        rtn = next((value for chan, value in writes if chan == RETURN), MISSING)
        if rtn is not MISSING:
            updated.append((task.name, rtn))
        elif isinstance(output_channels, str):
            updated.extend((task.name, value) for chan, value in writes
                           if chan == output_channels)
        elif any(chan in output_channels for chan, _ in writes):
            counts = Counter(chan for chan, _ in writes)
            if any(counts[chan] > 1 for chan in output_channels):
                # 同通道被写多次：逐条吐 {chan: value}
                updated.extend((task.name, {chan: value}) for chan, value in writes
                               if chan in output_channels)
            else:
                # 同通道只写一次：合并成 {chan: value, ...}
                updated.append((task.name,
                                {chan: value for chan, value in writes
                                 if chan in output_channels}))
    # 按节点分组；0 条 -> None，1 条 -> 解包成单值（保持通常的「一节点一更新」形态）
    grouped: dict[str, Any] = {t.name: [] for t, _ in output_tasks}
    for node, value in updated:
        grouped[node].append(value)
    for node, value in grouped.items():
        if len(value) == 0: grouped[node] = None
        if len(value) == 1: grouped[node] = value[0]
    if cached:
        grouped["__metadata__"] = {"cached": cached}    # 缓存命中的标记
    yield grouped
```

要点：

- `stream_mode="updates"` 吐的是 **`{节点名: 该节点的增量}`**，且做了「单条解包、多条聚合」的归一化（`{"node": 42}` vs `{"node": [42, 43]}`）。
- `cached=True` 时额外插入 `{"__metadata__": {"cached": True}}`——对应「节点缓存命中」场景（`match_cached_writes`）。

### 9.5 三种流模式的数据来源对照

| `stream_mode` | 由谁产出 | 触发时机 | 输出形态 |
|---|---|---|---|
| `"values"` | `loop.after_tick` → `map_output_values` | 每超步末 | 完整 state 快照 |
| `"updates"` | `loop.put_writes` → `output_writes` → `map_output_updates` | 每个任务完成 | `{节点: 增量}` |
| `"messages"` | `_messages.StreamMessagesHandler`（LLM 回调） | 每个 token / 每条消息 | `(message, metadata)` |
| `"custom"` | `get_stream_writer()` → `StreamWriter` | 节点内任意时刻 | 用户自定义 |
| `"debug"` | `debug.py` 的 map_* 系列 | 每超步（Plan/Execution/Update） | 结构化事件 |
| `"checkpoints"` | `debug.map_debug_checkpoint` | 每次 checkpoint | checkpoint 负载 |
| `"tasks"` | `debug.map_debug_tasks` / `map_debug_task_results` | 任务开始/结束 | task 负载 |

---

## 十、_read.py / _write.py：通道读写机制

这一对文件定义「演员（actor）」`PregelNode` 与它的两组动作：**读（ChannelRead）**与**写（ChannelWrite）**。

### 10.1 PregelNode：actor 容器

```python
# _read.py:97-149 — PregelNode 字段（节选 + 中文注释）
class PregelNode:
    channels: str | list[str]
    """作为输入传给 bound 的通道。str -> 传值；list -> 传 {chan: value} dict。"""
    triggers: list[str]
    """订阅通道：任一被写就触发本节点（版本比较见 §5.4）。"""
    mapper: Callable[[Any], Any] | None
    """把输入变换后再交给 bound 的映射器。"""
    writers: list[Runnable]
    """bound 之后执行、负责把 bound 的输出写进通道的写入器。"""
    bound: Runnable[Any, Any]
    """节点主逻辑：以上面 channels 的输入被调用。"""
    retry_policy: Sequence[RetryPolicy] | None
    cache_policy: CachePolicy | None
    timeout: TimeoutPolicy | None
    """单次调用的超时策略（仅异步节点支持，见 _retry）。"""
    tags: Sequence[str] | None
    metadata: Mapping[str, Any] | None
    is_error_handler: bool
    """本节点是否是「错误处理器节点」。"""
    error_handler_node: str | None
    """本节点失败时路由到的处理器节点名。"""
    subgraphs: Sequence[PregelProtocol]
    """本节点用到的子图（用于 xray 展示与子图流）。"""
```

**`channels` 是 str 还是 list 决定了输入形态**，这是 `StateGraph` 里「节点签名收 state dict 还是单值」的底层原因（§5.8）。

### 10.2 三个 cached_property：flat_writers / node / input_cache_key

```python
# _read.py:196-245 — PregelNode 的三个缓存属性（节选 + 注释）
def copy(self, update):                     # 复制时丢弃缓存属性，避免陈旧
    attrs = {**self.__dict__, **update}
    attrs.pop("flat_writers", None); attrs.pop("node", None); attrs.pop("input_cache_key", None)
    return PregelNode(**attrs)

@cached_property
def flat_writers(self) -> list[Runnable]:
    """把「连续的 ChannelWrite」合并成一个（减少 Runnable 调用层数）。"""
    writers = self.writers.copy()
    while (len(writers) > 1 and isinstance(writers[-1], ChannelWrite)
           and isinstance(writers[-2], ChannelWrite)):
        writers[-2] = ChannelWrite(writes=writers[-2].writes + writers[-1].writes)
        writers.pop()
    return writers

@cached_property
def node(self) -> Runnable | None:
    """把 bound 与 writers 组装成一个可执行 Runnable。"""
    writers = self.flat_writers
    if self.bound is DEFAULT_BOUND and not writers:   # 纯读节点
        return None
    elif self.bound is DEFAULT_BOUND and len(writers) == 1:
        return writers[0]
    elif self.bound is DEFAULT_BOUND:
        return RunnableSeq(*writers)
    elif writers:
        return RunnableSeq(self.bound, *writers)      # 常见：bound + 若干 writer
    else:
        return self.bound

@cached_property
def input_cache_key(self) -> INPUT_CACHE_KEY_TYPE:
    """输入缓存键 = (mapper, channels 元组)。同一键的输入只算一次。"""
    return (self.mapper, tuple(self.channels)
            if isinstance(self.channels, list) else (self.channels,))
```

`node` 的组装规则解释了「一个节点执行时到底跑了什么」：**`bound`（用户逻辑）后面串上 `ChannelWrite`（把返回值写到指定通道）**。`flat_writers` 的合并是纯优化——连续的 `ChannelWrite` 合成一个，少一层 `RunnableSeq` 调用。

### 10.3 ChannelRead：从任务内部读状态

```python
# _read.py:25-91 — ChannelRead（节选 + 注释）
class ChannelRead(RunnableCallable):
    """实现 CONFIG_KEY_READ 的读取逻辑。既是 Runnable，也可静态调用。"""
    def __init__(self, channel, *, fresh=False, mapper=None, tags=None):
        super().__init__(func=self._read, afunc=self._aread, tags=tags, name=None, trace=False)
        self.fresh = fresh; self.mapper = mapper; self.channel = channel

    @staticmethod
    def do_read(config, *, select, fresh=False, mapper=None):
        try:
            read: READ_TYPE = config[CONF][CONFIG_KEY_READ]    # runner/algo 注入的 local_read
        except KeyError:
            raise RuntimeError("Not configured with a read function"
                               "Make sure to call in the context of a Pregel process")
        if mapper:
            return mapper(read(select, fresh))
        else:
            return read(select, fresh)
```

`ChannelRead` 是「在节点内部读图状态」的机制：条件边（`add_conditional_edges` 路由函数）、工具、以及需要「读最新状态」的技能都用它。它把调用转交给 config 里的 `CONFIG_KEY_READ`（= `_algo.local_read`，§5.8）。`fresh=True` 表示「读包含本节点已写入的值」，`fresh=False` 读步初快照。

### 10.4 ChannelWrite：三种写条目

```python
# _write.py:26-50 — 三种写条目（完整 + 注释）
class ChannelWriteEntry(NamedTuple):
    channel: str                                 # 目标通道名
    value: Any = PASSTHROUGH                     # 要写的值；PASSTHROUGH 表示「用输入」
    skip_none: bool = False                      # 值为 None 时是否跳过
    mapper: Callable | None = None               # 写前变换

class ChannelWriteTupleEntry(NamedTuple):
    mapper: Callable[[Any], Sequence[tuple[str, Any]] | None]   # 从值里抽取多个 (chan, val)
    value: Any = PASSTHROUGH
    static: Sequence[tuple[str, Any, str | None]] | None = None # 供静态分析声明的写入

# Send 对象也可作为写条目出现（写进 TASKS 通道）
```

```python
# _write.py:105-126 — ChannelWrite.do_write（完整 + 注释）
@staticmethod
def do_write(config, writes, allow_passthrough=True):
    # ① 校验
    for w in writes:
        if isinstance(w, ChannelWriteEntry):
            if w.channel == TASKS:
                raise InvalidUpdateError("Cannot write to the reserved channel TASKS")
            if w.value is PASSTHROUGH and not allow_passthrough:
                raise InvalidUpdateError("PASSTHROUGH value must be replaced")
        if isinstance(w, ChannelWriteTupleEntry):
            if w.value is PASSTHROUGH and not allow_passthrough:
                raise InvalidUpdateError("PASSTHROUGH value must be replaced")
    # ② 组装成 (channel, value) 元组并交给 CONFIG_KEY_SEND（= 任务 deque.extend）
    write: TYPE_SEND = config[CONF][CONFIG_KEY_SEND]
    write(_assemble_writes(writes))
```

```python
# _write.py:172-192 — _assemble_writes（完整 + 注释）
def _assemble_writes(writes) -> list[tuple[str, Any]]:
    tuples: list[tuple[str, Any]] = []
    for w in writes:
        if isinstance(w, Send):
            tuples.append((TASKS, w))            # Send -> 写进 TASKS 通道
        elif isinstance(w, ChannelWriteTupleEntry):
            if ww := w.mapper(w.value):          # 参数化写入：动态决定写哪些通道
                tuples.extend(ww)
        elif isinstance(w, ChannelWriteEntry):
            value = w.mapper(w.value) if w.mapper is not None else w.value
            if value is SKIP_WRITE: continue     # 显式跳过
            if w.skip_none and value is None: continue
            tuples.append((w.channel, value))
        else:
            raise ValueError(f"Invalid write entry: {w}")
    return tuples
```

三个常量语义：`PASSTHROUGH`（用节点输入当值）、`SKIP_WRITE`（显式不写，从 mapper 返回）、`TASKS`（保留通道，禁止用户直写）。**`Send` 作为写条目时被翻译成 `(TASKS, send)`** —— 这就是 §5.6 里 Send 排队的源头。

### 10.5 register_writer / is_writer / get_static_writes

```python
# _write.py:128-169 — 写入器识别（节选 + 注释）
@staticmethod
def is_writer(runnable: Runnable) -> bool:
    """区分「写入器」与「普通 Runnable」。"""
    return (isinstance(runnable, ChannelWrite)
            or getattr(runnable, "_is_channel_writer", MISSING) is not MISSING)

@staticmethod
def register_writer(runnable: R, static=None) -> R:
    """把一个 Runnable 标记为写入器（供 is_writer 识别）。
    用 object.__setattr__ 绕过 pydantic/dataclass 的 __setattr__ 覆盖。"""
    object.__setattr__(runnable, "_is_channel_writer", static)
    return runnable
```

这组 API 让**自定义 Runnable（如工具）也能被当作「写入器」**：`is_writer` 检查一个私有属性 `_is_channel_writer`，`register_writer` 用 `object.__setattr__` 设置它。`get_static_writes` 则用于「静态分析」（如 `stream_mode="debug"` 展示写入目标、xray 图渲染）。

### 10.6 读写的完整闭环

```text
任务执行：
  PregelNode.node  (= RunnableSeq(bound, *flat_writers))
    ├─ 输入来自 _proc_input（读 channels / managed）
    ├─ bound.invoke(input)             # 用户逻辑
    └─ ChannelWrite.do_write(config, ...)
         ├─ _assemble_writes -> [(chan, val), ...]
         └─ CONFIG_KEY_SEND = task.writes.extend   # ★ 只进 deque，不即时生效
任务结束：
  runner.commit(task, None)
    └─ loop.put_writes(task.id, task.writes)        # 落盘 + 发流
超步末：
  loop.after_tick() -> apply_writes(...)            # ★ barrier：写入原子并入通道
```

**「写 deque → commit → apply_writes」三段式**是「步内不可见、步末统一提交」的物理保证（§4.5、§5.5）。

---

---

## 十一、_validate.py：图验证

`_validate.py`（120 行）只有两个函数，却是「编译期就挡住一半错误」的关口。它在 `Pregel.validate()`（§2.3）里被调用，跑在「建图完成」和「真正执行」之间。

| 函数 | 行号 | 作用 |
|---|---|---|
| `validate_graph` | 13–107 | 校验通道/节点/输入/输出/中断点的合法性 |
| `validate_keys` | 110–120 | 校验某个 key 是否存在于通道集合中 |

### 11.1 validate_graph：逐条规则

```python
# _validate.py:13-107 — validate_graph（完整 + 中文注释）
def validate_graph(nodes, channels, managed, input_channels, output_channels,
                   stream_channels, interrupt_after_nodes, interrupt_before_nodes) -> None:
    # ① 保留名检查：用户不得占用 LangGraph 内部通道名
    for chan in channels:
        if chan in RESERVED:
            raise ValueError(f"Channel name '{chan}' is reserved")
    for name in managed:
        if name in RESERVED:
            raise ValueError(f"Managed name '{name}' is reserved")

    # ② 遍历节点：收集订阅通道；校验节点读到/订阅的通道确实存在
    subscribed_channels = set[str]()
    for name, node in nodes.items():
        if name in RESERVED:
            raise ValueError(f"Node name '{name}' is reserved")
        if isinstance(node, PregelNode):
            subscribed_channels.update(node.triggers)
            if isinstance(node.channels, str):
                if node.channels not in channels:
                    raise ValueError(f"Node {name} reads channel '{node.channels}' "
                                     f"not in known channels: ...")
            else:
                for chan in node.channels:
                    # ★ 读的通道可以是 channel 或 managed value
                    if chan not in channels and chan not in managed:
                        raise ValueError(f"Node {name} reads channel '{chan}' "
                                         f"not in known channels: ...")
        else:
            raise TypeError(f"Invalid node type {type(node)}, expected PregelNode or NodeBuilder")

    # ③ 每个被订阅的触发通道都必须存在
    for chan in subscribed_channels:
        if chan not in channels:
            raise ValueError(f"Subscribed channel '{chan}' not in known channels: ...")

    # ④ 输入通道：必须存在，且至少有一个被某节点订阅
    if isinstance(input_channels, str):
        if input_channels not in channels:
            raise ValueError(f"Input channel '{input_channels}' not in known channels: ...")
        if input_channels not in subscribed_channels:
            raise ValueError(f"Input channel {input_channels} is not subscribed to by any node")
    else:
        for chan in input_channels:
            if chan not in channels:
                raise ValueError(f"Input channel '{chan}' not in ...")
        if all(chan not in subscribed_channels for chan in input_channels):
            raise ValueError(f"None of the input channels {input_channels} "
                             f"are subscribed to by any node")

    # ⑤ 输出/流通道必须存在
    all_output_channels = set[str]()
    if isinstance(output_channels, str): all_output_channels.add(output_channels)
    else: all_output_channels.update(output_channels)
    if isinstance(stream_channels, str): all_output_channels.add(stream_channels)
    elif stream_channels is not None: all_output_channels.update(stream_channels)
    for chan in all_output_channels:
        if chan not in channels:
            raise ValueError(f"Output channel '{chan}' not in known channels: ...")

    # ⑥ interrupt_before/after 指定的节点名必须存在（"*" 除外）
    if interrupt_after_nodes != "*":
        for n in interrupt_after_nodes:
            if n not in nodes:
                raise ValueError(f"Node {n} not in nodes")
    if interrupt_before_nodes != "*":
        for n in interrupt_before_nodes:
            if n not in nodes:
                raise ValueError(f"Node {n} not in nodes")
```

### 11.2 六类校验与对应的典型报错

| # | 校验 | 触发条件 | 报错文案（节选） | 修复 |
|---|---|---|---|---|
| ① | 保留名 | 通道/managed 名落在 `RESERVED`（如 `__pregel_*`、`TASKS`、`branch:to:*` 等） | `Channel name 'X' is reserved` | 换名；别用 `__pregel_`/`branch:`/`TASKS` 前缀 |
| ② | 节点读通道 | 节点 `channels` 里的通道既不是 channel 也不是 managed | `Node X reads channel 'Y' not in known channels` | 补通道定义，或把名字改成实际存在的 key |
| ② | 节点类型 | `nodes` 的值不是 `PregelNode`/`NodeBuilder` | `Invalid node type ...` | 用 `builder.add_node` 或 `NodeBuilder` |
| ③ | 订阅通道 | `triggers` 里引用了不存在的通道 | `Subscribed channel 'Y' not in known channels` | 通常是「边指向了不存在的节点名」——检查 `add_edge` 目标 |
| ④ | 输入通道 | 输入通道不存在，或没有任何节点订阅它 | `Input channel X is not subscribed to by any node` | 输入 schema 里有「无人消费」的键——删掉或接上消费者 |
| ⑤ | 输出通道 | 输出/流通道不存在 | `Output channel 'Y' not in known channels` | 输出 schema 里的键与 state schema 对不上 |
| ⑥ | 中断点 | `interrupt_before/after` 指定了不存在的节点 | `Node X not in nodes` | 名字拼错（最常见）；MEMORY 中「skill owner 拼写必须=真实目录名」同源 |

**④ 是最容易让人困惑的一条**：给 `input_schema` 加了一个字段却没有任何节点读它，编译期就会报 `is not subscribed to by any node`。LangGraph 的立场是：**输入必须通向某个消费者，否则就是死配置**。

### 11.3 validate_keys

```python
# _validate.py:110-120 — validate_keys（完整 + 注释）
def validate_keys(keys: str | Sequence[str] | None, channels: Mapping[str, Any]) -> None:
    """校验 keys（单个或多个）是否都在 channels 里。"""
    if isinstance(keys, str):
        if keys not in channels:
            raise ValueError(f"Key {keys} not in channels")
    elif keys is not None:
        for chan in keys:
            if chan not in channels:
                raise ValueError(f"Key {chan} not in channels")
```

`validate_keys` 是更轻量的单点校验，用于「只关心某几个 key 是否存在」的场景（比 `validate_graph` 的整套检查窄）。

### 11.4 编译期超时支持性检查

`validate_graph` 之外，`Pregel.validate()` 还会逐个节点跑 `validate_timeout_supported`（在 `langgraph/_internal/_timeout.py`）：

```python
# main.py:228-244 — validate()（节选，见 §2.3）
def validate(self) -> Self:
    for node in self.nodes.values():
        validate_timeout_supported(node)     # ★ 带 timeout 的同步节点在编译期就报错
    validate_graph(nodes=self.nodes, channels=self.channels, managed=...,
                   input_channels=self.input_channels,
                   output_channels=self.output_channels,
                   stream_channels=self.stream_channels_asis,
                   interrupt_after_nodes=self.interrupt_after_nodes,
                   interrupt_before_nodes=self.interrupt_before_nodes)
    return self
```

**同步节点 + timeout ⇒ 编译期直接 `sync_timeout_unsupported`**（在真正执行前就挡住），这与 §8.3 里 `call()` 的运行时检查是「同一约束的两道闸」。运行时 `run_with_retry` 还有第三道（`_retry.py:580-583`）：`if task.timeout is not None: raise sync_timeout_unsupported(...)`。**同一个约束三重设防**，因为超时看门狗依赖 asyncio 协作式取消，同步节点无法被打断。

---

## 十二、_retry.py：重试与超时机制

`_retry.py`（854 行）是 1.2.x 里增长最快的文件之一：除了基础的 `RetryPolicy` 退避，还新增了 **TimeoutPolicy 看门狗**、**进度观测器**、以及 **`asyncio.CancelledError` 的语义消歧**。

| 成员 | 行号 | 作用 |
|---|---|---|
| `run_with_retry` | 573–682 | 同步任务的重试主循环 |
| `arun_with_retry` | 685–838 | 异步任务的重试主循环（含 timeout） |
| `_should_retry_on` | 841–854 | 判定「某个异常是否命中某个 RetryPolicy」 |
| `_resolve_timeout` / `_ResolvedTimeout` | 63–84 | 把 `TimeoutPolicy` 解析成秒数 |
| `_arun_with_timeout` | 422–517 | 「任务 vs 看门狗」竞速，超时抛 `NodeTimeoutError` |
| `_TimedAttemptScope` | 128–271 | 被 timeout 守卫的 config 窗口（写入/流/子任务的「进度」信号源） |
| `_IdleProgressCallbackHandler` | 274–313 | 任何 LangChain 回调事件都重置 idle 时钟 |
| `_is_user_raised_cancelled` | 315–334 | 区分「框架取消」与「用户主动 raise CancelledError」 |
| `_checkpoint_ns_for_parent_command` | 544–570 | 计算 `Command.PARENT` 应上升到的父命名空间 |

### 12.1 RetryPolicy 字段与默认值

```python
# langgraph/types.py:416-435 — RetryPolicy（完整）
class RetryPolicy(NamedTuple):
    initial_interval: float = 0.5      # 首次重试前等待秒数
    backoff_factor: float = 2.0        # 每次重试间隔的倍增系数
    max_interval: float = 128.0        # 单次间隔上限（秒）
    max_attempts: int = 3              # 最大尝试次数（含首次）
    jitter: bool = True                # 是否叠加随机抖动
    retry_on: (...)= default_retry_on  # 触发重试的异常类/callable
```

```python
# langgraph/_internal/_retry.py:1-25 — default_retry_on（完整 + 注释）
def default_retry_on(exc: Exception) -> bool:
    import httpx, requests
    if isinstance(exc, ConnectionError):
        return True                                        # 连接错误 -> 重试
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600       # 5xx -> 重试；4xx -> 不重试
    if isinstance(exc, requests.HTTPError):
        return 500 <= exc.response.status_code < 600 if exc.response else True
    if isinstance(exc, (ValueError, TypeError, ArithmeticError, ImportError, LookupError,
                        NameError, SyntaxError, RuntimeError, ReferenceError,
                        StopIteration, StopAsyncIteration, OSError)):
        return False                                       # ★ 这些「逻辑错误」不重试
    return True                                            # 其余（多为未知/超时类）-> 重试
```

**默认策略的核心判断**：`ValueError/TypeError/RuntimeError/OSError` 等「代码写错了」的异常**不重试**（重试多少次都一样），而连接类、5xx、以及未分类异常默认重试。这解释了为什么「模型 401/402 不会重试」——它们是 4xx（或在 `httpx` 上被归为非 5xx）。

### 12.2 run_with_retry：同步重试主循环

```python
# _retry.py:573-682 — run_with_retry（节选 + 中文注释）
def run_with_retry(task, retry_policy, configurable=None) -> None:
    retry_policy = task.retry_policy or retry_policy      # 任务级策略优先于图级
    if task.timeout is not None:
        raise sync_timeout_unsupported(task.name)         # 同步节点不支持 timeout
    attempts = 0
    node_first_attempt_time = time.time()
    config = task.config
    if configurable is not None:
        config = patch_configurable(config, configurable)  # 注入 CONFIG_KEY_CALL
    # …… 把 node_first_attempt_time 写进 Runtime execution_info
    while True:
        # 每轮更新 node_attempt（1 起，用于观测/重试计数）
        config = patch_configurable(config, {CONFIG_KEY_RUNTIME: runtime.patch_execution_info(
            node_attempt=attempts + 1)})
        try:
            task.writes.clear()                 # ★ 清掉上一轮失败的残写
            return task.proc.invoke(task.input, config)   # 真的跑节点
        except ParentCommand as exc:
            ns = config[CONF][CONFIG_KEY_CHECKPOINT_NS]; cmd = exc.args[0]
            if cmd.graph in (ns, recast_checkpoint_ns(ns), task.name):
                for w in task.writers:          # 是给当前图的 Command -> 执行写入器
                    w.invoke(cmd, config)
                break
            elif cmd.graph == Command.PARENT:
                exc.args = (replace(cmd, graph=_checkpoint_ns_for_parent_command(ns)),)  # 上抛父图
            raise
        except GraphBubbleUp:
            raise                               # 中断/排空：直接结束，不重试
        except asyncio.CancelledError as exc:
            # ★ 同步节点无 asyncio 上下文，走到这必然是节点自己 raise 的
            raise NodeCancelledError(task.name) from exc
        except Exception as exc:
            if SUPPORTS_EXC_NOTES:
                exc.add_note(f"During task with name '{task.name}' and id '{task.id}'")
            if not retry_policy:
                raise
            matching_policy = None
            for policy in retry_policy:                       # 找第一个命中的策略
                if _should_retry_on(policy, exc):
                    matching_policy = policy; break
            if not matching_policy:
                raise
            attempts += 1                                     # attempts 只记「失败」
            if attempts >= matching_policy.max_attempts:
                raise                                         # 用尽 -> 放弃
            interval = matching_policy.initial_interval
            interval = min(matching_policy.max_interval,
                           interval * (matching_policy.backoff_factor ** (attempts - 1)))
            sleep_time = interval + random.uniform(0, 1) if matching_policy.jitter else interval
            time.sleep(sleep_time)
            logger.info(f"Retrying task {task.name} after {sleep_time:.2f} seconds "
                        f"(attempt {attempts}) after {exc.__class__.__name__} {exc}", exc_info=exc)
            config = patch_configurable(config, {CONFIG_KEY_RESUMING: True})   # 告诉子图「在重试」
```

退避序列（默认策略）：`0.5` → `1.0` → 用尽（`max_attempts=3`）。第 n 次失败后等待 `min(128, 0.5 * 2^(n-1))` 秒，再加 `uniform(0,1)` 抖动。

### 12.3 arun_with_retry：异步 + 超时

`arun_with_retry`（`_retry.py:685-838`）与同步版结构一致，多出：

```python
# _retry.py:694-752 — arun_with_retry（节选）
retry_policy = task.retry_policy or retry_policy
resolved_timeout = _resolve_timeout(task.timeout) if task.timeout is not None else None
# …… 缓存命中检查：若任务已因缓存跑过，直接返回
if match_cached_writes is not None and task.cache_key is not None:
    for t in await match_cached_writes():
        if t is task:
            return
while True:
    attempt_ctx = _start_timed_attempt(task, config, resolved_timeout) if resolved_timeout else None
    try:
        task.writes.clear()
        if resolved_timeout is None:
            if stream:
                async for _ in task.proc.astream(task.input, config): pass
                break
            return await task.proc.ainvoke(task.input, config)
        # ★ 有 timeout：走「任务 vs 看门狗」竞速
        result = await _arun_with_timeout(task, config, resolved_timeout, attempt_ctx, stream=stream)
        _finish_timed_attempt(config, attempt_ctx)
        if stream: break
        return result
    except asyncio.CancelledError as exc:
        # 见 §12.5：区分「框架取消」与「用户 raise」
        if _is_user_raised_cancelled():
            raise NodeCancelledError(task.name) from exc
        raise
    # …… 其余 except 与同步版一致（ParentCommand/GraphBubbleUp/Exception 退避）
```

`stream=self.use_astream` 时用 `task.proc.astream` 逐块消费（让 `stream_mode` 能在节点内即时吐 token），否则 `ainvoke`。

### 12.4 _arun_with_timeout：任务与看门狗竞速

```python
# _retry.py:422-517 — _arun_with_timeout（节选 + 注释）
async def _arun_with_timeout(task, config, timeout, attempt_ctx, *, stream) -> Any:
    run_timeout_s = timeout.run_timeout_secs
    idle_timeout_s = timeout.idle_timeout_secs
    # 进度回调：给「有观测器且配了 idle_timeout」的场景上报 progress 事件
    on_progress = ... 
    scope = _TimedAttemptScope(on_progress=on_progress,
        progress_min_interval=idle_timeout_s / 4 if idle_timeout_s is not None else 0.0,
        refresh_on=timeout.refresh_on)
    scoped_config = scope.wrap_config(config)   # ★ 用带守卫的 config 跑任务
    start = time.monotonic()
    if stream:
        async def run():
            async for _ in task.proc.astream(task.input, scoped_config):
                if timeout.refresh_on == "auto":
                    scope.touch()               # 有 token 产出 = 有进度
    else:
        async def run():
            return await task.proc.ainvoke(task.input, scoped_config)
    bg = create_task_in_config_context(run, scoped_config)
    watchdogs: dict[asyncio.Task, Literal["idle", "run"]] = {}
    if idle_timeout_s is not None:
        watchdogs[asyncio.create_task(scope.wait_for_idle_timeout(idle_timeout_s))] = "idle"
    if run_timeout_s is not None:
        watchdogs[asyncio.create_task(_run_timeout_watchdog(run_timeout_s))] = "run"
    try:
        done, _ = await asyncio.wait({bg, *watchdogs}, return_when=asyncio.FIRST_COMPLETED)
        if bg in done:                          # 任务先完成
            for w in watchdogs: w.cancel()
            return await bg
        for watchdog in done:                   # 看门狗先触发 -> 超时
            kind = watchdogs[watchdog]
            try:
                await watchdog
            except asyncio.TimeoutError as exc:
                elapsed = time.monotonic() - start
                scope.close(); task.writes.clear(); bg.cancel()
                bg.add_done_callback(_drain_cancelled)
                raise NodeTimeoutError(task.name, elapsed, kind=kind,
                    idle_timeout=idle_timeout_s, run_timeout=run_timeout_s) from exc
            raise RuntimeError(...)
    except asyncio.CancelledError:
        scope.close(); bg.cancel()
        for w in watchdogs: w.cancel()
        bg.add_done_callback(_drain_cancelled); raise
    finally:
        scope.close()
        for w in watchdogs: w.cancel()
```

**`idle_timeout` vs `run_timeout`**：

| 策略字段 | 语义 | 是否被「进度」重置 |
|---|---|---|
| `run_timeout` | 单次尝试的**硬墙钟上限** | **从不**重置（无论多少 token） |
| `idle_timeout` | 「多久没有可观测进度」的上限 | `refresh_on="auto"` 时被回调/写入/流/心跳重置；`"heartbeat"` 时只被 `runtime.heartbeat()` 重置 |

`_TimedAttemptScope.wrap_config`（`_retry.py:169-193`）是这套机制的实现核心：它把 config 里的 `CONFIG_KEY_SEND`、`CONFIG_KEY_STREAM`、`CONFIG_KEY_CALL`、`CONFIG_KEY_RUNTIME` 全部换成「守卫版」——任何一次**写入/流输出/子任务调度**都会 `touch()` 进度时钟；并且 `close()` 之后这些守卫会**拒绝后续写入**（`_guard_send` 检查 `self._active`），从而让「超时后仍在后台跑的协程」无法再污染状态。这是「超时 + 协作式取消」很难做对的地方，源码用「守卫 config + close 闸」解决。

**`NodeTimeoutError` 不继承内置 `TimeoutError`**（`errors.py:190-199`）：因为 `TimeoutError` 是 `OSError` 子类，会被 `default_retry_on` 的 `OSError -> False` 规则判为「不重试」。LangGraph 故意让它独立，从而**默认策略会重试超时**。

### 12.5 CancelledError 的语义消歧

```python
# _retry.py:315-334 — _is_user_raised_cancelled（完整 + 注释）
def _is_user_raised_cancelled() -> bool:
    """判断在飞的 CancelledError 是否来自任务体自己。
    Pregel 掐断兄弟任务时会调用 task.cancel()，这把 target.cancelling() 加 1；
    用户节点自己 raise asyncio.CancelledError 时 cancelling() == 0。"""
    if not SUPPORTS_TASK_CANCELLING:
        return False
    current = asyncio.current_task()
    if current is None:
        return False
    return current.cancelling() == 0
```

背景（`errors.NodeCancelledError` 的 docstring）：`asyncio.CancelledError` 是 `BaseException`，运行器把「被取消的 future」当作「静默拆除」。但**用户节点自己 `raise CancelledError`** 时，如果沿用同一路径，整个 run 会**静默报成功**。所以 `_retry` 层把它转成 `NodeCancelledError`（普通 `Exception`），让它走正常失败路径。这是「失败必须可见」的一条硬约束（呼应 MEMORY「子任务假成功」）。

### 12.6 ParentCommand：跨图写状态

```python
# _retry.py:544-570 — _checkpoint_ns_for_parent_command（节选 + 注释）
def _checkpoint_ns_for_parent_command(ns: str) -> str:
    """checkpoint 命名空间是 '|' 分隔的路径，通常每段形如 'name:task_id'，
    但运行期可能插入纯数字段（如 '|1'）来区分并发任务。纯数字段不是真实层级，计算父命名空间前要丢掉。"""
    parts = ns.split(NS_SEP)
    while parts and parts[-1].isdigit(): parts.pop()
    if parts: parts.pop()                    # 丢掉当前帧
    while parts and parts[-1].isdigit(): parts.pop()
    return NS_SEP.join(parts)
```

当子图节点返回 `Command(graph=Command.PARENT, update=...)` 时，`run_with_retry` 把 `cmd.graph` 从这个命名空间**换成父命名空间**再上抛（`_retry.py:627-629`），让父图的 writer 处理它。**这是「子图向父图写状态」的唯一通道**，也是 `update_state` 能在子图上工作的底层依据。

### 12.7 _should_retry_on

```python
# _retry.py:841-854 — _should_retry_on（完整 + 注释）
def _should_retry_on(retry_policy: RetryPolicy, exc: Exception) -> bool:
    if isinstance(retry_policy.retry_on, Sequence):
        return isinstance(exc, tuple(retry_policy.retry_on))    # 异常类列表
    elif isinstance(retry_policy.retry_on, type) and issubclass(retry_policy.retry_on, Exception):
        return isinstance(exc, retry_policy.retry_on)           # 单一异常类
    elif callable(retry_policy.retry_on):
        return retry_policy.retry_on(exc)                       # 自定义判定函数
    else:
        raise TypeError("retry_on must be an Exception class, a list or tuple of "
                        "Exception classes, or a callable")
```

`retry_policy` 是一个**序列**，`run_with_retry` 遍历它取「第一个命中的策略」——所以你可以配多条策略（如「对网络错误重试 3 次 + 对限流重试 8 次」），按顺序匹配。

---

---

## 十三、_checkpoint.py：检查点集成

`_checkpoint.py`（332 行）是超步与「持久化后端（checkpointer）」之间的适配层：**建检查点 / 从检查点复原通道 / 复制检查点 / DeltaChannel 快照节奏**。

| 函数 | 行号 | 作用 |
|---|---|---|
| `empty_checkpoint` | 28–36 | 全新线程的第一个空 checkpoint |
| `copy_checkpoint` | 322–331 | 深拷贝 checkpoint（保护调用方持有的副本） |
| `create_checkpoint` | 149–214 | 由「上一 checkpoint + 当前通道状态」造新 checkpoint |
| `channels_from_checkpoint` | 229–277 | 从 checkpoint 复原通道（含 DeltaChannel 祖先回溯） |
| `achannels_from_checkpoint` | 280–319 | 异步版 |
| `delta_channels_to_snapshot` | 50–71 | 判定哪些 DeltaChannel 该打全量快照（纯判定，无副作用） |
| `exit_delta_task_id` | 39–47 | exit 模式下 DeltaChannel 写入的合成 task_id |
| `create_checkpoint_plan_for_update_state_api` | 117–146 | `update_state` 的 checkpoint 规划 |

### 13.1 checkpoint 的结构与版本

```python
# _checkpoint.py:28-36 — empty_checkpoint（完整）
def empty_checkpoint() -> Checkpoint:
    return Checkpoint(
        v=LATEST_VERSION,                            # 当前 = 4
        id=str(uuid6(clock_seq=-2)),                 # uuid6：时间有序（利于 DB 排序）
        ts=datetime.now(timezone.utc).isoformat(),
        channel_values={},                           # 通道值快照
        channel_versions={},                         # 通道版本（§5.4 触发用）
        versions_seen={},                            # 各节点「已见版本」
    )
```

`Checkpoint` 三个核心字段与 §1.3 的版本机制一一对应。`v=LATEST_VERSION=4` 是**语义版本**：它会决定 task_id 用 `uuid5` 还是 `xxhash`（§5.4 的 `checkpoint["v"] > 1`），也是「跨版本迁移」的锚点。

### 13.2 create_checkpoint：快照与 DeltaChannel

```python
# _checkpoint.py:149-214 — create_checkpoint（节选 + 注释）
def create_checkpoint(checkpoint, channels, step, *, id=None, updated_channels=None,
                      get_next_version=None, channels_to_snapshot=None) -> Checkpoint:
    ts = datetime.now(timezone.utc).isoformat()
    channels_to_snapshot = channels_to_snapshot or set()
    if channels is None:
        values = checkpoint["channel_values"]
        channel_versions = checkpoint["channel_versions"]
    else:
        values = {}
        channel_versions = dict(checkpoint["channel_versions"])
        for k in channels:
            if k not in channel_versions:
                continue
            ch = channels[k]
            if k in channels_to_snapshot:
                # ★ 该通道要打全量快照：写一个 _DeltaSnapshot 包住的当前值
                if get_next_version is not None and (updated_channels is None or k not in updated_channels):
                    # exit 模式下「快照节奏到了但最后一步没写该通道」的补版本，
                    # 避免 saver.put() 因 new_versions 缺该通道而丢掉快照
                    channel_versions[k] = get_next_version(channel_versions[k], None)
                values[k] = _DeltaSnapshot(ch.get())
            else:
                v = ch.checkpoint()              # 普通通道：由通道自己决定 checkpooint 值
                if v is not MISSING:
                    values[k] = v                # MISSING 表示「本检查点不存该通道」
    return Checkpoint(
        v=LATEST_VERSION, ts=ts, id=id or str(uuid6(clock_seq=step)),  # id 由 step 时间有序生成
        channel_values=values, channel_versions=channel_versions,
        versions_seen=checkpoint["versions_seen"],
        updated_channels=None if updated_channels is None else sorted(updated_channels))
```

**普通通道 vs DeltaChannel 的区别**：普通通道（`LastValue`/`Topic`/`BinaryOperatorAggregate`）每次 checkpoint 直接序列化当前值；`DeltaChannel` 为了省空间**默认不存值**，而让「祖先回溯 + 重放写入」来重建（见 `channels_from_checkpoint`）。

### 13.3 channels_from_checkpoint：复原与祖先回溯

```python
# _checkpoint.py:229-277 — channels_from_checkpoint（节选 + 注释）
def channels_from_checkpoint(specs, checkpoint, *, saver=None, config=None):
    """从 checkpoint 复原通道。多数通道直接 spec.from_checkpoint(values[k])；
    DeltaChannel 若缺值，需要 saver.get_delta_channel_history 做祖先回溯，
    找到最近一个种子（_DeltaSnapshot 或迁移前的普通值），再累积其间的写入。"""
    channel_specs, managed_specs = {}, {}
    for k, v in specs.items():
        if isinstance(v, BaseChannel): channel_specs[k] = v
        else: managed_specs[k] = v
    # 需要回溯的 DeltaChannel：是 DeltaChannel 且当前 checkpoint 里没存值
    delta_channels = [k for k, spec in channel_specs.items()
                      if _needs_replay(spec, checkpoint["channel_values"].get(k, MISSING))]
    histories = {}
    if delta_channels and saver is not None and config is not None:
        # ★ 批量一次调用（避免 N 次 DB 往返）
        histories = saver.get_delta_channel_history(config=config, channels=delta_channels)
    channels = {}
    for k, spec in channel_specs.items():
        if k in histories:
            delta_spec = cast(DeltaChannel, spec); history = histories[k]
            replay_ch = delta_spec.from_checkpoint(history.get("seed", MISSING))
            replay_ch.replay_writes(history["writes"])   # 用祖先写入重放重建
            ch = replay_ch
        else:
            ch = spec.from_checkpoint(checkpoint["channel_values"].get(k, MISSING))
        channels[k] = ch
    return channels, managed_specs
```

`_needs_replay`（`_checkpoint.py:217-226`）的判断很直白：**只有同时满足「是 DeltaChannel」且「当前离线存储里没有值」才回溯**。`_DeltaSnapshot` 包和迁移前的普通值是「种子」，能直接解析；只有「不存在」才需要重建。

### 13.4 DeltaChannel 的快照节奏

```python
# _checkpoint.py:50-71 — delta_channels_to_snapshot（完整 + 注释）
def delta_channels_to_snapshot(channels, counters_since_delta_snapshot) -> set[str]:
    """返回「现在就应打全量快照」的 DeltaChannel 名集合。
    满足任一条件即快照：
      - 累积更新数 >= 该通道的 snapshot_frequency
      - 距上次快照的超步数 >= DELTA_MAX_SUPERSTEPS_SINCE_SNAPSHOT
    这是纯判定（不做任何修改）。"""
    result = set()
    for name, ch in channels.items():
        if not isinstance(ch, DeltaChannel) or not ch.is_available():
            continue
        updates, supersteps = counters_since_delta_snapshot.get(name, (0, 0))
        if updates >= ch.snapshot_frequency or supersteps >= DELTA_MAX_SUPERSTEPS_SINCE_SNAPSHOT:
            result.add(name)
    return result
```

**为什么要「定期打全量快照」**：DeltaChannel 只存增量，读取时要沿祖先链重放全部写入。若一直只增不减，回溯成本会线性增长。所以设置「写入条数阈值」+「超步数兜底阈值」双触发，定期把当前值凝成 `_DeltaSnapshot` 种子。这是「空间 vs 读取延迟」的工程折衷。

### 13.5 与 _loop.py 的协作

`_loop._put_checkpoint`（§4.9）的流程与这里的函数一一对应：

```text
_loop._put_checkpoint(metadata):
  1. delta_channels_to_snapshot(...)         # 决定哪些 DeltaChannel 打快照
  2. create_checkpoint(prev, channels, step, channels_to_snapshot=...)  # 造新 checkpoint
  3. copy_checkpoint(...)                    # 深拷贝交给 checkpointer
  4. checkpointer.put(config, checkpoint, metadata, new_versions)       # 落盘
```

进入 `PregelLoop.__enter__` 时反向调用 `channels_from_checkpoint` 复原通道。**「建/复原」两端对称**，保证「重放能拿到与上次启动完全一致的通道状态」。

---

## 十四、recursion_limit 与 GraphRecursionError

这是用户最常撞见的「运行期问题」之一，而它的实现分散在 `_loop.py`（判停）与 `main.py`（抛错）两处。

### 14.1 默认值与三个落点

```python
# langgraph/_internal/_config.py:32
DEFAULT_RECURSION_LIMIT = int(getenv("LANGGRAPH_DEFAULT_RECURSION_LIMIT", "10007"))
```

**LangGraph 1.2.9 的默认 `recursion_limit` 是 10007**（可用环境变量 `LANGGRAPH_DEFAULT_RECURSION_LIMIT` 覆盖）。这远大于旧版本（曾经的 25/50）——设计意图是「让循环/ReAct agent 不会被不合理的低限打断」，代价是「真死循环要跑很久才会停」。这个值由 `_internal/_config.py:335` 注入到每个 config：

```python
# _internal/_config.py:184-186, 225-226 — recursion_limit 的传播
elif key == "recursion_limit":
    if config["recursion_limit"] != DEFAULT_RECURSION_LIMIT:
        base["recursion_limit"] = config["recursion_limit"]     # 非默认值才向下继承
...
if recursion_limit is not None:
    config["recursion_limit"] = recursion_limit                 # 显式设置覆盖
```

要点：**非默认值会沿 config 向下继承到子图/子任务**；默认值不触发继承（省一层字典拷贝）。

### 14.2 判停：stop 的计算与 tick 的检查

```python
# _loop.py:1701（Sync）/ 1961（Async）— PregelLoop 入口处计算 stop
self.stop = self.step + self.config["recursion_limit"] + 1

# _loop.py:599-609 — tick() 的第一件事就是判停
def tick(self) -> bool:
    """执行一轮 Pregel 循环。返回 True 表示还需要下一轮。"""
    # check if iteration limit is reached
    if self.step > self.stop:
        self.status = "out_of_steps"     # ★ 不是抛错，而是打一个状态标记
        return False                     # 结束循环
    # prepare next tasks
    self.tasks = prepare_next_tasks(..., self.step, self.stop, ...)
```

**关键设计**：`tick()` 判停时**不抛异常**，只把 `status` 置为 `"out_of_steps"` 并返回 `False`。这样「已经跑完的这部分结果」仍能被正常输出（`loop.output` 已算好），异常留到 `main.py` 层的最后统一抛。这个「先标记、后抛错」的顺序保证：`stream()` 在抛 `GraphRecursionError` 之前，**先把最后一批 events 吐给调用方**（`main.py:2991-3011`）。

注意 `self.stop = self.step + recursion_limit + 1`：`step` 是「进入时已有的步数」（恢复场景下非 0），所以限值是**「本次运行允许的总步数」而非「剩余步数」**，且 `+1` 给了首步一个余量。

### 14.3 抛错：main.py 的两个位置

```python
# main.py:3001-3015 — stream()（同步）出口处
if loop.status == "out_of_steps":
    msg = create_error_message(
        message=(f"Recursion limit of {config['recursion_limit']} reached "
                 "without hitting a stop condition. You can increase the "
                 "limit by setting the `recursion_limit` config key."),
        error_code=ErrorCode.GRAPH_RECURSION_LIMIT)
    raise GraphRecursionError(msg)
elif loop.status == "draining":
    if loop.control is None:
        raise RuntimeError("Draining status requires run control")
    raise GraphDrained(loop.control.drain_reason or "shutdown")
run_manager.on_chain_end(loop.output)     # 正常结束：回调 on_chain_end
```

异步版在 `main.py:3483-3496` 完全镜像。注意 `_defaults` 里的前置校验（§2.4）：

```python
# main.py:2563-2564 — recursion_limit 必须 >= 1
if config["recursion_limit"] < 1:
    raise ValueError("recursion_limit must be at least 1")
```

### 14.4 GraphRecursionError 的类型与错误码

```python
# errors.py:67-87 — GraphRecursionError（完整 + 注释）
class GraphRecursionError(RecursionError):
    """图用尽了最大步数时抛出。用于防止死循环。
    想提高上限：在 config 里指定更高的 recursion_limit。

    示例：
        graph = builder.compile()
        graph.invoke({"messages": [("user", "Hello, world!")]},
                     {"recursion_limit": 1000})   # config 是第二个位置参数
    """
    pass

# errors.py:34-47 — 错误码与统一文案
class ErrorCode(Enum):
    GRAPH_RECURSION_LIMIT = "GRAPH_RECURSION_LIMIT"
    INVALID_CONCURRENT_GRAPH_UPDATE = "INVALID_CONCURRENT_GRAPH_UPDATE"
    INVALID_GRAPH_NODE_RETURN_VALUE = "INVALID_GRAPH_NODE_RETURN_VALUE"
    MULTIPLE_SUBGRAPHS = "MULTIPLE_SUBGRAPHS"
    INVALID_CHAT_HISTORY = "INVALID_CHAT_HISTORY"

def create_error_message(*, message, error_code):
    return (f"{message}\n"
            "For troubleshooting, visit: https://docs.langchain.com/oss/python/langgraph/"
            f"errors/{error_code.value}")
```

要点：**`GraphRecursionError` 继承 `RecursionError`（进而继承 `RuntimeError`）**。这有两个后果：

1. 老的 `except Exception` 能捕获它（它是 `Exception` 子类）；
2. 但它**不是** `GraphBubbleUp` 家族——所以**运行器不会把它当「中断信号」抑制**，它会正常冒泡成运行失败。

而 `create_error_message` 会自动附上文档链接，`ErrorCode.GRAPH_RECURSION_LIMIT` 指向官方的 `GRAPH_RECURSION_LIMIT` 排错页。

### 14.5 GraphBubbleUp 家族速查

失败/中断/控制流的异常都通过 `errors.py` 定义，理解它们的继承关系才不会「catch 错东西」：

```text
BaseException
├── Exception
│   ├── GraphBubbleUp                    # errors.py:50-51 —— 「信号类」异常的基类
│   │   ├── GraphInterrupt               # errors.py:102-107 —— 子图中断（顶层被抑制）
│   │   │   └── NodeInterrupt（@deprecated）  # errors.py:110-126 —— 旧 API
│   │   ├── GraphDrained                 # errors.py:54-64 —— 协作式排空（SIGTERM）
│   │   └── ParentCommand                # errors.py:129-133 —— Command.PARENT 上抛
│   ├── RecursionError
│   │   └── GraphRecursionError          # errors.py:67-87 —— ★ 超步耗尽（真失败）
│   ├── InvalidUpdateError               # errors.py:90-99 —— 非法写入（如写 TASKS）
│   ├── EmptyInputError                  # errors.py:136-139 —— 空输入
│   ├── TaskNotFound                     # errors.py:142-145 —— 分布式找不到任务
│   ├── NodeCancelledError               # errors.py:168-187 —— 用户 raise CancelledError
│   └── NodeTimeoutError                 # errors.py:190-241 —— 节点超时（可重试）
└── ...
```

| 异常 | 是 `GraphBubbleUp`？ | 运行器行为 | 用户该怎么做 |
|---|---|---|---|
| `GraphInterrupt` | 是 | 顶层图**抑制**（正常收尾）；子图**上抛** | 读 `state.tasks[].interrupts`，别 catch |
| `GraphDrained` | 是 | 抑制并落 checkpoint，可恢复 | 收到 SIGTERM 后的正常退出 |
| `ParentCommand` | 是 | 转成父图的 Command 写入 | 无需处理 |
| `GraphRecursionError` | **否** | **panic，运行失败** | 提高 `recursion_limit` 或修死循环 |
| `NodeTimeoutError` | 否 | 按 RetryPolicy 决定是否重试 | 配 `TimeoutPolicy` 或增大超时 |
| `NodeCancelledError` | 否 | 作为普通节点失败上报 | 别在自己的节点里 raise `CancelledError` |
| `InvalidUpdateError` | 否 | panic | 修写入（多半是写了保留通道） |

**「检测 HITL 审批必须读 `state.tasks[].interrupts`」的源码依据就在这里**：`GraphInterrupt` 在顶层被 `_suppress_interrupt` 吃掉（§4.8），调用方 `catch` 不到它，只能从状态快照读 `interrupts`。

---

---

## 十五、辅助模块

前十四章覆盖了执行引擎的主干。这一章收拢几个「支撑性」模块：`debug.py`、`_messages.py`、`protocol.py`、`types.py`、`_log.py`、`_config.py`、`_tools.py`。

### 15.1 debug.py：debug / checkpoints / tasks 流的事件负载

`debug.py`（302 行）把「任务、写入、checkpoint」翻译成 `stream_mode="debug"` 的结构化事件：

| 函数 | 行号 | 产出的流模式 | 内容 |
|---|---|---|---|
| `map_debug_tasks` | 41–71 | `"tasks"` / `"debug"` | 每个任务的 `{id, name, input, triggers, metadata}` |
| `map_debug_task_results` | 106–128 | `"tasks"` / `"debug"` | 任务结果 `{id, name, error, result, interrupts}` |
| `map_debug_checkpoint` | 144–206 | `"checkpoints"` / `"debug"` | checkpoint 负载 `{config, parent_config, values, metadata, next, tasks}` |
| `map_task_result_writes` | 83–103 | —— | 把同通道多次写入折叠成 `{"$writes": [...]}` |
| `tasks_w_writes` | 209–279 | —— | 把 pending writes / 子图状态附加到 `PregelTask`（`get_state()` 也用） |
| `get_colored_text` / `get_bolded_text` | 295–302 | —— | 终端彩色输出 |

```python
# debug.py:41-71 — map_debug_tasks（节选 + 注释）
def map_debug_tasks(tasks) -> Iterator[TaskPayload]:
    """为 stream_mode=debug 产出 "task" 事件。"""
    for task in tasks:
        if task.config is not None and TAG_HIDDEN in task.config.get("tags", []):
            continue                              # 隐藏任务不进 debug 流
        payload: TaskPayload = {"id": task.id, "name": task.name,
                                "input": task.input, "triggers": task.triggers}
        if task.config is not None:
            # 只转发「用户有意义」的元数据，剔除 langgraph 内部框架键
            md = {k: v for k, v in (task.config.get("metadata") or {}).items()
                  if k not in EXCLUDED_METADATA_KEYS}
            filtered_tags = filter_to_user_tags(task.config.get("tags"))
            if filtered_tags is not None:
                md["tags"] = filtered_tags
            if md:
                payload["metadata"] = md
        yield payload
```

```python
# debug.py:83-103 — map_task_result_writes（完整 + 注释）
def map_task_result_writes(writes) -> dict[str, Any]:
    """把任务写入折叠成结果 dict。同通道单条 -> {channel: write}；
    同通道多条 -> {channel: {'$writes': [w1, w2, ...]}}。"""
    result: dict[str, Any] = {}
    for channel, value in writes:
        existing = result.get(channel)
        if existing is not None:
            channel_writes = existing["$writes"] if is_multiple_channel_write(existing) \
                             else [existing]
            channel_writes.append(value)
            result[channel] = {"$writes": channel_writes}
        else:
            result[channel] = value
    return result
```

**`stream_mode="debug"` 是「学习 Pregel 执行」的最佳工具**：它把 §1.1 的 Plan/Execution/Update 三阶段显式物化成事件流。§16.1 的示例一会用它打印每一步。

### 15.2 _messages.py：stream_mode="messages"

`_messages.py`（461 行）实现「LLM 逐 token 流」：一个 `BaseCallbackHandler`，从 chat model 的 `on_llm_new_token` 与节点输出的 `on_chain_end` 两处收集 `BaseMessage`，再经 `stream((ns, "messages", (message, metadata)))` 吐出。

```python
# _messages.py:49-104 — StreamMessagesHandler（节选 + 注释）
class StreamMessagesHandler(BaseCallbackHandler, _StreamingCallbackHandler):
    """实现 stream_mode=messages 的回调处理器：收集
    (1) chat model 流事件；(2) 节点输出。"""
    run_inline = True     # ★ 主线程内联执行，避免顺序/加锁问题

    def __init__(self, stream, subgraphs, *, parent_ns=None):
        self.stream = stream
        self.subgraphs = subgraphs
        self.metadata: dict[UUID, Meta] = {}
        self.seen: set[int | str] = set()    # 去重：已发的 message.id
        self.parent_ns = parent_ns

    def _emit(self, meta, message, *, dedupe=False):
        if dedupe and message.id in self.seen: return
        if message.id is None:
            message.id = str(uuid4())        # 无 id 的临时补一个，供去重
        self.seen.add(message.id)
        self.stream((meta[0], "messages", (message, meta[1])))   # ★ 事件形态
```

关键处理点：

- **`on_chat_model_start`**（`_messages.py:130-149`）：记录 `run_id -> (ns, metadata)`；`subgraphs=False` 时**丢弃子图产生的事件**（`len(ns) > 0 and ns != self.parent_ns`）。
- **`on_llm_new_token`**（151–164）：从 `ChatGenerationChunk.chunk.message` 提取增量消息并 emit。
- **`on_llm_end`**（166–179）：emit 完整消息（`dedupe=True`，与流式的最后一个 chunk 去重）。
- **`on_chain_start`**（191–221）：把节点的**输入消息**记入 `seen`（这样节点「回显输入」时不会被当成新消息重复发）。
- **`on_chain_end`**（223–246）：把节点输出（可能是 `Command`、`Command` 列表、或直接 `BaseMessage`）里的消息 emit 出来。

`StreamMessagesHandlerV2`（`_messages.py:259+`）是 v2 变体：继承 `_V2StreamingCallbackHandler` 会翻转 `BaseChatModel.invoke` 走 `_stream_chat_model_events`（触发 `on_stream_event`）而非 `_stream`（触发 `on_llm_new_token`）。只有内部 `CONFIG_KEY_STREAM_MESSAGES_V2` 打开时才启用；直接 `graph.stream(stream_mode="messages")` 的调用方仍拿到 v1 的 `AIMessageChunk` 形态。

**本项目相关经验**：主 agent 的 token 流式链路（前端 `useChat` → `messages-tuple`）依赖这套 handler；子 agent 不流式，正是因为没有为子图注册该 handler/或 `subgraphs=False`。

### 15.3 protocol.py：抽象接口

`protocol.py`（288 行）定义两个**接口**（不是实现）：

```python
# protocol.py:25-269 — PregelProtocol（骨架 + 注释）
class PregelProtocol(Runnable[InputT, Any], Generic[StateT, ContextT, InputT, OutputT]):
    """所有「像图一样工作的对象」的抽象接口。Pregel 与 RemoteGraph 都实现它。"""
    @abstractmethod
    def with_config(self, config=None, **kwargs) -> Self: ...
    @abstractmethod
    def get_graph(self, config=None, *, xray=False) -> DrawableGraph: ...
    @abstractmethod
    async def aget_graph(self, config=None, *, xray=False) -> DrawableGraph: ...
    @abstractmethod
    def get_state(self, config, *, subgraphs=False) -> StateSnapshot: ...
    @abstractmethod
    async def aget_state(self, config, *, subgraphs=False) -> StateSnapshot: ...
    @abstractmethod
    def get_state_history(self, config, *, filter=None, before=None, limit=None) -> Iterator: ...
    @abstractmethod
    def bulk_update_state(self, config, updates) -> RunnableConfig: ...
    @abstractmethod
    def update_state(self, config, values, as_node=None) -> RunnableConfig: ...
    @abstractmethod
    def stream(self, input, config=None, *, context=None, stream_mode=None,
               interrupt_before=None, interrupt_after=None, subgraphs=False,
               version="v1") -> Iterator: ...
    @abstractmethod
    def astream(self, ...) -> AsyncIterator: ...
    @abstractmethod
    def invoke(self, input, config=None, *, context=None, ...) -> GraphOutput: ...
    @abstractmethod
    async def ainvoke(self, ...) -> GraphOutput: ...
```

```python
# protocol.py:272-288 — StreamChunk 与 StreamProtocol（完整）
StreamChunk = tuple[tuple[str, ...], str, Any]   # (namespace, mode, payload)

class StreamProtocol:
    """流「协议」的轻量封装：一个可调用对象 + 它支持的 mode 集合。"""
    __slots__ = ("modes", "__call__")
    modes: set[StreamMode]
    __call__: Callable[[Self, StreamChunk], None]

    def __init__(self, __call__, modes):
        self.__call__ = ...      # 存成实例属性（__slots__ 里显式声明）
        self.modes = modes
```

要点：**`StreamChunk = (namespace, mode, payload)`** 是流事件的统一三元组——`namespace` 是子图路径（`()` 表示顶层），`mode` 是 `values/updates/messages/...`，`payload` 是具体负载。所有流消费端（`_output`、SDK、LangGraph Server）都用这个形态。**`StreamProtocol` 同时携带「能吐哪些 mode」**，让 `stream_mode` 的过滤不必等到运行时。

### 15.4 types.py 与 _log.py

```python
# types.py:1-14 — 纯 deprecation re-export（完整）
"""Re-export types moved to langgraph.types"""
from langgraph.types import (
    All, CachePolicy, PregelExecutableTask, PregelTask, RetryPolicy,
    StateSnapshot, StateUpdate, StreamMode, StreamWriter, default_retry_on)

# types.py:29-38 — 导入即告警
from warnings import warn
from langgraph.warnings import LangGraphDeprecatedSinceV10
warn("Importing from langgraph.pregel.types is deprecated. "
     "Please use 'from langgraph.types import ...' instead.",
     LangGraphDeprecatedSinceV10, stacklevel=2)
```

**`pregel/types.py` 已不是定义处，只是转发+弃用警告**。真正定义在 `langgraph/types.py`（`PregelExecutableTask`、`RetryPolicy`、`TimeoutPolicy`、`Command`、`Send`、`Interrupt`、`StateSnapshot`……全在那儿）。任何 `from langgraph.pregel.types import X` 都会打弃用警告——请改用 `from langgraph.types import X`。

```python
# _log.py:1-3 — 全文
import logging
logger = logging.getLogger("langgraph")
```

**Pregel 全包共用一个 logger `"langgraph"`**。想让日志冒出来（如「Retrying task ... after ...」），需要配置该 logger 的 handler/level，或在应用层 `logging.basicConfig(level=logging.INFO)`。这解释了「为什么 `_retry` 里 `logger.info` 的『重试』信息在默认配置下看不到」。

`_config.py` 是 **0 行空文件**（内容已迁到 `langgraph/_internal/_config.py`）；`_tools.py` 是 `stream_mode="tools"` 的 `StreamToolCallHandler`（工具调用流），与 `_messages.py` 同构，此处不展开。

### 15.5 辅助模块职责速查

| 文件 | 行数 | 一句话职责 | 关键导出 |
|---|---|---|---|
| `debug.py` | 302 | debug/checkpoints/tasks 流事件构造 | `map_debug_tasks` / `map_debug_checkpoint` |
| `_messages.py` | 461 | `stream_mode="messages"`（LLM 逐 token） | `StreamMessagesHandler` / `StreamMessagesHandlerV2` |
| `_tools.py` | —— | `stream_mode="tools"`（工具调用流） | `StreamToolCallHandler` |
| `protocol.py` | 288 | 图对象的抽象接口 + 流协议 | `PregelProtocol` / `StreamProtocol` / `StreamChunk` |
| `types.py` | 38 | 类型转发 + 弃用警告（→ `langgraph.types`） | 全部 re-export |
| `_log.py` | 3 | 统一 logger | `logger` |
| `_config.py` | 0 | 空（已迁到 `_internal/_config.py`） | —— |

---

## 十六、完整代码示例

以下三个示例都在「可运行的最小图」上，用 `stream_mode`/`RetryPolicy`/`recursion_limit` 三个杠杆，把前十四章的机制**看得见**。示例基于 LangGraph 1.2.9（本项目 `.venv`），并给出每个示例对应观察到的源码行为。

### 16.1 示例一：用 stream_mode="debug" 观察 superstep

目标：把 §1.1 的 **Plan → Execution → Update** 三阶段显式打印出来，观察「一步内并行、步末统一提交」。

```python
# demo_debug_stream.py —— 观察 superstep 执行
from typing import Annotated, TypedDict
from operator import add
from langgraph.graph import StateGraph, START, END

# 状态：两个字段都用 add 作为 reducer（便于观察并行写入如何汇聚）
class State(TypedDict):
    a: Annotated[list[int], add]     # 节点写入用列表，reducer 累积
    b: Annotated[list[int], add]

def node_a(state: State) -> dict:
    # 只做一件可观察的事：往 a 里加一个标记
    print(f"[node_a] 输入 a={state['a']} b={state['b']}")
    return {"a": ["from_a"]}

def node_b(state: State) -> dict:
    print(f"[node_b] 输入 a={state['a']} b={state['b']}")
    return {"b": ["from_b"]}

builder = StateGraph(State)
builder.add_node("a", node_a)
builder.add_node("b", node_b)
# fan-out：START 同时连到 a 与 b（同一步并行）
builder.add_edge(START, "a")
builder.add_edge(START, "b")
builder.add_edge("a", END)
builder.add_edge("b", END)
graph = builder.compile()

# 关键：stream_mode="debug" 会同时吐出 checkpoints/tasks 等结构化事件
for event in graph.stream({"a": [], "b": []}, stream_mode="debug"):
    etype = event["type"]
    if etype == "checkpoint":
        # ★ Update 阶段后的事件：包含本步的 next（下步待跑节点）与 values（完整状态）
        print(f"--- checkpoint step={event['step']} next={event['next']} "
              f"values={event['values']}")
    elif etype == "task":
        # ★ Plan 阶段的事件：本步被选中的任务（含 triggers，验证「版本驱动触发」）
        print(f"    task name={event['payload']['name']} "
              f"triggers={event['payload']['triggers']}")
    elif etype == "task_result":
        # ★ Execution 阶段的事件：任务结果（含写入的通道值）
        p = event["payload"]
        print(f"    task_result name={p['name']} result={p['result']}")
```

**预期输出与解读**（`debug` 模式的典型形态）：

```text
--- checkpoint step=0 next=['a', 'b'] values={'a': [], 'b': []}   # 首步：Plan 选出 a、b
    task name=a triggers=['branch:to:a']                          # a 的触发源是 START 的写入
    task name=b triggers=['branch:to:b']                          # b 同上
    task_result name=a result={'a': ['from_a']}                   # a 完成，写入 a 通道
    task_result name=b result={'b': ['from_b']}                   # b 同一步完成
--- checkpoint step=1 next=[] values={'a': ['from_a'], 'b': ['from_b']}  # Update：step1 已汇聚
```

三个可直接观察到的源码事实：

1. **Plan 一次性列出 a、b 两个任务** ⇒ 对应 `prepare_next_tasks`（§5.2）在一步里同时返回 PULL/PUSH 任务；
2. **a、b 在同一个 `step` 事件里出现** ⇒ 同一步并行（BSP 的 bulk synchronous）；
3. **step=1 的 `values` 里 a、b 都有值** ⇒ 步末 `apply_writes`（§5.5）把两个任务的写入**原子汇聚**；若把某个节点改成 `time.sleep(3)`，另一个节点仍会在同一步开始（不会被等），但结果永远在步末一起可见。

想进一步看「谁触发了谁」，关注 `triggers`：`branch:to:a` 是 START 写入的特殊通道（§9.3），证明**「边」在运行时就是「通道写入」**。

### 16.2 示例二：节点重试策略（RetryPolicy）

目标：验证 §12 的「退避序列、`retry_on` 过滤、`NodeTimeoutError` 可重试」。

```python
# demo_retry.py —— RetryPolicy 配置与观察
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import RetryPolicy

class State(TypedDict):
    tries: int

_attempts = {"n": 0}

def flaky(state: State) -> dict:
    """前两次抛连接类错误（默认策略会重试），第三次成功。"""
    _attempts["n"] += 1
    print(f"[flaky] 第 {_attempts['n']} 次尝试")
    if _attempts["n"] < 3:
        # ConnectionError 属于 default_retry_on 的「重试」类
        raise ConnectionError("模拟网络抖动")
    return {"tries": _attempts["n"]}

builder = StateGraph(State)
# ★ 自定义策略：最多 4 次尝试、首次间隔 0.2s、回退因子 2、关闭抖动
builder.add_node("flaky", flaky, retry_policy=RetryPolicy(
    initial_interval=0.2,      # 0.2s
    backoff_factor=2.0,        # 0.2 -> 0.4 -> 0.8
    max_interval=5.0,          # 单次间隔上限
    max_attempts=4,            # 含首次，共 4 次
    jitter=False,              # 关闭随机抖动，便于观察确定序列
    # retry_on 省略 -> 用 default_retry_on（ConnectionError 会重试，ValueError 不会）
))
builder.add_edge(START, "flaky")
builder.add_edge("flaky", END)
graph = builder.compile()

result = graph.invoke({"tries": 0})
print("最终结果:", result)      # {'tries': 3}
```

**对应源码行为**：

- **退避序列**：0.2 → 0.4（第 1 次失败后等 0.2s，第 2 次失败后等 0.4s），对应 `_retry.py:663-668` 的 `interval = min(max_interval, initial_interval * backoff_factor**(attempts-1))`。
- **`max_attempts=4`** 表示「最多 4 次尝试」；第 3 次成功，所以只失败 2 次，`attempts` 到 2 就退出了。
- **`retry_on` 默认值**：若把 `ConnectionError` 换成 `ValueError`，则**一次都不会重试**（`default_retry_on` 对 `ValueError` 返回 False）。
- **日志**：把 `logging.getLogger("langgraph").setLevel(logging.INFO)` 打开后，能看到 `Retrying task flaky after 0.20 seconds (attempt 1) after ConnectionError ...`（§12.2 的 `logger.info`）。
- **超时可重试**：若给异步节点配 `TimeoutPolicy(idle_timeout=5)`，超时抛 `NodeTimeoutError`（**不继承 `TimeoutError`**），默认策略会重试它（§12.4 的要点）。

自定义「只重试特定异常」的写法：

```python
# 只对「限流」类错误重试，其它一律不重试
class RateLimitedError(Exception): ...

builder.add_node("llm_call", llm_call, retry_policy=RetryPolicy(
    max_attempts=8, initial_interval=1.0, backoff_factor=1.5, max_interval=30.0,
    retry_on=RateLimitedError,          # ★ 只有这个类会触发重试
))
# 或按状态码自定义判定：
builder.add_node("http_call", http_call, retry_policy=RetryPolicy(
    retry_on=lambda exc: getattr(exc, "status", 0) in (429, 503),
))
```

`retry_on` 的三种形态（异常类 / 异常类序列 / callable）由 `_should_retry_on`（§12.7）统一分派。

### 16.3 示例三：recursion_limit 触发与 GraphRecursionError

目标：制造一个真循环，观察 §14 的「判停 → 状态标记 → 统一抛错 → 中途结果先输出」。

```python
# demo_recursion.py —— recursion_limit 与 GraphRecursionError
from typing import Annotated, TypedDict
from operator import add
from langgraph.graph import StateGraph, START, END
from langgraph.errors import GraphRecursionError

class State(TypedDict):
    n: Annotated[int, add]       # 每轮 +1，永不满足停止条件

def step_forever(state: State) -> dict:
    print(f"[step_forever] n={state['n']}")
    return {"n": 1}              # 永远 +1 -> 死循环

def route(state: State) -> str:
    return "loop"                # 永远回到自己

builder = StateGraph(State)
builder.add_node("loop", step_forever)
builder.add_edge(START, "loop")
builder.add_conditional_edges("loop", route, {"loop": "loop"}, )
graph = builder.compile()

try:
    # ★ 把 recursion_limit 压到很小（默认是 10007，跑完要很久）
    graph.invoke({"n": 0}, {"recursion_limit": 5})
except GraphRecursionError as e:
    print("捕获到 GraphRecursionError：")
    print(str(e))
    # 典型文案：
    # Recursion limit of 5 reached without hitting a stop condition.
    # You can increase the limit by setting the `recursion_limit` config key.
    # For troubleshooting, visit: https://docs.langchain.com/oss/python/langgraph/
    # errors/GRAPH_RECURSION_LIMIT
```

**「中途结果先输出」的验证**：改用 `stream()` 而非 `invoke()`：

```python
# 观察「抛错前，前面的 events 已经吐出来了」
count = 0
try:
    for chunk in graph.stream({"n": 0}, {"recursion_limit": 5}, stream_mode="updates"):
        count += 1
        print(f"event #{count}: {chunk}")     # 这些 event 在 GraphRecursionError 之前就打印了
except GraphRecursionError as e:
    print(f"已收到 {count} 个 event 后，才抛 GraphRecursionError")
```

这直接对应 §14.2 的设计：**`tick()` 判停只置 `status="out_of_steps"` 并返回 `False`，异常留到 `main.py` 出口统一抛**——所以 `stream()` 能先把最后一批（乃至全部）事件交付给调用方。

**正确处理「合法地需要很多步」的场景**：

```python
# 显式提高上限（会沿 config 继承到子图，见 §14.1）
graph.invoke(input, {"recursion_limit": 1000})

# 或在 compile 时给默认 config
graph = builder.compile().with_config({"recursion_limit": 500})

# 或走环境变量给「整个进程」设默认值
# 启动前：export LANGGRAPH_DEFAULT_RECURSION_LIMIT=2000
```

三个注意点：

1. **默认 10007 很大**：真死循环时不会「立刻」报错，而是跑很久——生产环境建议对「已知有界」的图显式调低，对 ReAct 类图显式给足。
2. **`GraphRecursionError` 不是 `GraphBubbleUp`**：`except Exception` 能抓到它，但**不能**用「catch `GraphBubbleUp` 来吞中断」的那套逻辑来吞它。
3. **恢复场景下的语义**：`self.stop = self.step + recursion_limit + 1`（§14.2），所以从 checkpoint 恢复时，`recursion_limit` 是「总步数预算」而非「剩余步数」——如果恢复后还需要很多步，要显式调大。

---

---

## 十七、总结与排错速查

### 17.1 一句话总结

**LangGraph 的执行引擎 = 一个「版本驱动的 BSP 超步循环」**：`prepare_next_tasks` 用通道版本比较选出该跑的任务（Plan），`PregelRunner` 把它们并发丢进线程池/事件循环执行（Execution），步末 `apply_writes` 原子提交所有写入（Update）。函数式 API 只是把「待办任务」的载体从 `Send` 换成 `Call`，其余机制（调度、并发、检查点、重试、流）全部复用。

### 17.2 模块职责速查表（pregel/ 全 20 文件）

| 文件 | 行数 | 核心类/函数 | 一句话职责 |
|---|---|---|---|
| `__init__.py` | 3 | —— | 导出 `Pregel` / `NodeBuilder` |
| `main.py` | 4364 | `Pregel` / `NodeBuilder` | 主类：构造、validate、stream/invoke、update_state |
| `_loop.py` | 1988 | `PregelLoop` / `AsyncPregelLoop` | 超步主循环、checkpoint 落盘、流输出、interrupt 抑制 |
| `_algo.py` | 1460 | `prepare_next_tasks` / `apply_writes` | 调度算法（Plan + Update barrier） |
| `_runner.py` | 941 | `PregelRunner` | 单超步并发执行、commit、panic、函数式调用 |
| `_executor.py` | 223 | `BackgroundExecutor` / `AsyncBackgroundExecutor` | 线程池 / asyncio 任务 + 信号量 |
| `_retry.py` | 854 | `run_with_retry` / `arun_with_retry` | 退避重试 + TimeoutPolicy 看门狗 |
| `_call.py` | 298 | `call` / `get_runnable_for_task` / `SyncAsyncFuture` | 函数式 API 与对象标识 |
| `_io.py` | 174 | `map_input` / `map_command` / `map_output_*` | 输入输出与流模式映射 |
| `_read.py` | 298 | `ChannelRead` / `PregelNode` | actor 容器 + 读状态 |
| `_write.py` | 192 | `ChannelWrite` / `ChannelWriteEntry` | 写通道的 Runnable |
| `_validate.py` | 120 | `validate_graph` / `validate_keys` | 编译期合法性检查 |
| `_checkpoint.py` | 332 | `create_checkpoint` / `channels_from_checkpoint` | 检查点建/复原 + DeltaChannel |
| `_messages.py` | 461 | `StreamMessagesHandler` | `stream_mode="messages"` |
| `_tools.py` | —— | `StreamToolCallHandler` | `stream_mode="tools"` |
| `debug.py` | 302 | `map_debug_*` | `stream_mode="debug"` 事件负载 |
| `protocol.py` | 288 | `PregelProtocol` / `StreamProtocol` | 图对象抽象接口 + 流协议 |
| `types.py` | 38 | —— | 类型转发 + 弃用警告 |
| `_log.py` | 3 | `logger` | 统一 logger（`"langgraph"`） |
| `_config.py` | 0 | —— | 空（已迁到 `_internal/_config.py`） |

### 17.3 十个关键不变量

1. **步内不可见、步末统一提交**：任务写入先堆 `task.writes`（deque），`after_tick` 才 `apply_writes`（§4.5、§5.5）。
2. **版本驱动触发**：`channel_versions[chan] > versions_seen[node][chan]` 才触发，**严格大于**（§5.4）。
3. **task_id 内容寻址**：`hash(checkpoint, ns, step, name, kind, triggers)`，同一逻辑输入永远同一 ID（§5.4）。
4. **`updated_channels` / `trigger_to_nodes` 只是加速**，不改变触发语义（§5.2）。
5. **`CONFIG_KEY_SEND = deque.extend`**：线程安全、不即时生效（§5.7）。
6. **顶层图吞 `GraphInterrupt`**，子图上抛；检测中断只能读 `state.tasks[].interrupts`（§4.8、§14.5）。
7. **失败即取消兄弟**；中断合并成一个 `GraphInterrupt`（§6.7）。
8. **`recursion_limit` 判停只置 `status="out_of_steps"`**，异常留到 `main.py` 出口抛（§14.2）。
9. **`NodeTimeoutError` 不继承 `TimeoutError`**（否则会被 `default_retry_on` 判为不重试）（§12.4）。
10. **用户 raise 的 `CancelledError` 必须转成 `NodeCancelledError`**，否则 run 会静默报成功（§12.5）。

### 17.4 排错速查

| 症状 | 最可能原因 | 定位手段 | 修复 |
|---|---|---|---|
| 图「卡住」不动 | 没有节点被触发（版本未推高）；或输入无消费者 | `stream_mode="debug"` 看 `task` 事件是否为空 | 检查边的目标、`Command(goto=...)` 拼写 |
| 报 `GraphRecursionError` | 真死循环，或图天然需要 > limit 步 | 看错误里的 `Recursion limit of N` | 调大 `recursion_limit`；修停止条件 |
| 节点日志看不到「重试」 | logger `"langgraph"` 未配置 | `logging.getLogger("langgraph").setLevel(INFO)` | 配置 logging（§15.4） |
| 节点失败但整图「静默成功」 | 用户节点 raise 了 `CancelledError`（被当拆除） | 看有无 `NodeCancelledError`（1.2.x 已修） | 别 raise `CancelledError`；用普通异常 |
| 编译期 `Channel name 'X' is reserved` | 用了保留名（`TASKS`/`__pregel_*`/`branch:to:*`） | §11.2 表 | 换名 |
| 编译期 `Input channel X is not subscribed to by any node` | 输入 schema 有无人消费的键 | §11.2 | 删掉或接上消费者 |
| 编译期 `Node X not in nodes`（interrupt_before/after） | 节点名拼错 | 对一遍 `builder.nodes` | 改对名字 |
| 同超步内读到别的节点的写入 | 违反 BSP 假设 | 看是否有跨节点共享的可变对象 | 用 `local_read(fresh=True)` 只读自己的；跨步用状态通道 |
| 缓存「命中错对象」 | 两个内容相同/`__module__` 为 None 的函数共用 `"__dynamic__"` 桶 | §8.1 | 给函数显式命名/模块；或关缓存 |
| 并发上不去 | `max_concurrency` 限图中并发；全局 run 槽位限跨 run | 分别查 `config["max_concurrency"]` 与运行时槽位 | 分层调参（§7.2） |
| 超时「不生效」 | 给同步节点配了 `TimeoutPolicy` | 编译期 `sync_timeout_unsupported` | 改异步节点，或把同步工作丢线程池（§8.3） |
| 中断后 `get_state()` 看不到审批 | 读了 `state.values` 而非 `state.tasks` | §14.5 | 读 `state.tasks[].interrupts` |
| DeltaChannel 读取越来越慢 | 增量链太长，未打全量快照 | `delta_channels_to_snapshot`（§13.4） | 调低 `snapshot_frequency` |

### 17.5 与项目经验的对照

把 §17.3 的不变量/§17.4 的速查对上本项目的既有结论，能加深理解（这些是**同一套机制**在生产中的表现）：

- **「重复问同一题不会重跑工具」** ⇒ 节点缓存（`CachePolicy` + `identifier`，§8.1）与「模型复用上轮结论」叠加；`stream_mode="updates"` 会带 `{"__metadata__": {"cached": True}}`（§9.4）。
- **「切会话会把在跑的 run 打死」** ⇒ 运行器在 `on_disconnect` 时对任务 `cancel()`；异步执行器 `__aexit__` 的 `task.cancel(self.sentinel)`（§7.2）就是这条链的底层。
- **「失败熔断/规划期 not-found 被无谓重试」** ⇒ 重试策略的 `retry_on` 覆盖范围（§12.1）：`RuntimeError` 默认**不重试**，但业务里「not found」常被包成自定义异常从而落入「其余 -> 重试」分支——这正是需要自定义 `retry_on` 的地方。
- **「并发天花板 = 全局 10 个 run 槽」** ⇒ 与 §7.2 的 `max_concurrency`（图内节点并发）是**两个层级**：前者限「多少个 run 同时跑」，后者限「一个 run 内多少节点同时跑」。
- **「子任务假成功：失败信号机读化」** ⇒ §6.5 的 `ERROR` 通道写入与 §6.8 的错误处理器路由：失败必须落到机读信号，不能只靠异常冒泡。
- **「trace 归并绑定落盘 / 重启分裂」** ⇒ `_checkpoint` 的 `channel_values`/`versions_seen` 持久化与「按线程绑定 checkpointer」（§13）。
- **「HITL interrupt 被 top-level graph 抑制」** ⇒ §4.8 + §14.5：`GraphInterrupt` 只在**非嵌套**图被吞，子图上抛为父任务的 interrupt 写入；检测审批必须读 `state.tasks[].interrupts`。

### 17.6 推荐阅读顺序（源码）

若是第一次读 `pregel/`，建议按「数据流」而非「文件顺序」：

```text
1. main.py 的 Pregel 类 docstring（Plan/Execution/Update）      # 建立心智模型
2. _loop.py 的 tick() / after_tick()                            # 超步循环骨架
3. _algo.py 的 prepare_next_tasks / apply_writes                # 调度与 barrier
4. _runner.py 的 tick() / commit()                              # 并发与写入落盘
5. _read.py / _write.py 的 PregelNode / ChannelWrite            # 演员的读写
6. _io.py 的 map_*                                              # 输入输出与流模式
7. _executor.py / _call.py                                      # 执行器与函数式 API
8. _retry.py / _validate.py / _checkpoint.py                    # 重试、验证、持久化
9. debug.py / _messages.py / protocol.py                        # 可观测性
```

配合 **§16.1 的 `stream_mode="debug"` 示例**边读边跑，能把「源码里的函数」和「运行时的实际事件」一一对上——这是理解 Pregel 最快的路径。
