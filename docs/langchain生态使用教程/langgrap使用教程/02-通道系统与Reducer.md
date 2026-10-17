# LangGraph 通道系统与 Reducer 详解教程

> 基于本项目 `.venv` 内**实际安装**的源码编写（`langgraph 1.2.9`）：
> - `langgraph/channels/__init__.py`（通道包出口，9 个公开通道类）
> - `langgraph/channels/base.py`（`BaseChannel` 抽象基类）
> - `langgraph/channels/last_value.py`（`LastValue` / `LastValueAfterFinish`）
> - `langgraph/channels/binop.py`（`BinaryOperatorAggregate`，即 reducer 通道）
> - `langgraph/channels/topic.py`（`Topic` 发布订阅通道）
> - `langgraph/channels/any_value.py`（`AnyValue`）
> - `langgraph/channels/ephemeral_value.py`（`EphemeralValue`）
> - `langgraph/channels/delta.py`（`DeltaChannel`，Beta）
> - `langgraph/channels/named_barrier_value.py`（`NamedBarrierValue` / `NamedBarrierValueAfterFinish`）
> - `langgraph/channels/untracked_value.py`（`UntrackedValue`）
> - `langgraph/managed/__init__.py` + `langgraph/managed/base.py` + `langgraph/managed/is_last_step.py`（ManagedValue 体系）
> - `langgraph/_internal/_fields.py`（状态字段默认值 / 注解处理）
> - 交叉引用：`langgraph/graph/state.py`（`_get_channels` / `_get_channel`，Annotated → 通道映射）、`langgraph/pregel/_algo.py`（`apply_writes`，通道生命周期驱动）、`langgraph/pregel/_checkpoint.py`（`create_checkpoint` / `channels_from_checkpoint`）、`langgraph/checkpoint/serde/types.py`（`_DeltaSnapshot`）、`langgraph/graph/message.py`（`add_messages`）、`langgraph/types.py`（`Overwrite`）

---

## 目录

1. [概述：Pregel 运行时的通道模型](#1-概述pregel-运行时的通道模型)
2. [BaseChannel 基类详解](#2-basechannel-基类详解)
3. [LastValue：默认单值通道](#3-lastvalue默认单值通道)
4. [BinaryOperatorAggregate：Reducer 通道的本体](#4-binaryoperatoraggregatereducer-通道的本体)
5. [Annotated[T, reducer] 如何映射到通道](#5-annotatedt-reducer-如何映射到通道)
6. [Topic：发布订阅通道](#6-topic发布订阅通道)
7. [AnyValue：任意值通道](#7-anyvalue任意值通道)
8. [EphemeralValue：步级临时值](#8-ephemeralvalue步级临时值)
9. [DeltaChannel：增量重放通道（Beta）](#9-deltachannel增量重放通道beta)
10. [NamedBarrierValue：屏障同步通道](#10-namedbarriervalue屏障同步通道)
11. [UntrackedValue：不进 checkpoint 的值](#11-untrackedvalue不进-checkpoint-的值)
12. [ManagedValue 托管值体系](#12-managedvalue-托管值体系)
13. [状态字段处理：_fields.py](#13-状态字段处理_fieldspy)
14. [Reducer 函数设计模式](#14-reducer-函数设计模式)
15. [通道的序列化与 Checkpoint](#15-通道的序列化与-checkpoint)
16. [通道对比总表](#16-通道对比总表)
17. [完整代码示例](#17-完整代码示例)
18. [常见陷阱与调试](#18-常见陷阱与调试)

---

## 1. 概述：Pregel 运行时的通道模型

### 1.1 什么是「通道」

LangGraph 的执行引擎是 Pregel（BSP，整体同步并行模型）。在这个模型里，**节点之间从不直接传数据**——它们只做两件事：

1. **读**：订阅（trigger on）一组通道，当这些通道「可用」（available，即 `get()` 不抛 `EmptyChannelError`）且版本比上次见过的新时，节点被调度；
2. **写**：节点返回的字典 `{"key": value}` 被翻译成一组 `(channel, value)` 写入（write），先攒着，**步末统一提交**。

**通道（Channel）就是这两个动作之间的共享存储单元**。你写的 `StateGraph(State)` 里，`State` 这个 TypedDict/dataclass/Pydantic 模型的**每一个字段都会被编译成一个同名通道**。所以「状态」在运行时并不是一个 dict，而是一排各有脾气（覆盖、累加、发布订阅、屏障……）的通道对象；dict 形态的状态只是「把所有非空通道 `get()` 出来拼成的视图」。

```
        ┌───────────────────── 一个超步（super-step） ─────────────────────┐
        │                                                                  │
  调度   │   prepare_next_tasks: 通道可用+版本更新 ⇒ 生成任务                │
        │        │                                                         │
  执行   │        ▼   节点并行运行，产出 writes=[(chan, val), ...]          │
        │        │                                                         │
  提交   │        ▼   apply_writes: consume → update(vals) →               │
        │                        update(EMPTY_SEQ) → finish                │
        │        │                                                         │
  持久化 │        ▼   create_checkpoint: 每个通道 checkpoint() ⇒            │
        │                        channel_values（MISSING 的不落盘）         │
        └──────────────────────────────────────────────────────────────────┘
```

### 1.2 「Reducer」到底是什么

大家熟悉的写法：

```python
class State(TypedDict):
    messages: Annotated[list, add_messages]   # reducer
    count: int                                # 无 reducer
```

这里的 `add_messages` 不是魔法，它在编译期被识别为一个**二元归约函数**，于是 `messages` 字段被映射成 `BinaryOperatorAggregate` 通道；没有 reducer 的 `count` 字段被映射成 `LastValue` 通道。**Reducer 的本质就是「同一个超步里多路写入如何折叠成一个值」以及「新写入如何与旧值合并」的规则**，这个规则被封装在通道对象的 `update()` 方法里。

`langgraph/channels/__init__.py` 列出了 1.2.9 的全部公开通道：

```python
__all__ = (
    # base
    "BaseChannel",
    # value types
    "AnyValue",
    "LastValue",
    "LastValueAfterFinish",
    "UntrackedValue",
    "EphemeralValue",
    "BinaryOperatorAggregate",
    "DeltaChannel",
    "NamedBarrierValue",
    "NamedBarrierValueAfterFinish",
    # topics
    "Topic",
)
```

> 命名对照（部分旧文档/资料与 1.2.9 实名的差异）：
> - 常被叫作 "BinaryOp" 的 reducer 通道，实名是 **`BinaryOperatorAggregate`**（文件 `binop.py`）；
> - 常被叫作 "Delta" 的增量通道，实名是 **`DeltaChannel`**（文件 `delta.py`）。

---

## 2. BaseChannel 基类详解

### 2.1 源码全览

`langgraph/channels/base.py` 全文只有 122 行，是所有通道的契约：

```python
class BaseChannel(Generic[Value, Update, Checkpoint], ABC):
    """Base class for all channels."""

    __slots__ = ("key", "typ")

    def __init__(self, typ: Any, key: str = "") -> None:
        self.typ = typ
        self.key = key

    @property
    @abstractmethod
    def ValueType(self) -> Any:
        """The type of the value stored in the channel."""

    @property
    @abstractmethod
    def UpdateType(self) -> Any:
        """The type of the update received by the channel."""

    # serialize/deserialize methods

    def copy(self) -> Self:
        """Return a copy of the channel.
        By default, delegates to `checkpoint()` and `from_checkpoint()`.
        """
        return self.from_checkpoint(self.checkpoint())

    def checkpoint(self) -> Checkpoint | Any:
        """Return a serializable representation of the channel's current state.
        Raises `EmptyChannelError` if the channel is empty (never updated yet),
        or doesn't support checkpoints.
        """
        try:
            return self.get()
        except EmptyChannelError:
            return MISSING

    @abstractmethod
    def from_checkpoint(self, checkpoint: Checkpoint | Any) -> Self:
        """Return a new identical channel, optionally initialized from a checkpoint."""

    # read methods

    @abstractmethod
    def get(self) -> Value:
        """Return the current value of the channel.
        Raises `EmptyChannelError` if the channel is empty (never updated yet)."""

    def is_available(self) -> bool:
        try:
            self.get()
            return True
        except EmptyChannelError:
            return False

    # write methods

    @abstractmethod
    def update(self, values: Sequence[Update]) -> bool:
        """Update the channel's value with the given sequence of updates.
        The order of the updates in the sequence is arbitrary.
        This method is called by Pregel for all channels at the end of each step.
        If there are no updates, it is called with an empty sequence.
        Raises `InvalidUpdateError` if the sequence of updates is invalid.
        Returns `True` if the channel was updated, `False` otherwise."""

    def consume(self) -> bool:
        """Notify the channel that a subscribed task ran. By default, no-op."""
        return False

    def finish(self) -> bool:
        """Notify the channel that the Pregel run is finishing. By default, no-op."""
        return False
```

### 2.2 逐条解析

**（1）三个泛型参数 `Generic[Value, Update, Checkpoint]`**

这是理解各通道差异的钥匙：

| 参数 | 含义 | 例子 |
|------|------|------|
| `Value` | `get()` 读出来的类型（节点看到的状态字段类型） | `Topic` 的 Value 是 `Sequence[Value]`（列表），而写入可以是单个值 |
| `Update` | `update()` 接受的写入类型 | `Topic` 接受 `Value \| list[Value]`（一次发布一批） |
| `Checkpoint` | 持久化形态 | `NamedBarrierValue` 存的是 `set`（已到达者集合），`LastValueAfterFinish` 存的是 `tuple[Value, bool]` |

**（2）`MISSING` 哨兵**

来自 `langgraph/_internal/_typing.py`，就是一个裸 `object()`：

```python
MISSING = object()
"""Unset sentinel value."""
```

所有通道都用 `self.value = MISSING` 表示「从未被写过」。`checkpoint()` 返回 `MISSING` 意味着「**这个通道不进 checkpoint 的 `channel_values`**」——这是 `UntrackedValue`/`DeltaChannel` 实现「不持久化」的全部机制。注意它和 `None` 严格区分：`None` 是合法值，`MISSING` 是「没有值」。

**（3）`EmptyChannelError` 与 `is_available()`**

`get()` 在空通道上抛 `EmptyChannelError`，这不是错误处理而是**调度语义**：任务准备阶段发现某订阅通道不可用，节点就不触发。`is_available()` 是它的无异常版，基类默认实现是「调 `get()` 接异常」，各子类都覆写成廉价的哨兵比较（如 `self.value is not MISSING`）。

**（4）`update(values) -> bool` 的三条铁律**

- 每个超步末尾，**每个通道都会被调一次** `update`；本步有写入的通道收到全部写入（`Sequence[Update]`），没写入但「可用」的通道收到**空序列**（这是 `EphemeralValue`/`Topic` 实现「下一步自动清空」的钩子）；
- **序列内写入的顺序是任意的**（源码注释原话："The order of the updates in the sequence is arbitrary"）——所以并行节点写同一通道时，reducer 必须满足交换律，否则结果不确定；
- 返回 `True` 表示「通道变了」，Pregel 会给它**提升版本号**（`channel_versions[chan] = next_version`），版本变化是下游节点被触发的依据；返回 `False` 则一切不动。

**（5）`consume()` 与 `finish()`：两个可选的生命周期钩子**

- `consume()`：在**本步被读取过**（作为某任务的 trigger）的通道上调用，语义是「值已被消费」。`NamedBarrierValue` 用它重置屏障，`LastValueAfterFinish` 用它清值；默认 no-op 意味着 `LastValue`/`BinaryOperatorAggregate` 的值**读了不消失**。
- `finish()`：当某个超步「暂时看起来是最后一步」（本步更新的通道都触发不了任何节点）时对所有通道调用。`LastValueAfterFinish` / `NamedBarrierValueAfterFinish` 用它把值「放行」——这正是 `defer=True` 节点（延迟到图收尾前执行）的底层机制。

### 2.3 生命周期在 `apply_writes` 里的真实调用序

以上四个钩子由 `langgraph/pregel/_algo.py::apply_writes` 驱动，顺序非常明确（节选）：

```python
# Consume all channels that were read
for chan in {chan for task in tasks for chan in task.triggers
             if chan not in RESERVED and chan in channels}:
    if channels[chan].consume() and next_version is not None:
        checkpoint["channel_versions"][chan] = next_version

# Group writes by channel ...（跳过 NO_WRITES/PUSH/RESUME/INTERRUPT/RETURN/ERROR 等保留通道）

# Apply writes to channels
updated_channels: set[str] = set()
for chan, vals in pending_writes_by_channel.items():
    if chan in channels:
        if channels[chan].update(vals) and next_version is not None:
            checkpoint["channel_versions"][chan] = next_version
            if channels[chan].is_available():
                updated_channels.add(chan)

# Channels that weren't updated in this step are notified of a new step
if bump_step:
    for chan in channels:
        if channels[chan].is_available() and chan not in updated_channels:
            if channels[chan].update(EMPTY_SEQ) and next_version is not None:
                checkpoint["channel_versions"][chan] = next_version
                ...

# If this is (tentatively) the last superstep, notify all channels of finish
if bump_step and updated_channels.isdisjoint(trigger_to_nodes):
    for chan in channels:
        if channels[chan].finish() and next_version is not None:
            ...
```

解析四个要点：

1. **顺序固定：consume → update(实写入) → update(空序列) → finish**。先消费（重置屏障类通道），再折叠本步写入，然后给「没收到写入的可用通道」发空通知（临时值在这里自清），最后如果本步更新触发不了任何节点，就通知 finish（AfterFinish 家族在这里放行）。
2. **版本号只在 `update/consume/finish` 返回 `True` 时提升**，且提升后还要 `is_available()` 才算进 `updated_channels`——「不可用的通道触发不了任务」。
3. 写入未知通道只打 warning 不报错（`wrote to unknown channel {chan}, ignoring it`）。
4. `finish()` 是「暂定收尾」：如果放行后的通道又能触发节点（如 defer 节点），循环会继续，所以叫 tentatively。

---

## 3. LastValue：默认单值通道

### 3.1 源码摘录

`langgraph/channels/last_value.py`：

```python
class LastValue(Generic[Value], BaseChannel[Value, Value, Value]):
    """Stores the last value received, can receive at most one value per step."""

    __slots__ = ("value",)

    def __init__(self, typ: Any, key: str = "") -> None:
        super().__init__(typ, key)
        self.value = MISSING

    def __eq__(self, value: object) -> bool:
        return isinstance(value, LastValue)

    def update(self, values: Sequence[Value]) -> bool:
        if len(values) == 0:
            return False
        if len(values) != 1:
            msg = create_error_message(
                message=f"At key '{self.key}': Can receive only one value per step. "
                        f"Use an Annotated key to handle multiple values.",
                error_code=ErrorCode.INVALID_CONCURRENT_GRAPH_UPDATE,
            )
            raise InvalidUpdateError(msg)
        self.value = values[-1]
        return True

    def get(self) -> Value:
        if self.value is MISSING:
            raise EmptyChannelError()
        return self.value

    def checkpoint(self) -> Value:
        return self.value
```

### 3.2 解析

**（1）覆盖语义 + 单写保护**。`LastValue` 每步**最多接受一个写入**，直接覆盖旧值。第二个并行写入立刻炸出整个 LangGraph 用户最常撞的异常：

```
InvalidUpdateError: At key 'xxx': Can receive only one value per step.
Use an Annotated key to handle multiple values.
(error_code=INVALID_CONCURRENT_GRAPH_UPDATE)
```

注意报错文案直接给了修复方案：给字段加 `Annotated[T, reducer]`。这个错误只在**同一个超步有 ≥2 个任务写同一个字段**时发生（并行分支、或 `Send` 扇出）；串行覆盖永远不会触发。

**（2）空通知不清值**。`update([])` 返回 `False`——`LastValue` 的值一旦写入就**跨步保留**，直到被新值覆盖。这与 `EphemeralValue`/`AnyValue`/`Topic(accumulate=False)` 的「下一步自清」形成对照。

**（3）`__eq__` 只看类型**。任意两个 `LastValue` 相等（不比较 `typ`）。这个宽松相等被用在 schema 合并上——`StateGraph._add_schema` 中，同名字段在多个 schema 里重复出现时：

```python
if key in self.channels:
    if self.channels[key] != channel:
        if isinstance(channel, LastValue):
            pass          # LastValue 与任何 LastValue 兼容，静默通过
        else:
            raise ValueError(f"Channel '{key}' already exists with a different type")
```

**（4）持久化形态 = 值本身**。`checkpoint()` 直接返回 `self.value`；若从未写过（`MISSING`），`create_checkpoint` 会跳过它（不写入 `channel_values`）。`from_checkpoint` 恢复时同样以 `MISSING` 判空。

**（5）它是「兜底通道」**。第 5 节会看到：`_get_channel` 对没有任何 Annotated 元数据的字段，一律返回 `LastValue(annotation)`。所以**状态字段的默认通道就是 LastValue**。

### 3.3 变体：LastValueAfterFinish

同文件的 `LastValueAfterFinish(BaseChannel[Value, Value, tuple[Value, bool]])` 在 `LastValue` 之上加了一个 `finished` 标志：

```python
def update(self, values):          # 收到写入 ⇒ finished=False，暂存 value
    if len(values) == 0: return False
    self.finished = False
    self.value = values[-1]
    return True

def finish(self) -> bool:          # 图暂定收尾 ⇒ 放行
    if not self.finished and self.value is not MISSING:
        self.finished = True
        return True
    return False

def get(self) -> Value:            # 只有 finished 后才可读
    if self.value is MISSING or not self.finished:
        raise EmptyChannelError()
    return self.value

def consume(self) -> bool:         # 被读走一次后清空
    if self.finished:
        self.finished = False
        self.value = MISSING
        return True
    return False
```

语义：**值先攒着，直到「运行即将结束」才可见；被消费一次后清除**。持久化形态是 `(value, finished)` 二元组。它的实际用武之地在 `graph/state.py::attach_edge`：`defer=True` 的节点，其分支路由通道用 `LastValueAfterFinish(Any)`，普通节点用 `EphemeralValue(Any, guard=False)`——defer 节点就是靠它把「路由决定」压到全图收尾前才生效。

---

## 4. BinaryOperatorAggregate：Reducer 通道的本体

### 4.1 源码摘录

`langgraph/channels/binop.py`：

```python
class BinaryOperatorAggregate(Generic[Value], BaseChannel[Value, Value, Value]):
    """Stores the result of applying a binary operator to the current value
    and each new value.

    ```python
    import operator
    total = Channels.BinaryOperatorAggregate(int, operator.add)
    ```
    """

    __slots__ = ("value", "operator")

    def __init__(self, typ: type[Value], operator: Callable[[Value, Value], Value]):
        super().__init__(typ)
        self.operator = operator
        # special forms from typing or collections.abc are not instantiable
        # so we need to replace them with their concrete counterparts
        typ = _strip_extras(typ)
        if typ in (collections.abc.Sequence, collections.abc.MutableSequence):
            typ = list
        if typ in (collections.abc.Set, collections.abc.MutableSet):
            typ = set
        if typ in (collections.abc.Mapping, collections.abc.MutableMapping):
            typ = dict
        try:
            self.value = typ()
        except Exception:
            self.value = MISSING

    def update(self, values: Sequence[Value]) -> bool:
        if not values:
            return False
        if self.value is MISSING:
            self.value = values[0]
            values = values[1:]
        seen_overwrite: bool = False
        for value in values:
            is_overwrite, overwrite_value = _get_overwrite(value)
            if is_overwrite:
                if seen_overwrite:
                    raise InvalidUpdateError(
                        "Can receive only one Overwrite value per super-step.")
                self.value = overwrite_value
                seen_overwrite = True
                continue
            if not seen_overwrite:
                self.value = self.operator(self.value, value)
        return True

    def checkpoint(self) -> Value:
        return self.value
```

### 4.2 解析

**（1）初始值是 `typ()` 构造出来的**。这是它与 `LastValue` 最大的行为差异：`Annotated[list, operator.add]` 的通道初始值是 `[]`（因为 `list()` 成功），所以第一次 reducer 调用是 `operator.add([], 首个写入)`；而 `Annotated[int, operator.add]` 初始值是 `0`。抽象类型不可实例化，所以构造前先做替换——`Sequence→list`、`Set→set`、`Mapping→dict`（`_strip_extras` 负责剥掉 `Annotated`/`Required`/`NotRequired` 包装）。如果连替换后都构造不出来（比如 `typ` 是 `Any` 或某个无零参构造器的类），退回 `MISSING`，此时 `update` 用**第一个写入直接当初始值**（跳过一次 reducer 调用）。

**（2）折叠语义**。每步把所有写入按序两两折叠：`value = operator(value, v)`。再次强调：**同一步内多个写入的先后顺序是任意的**，`operator` 必须可交换才能保证确定性结果（`operator.add` 对 list/dict/set/int 都满足；字符串拼接、`max`、`min` 也安全；「后写覆盖前写」类的 lambda 不安全）。

**（3）空通知不改变值**。`update([])` 返回 `False`，累加值跨步、跨 checkpoint 保留。

**（4）`Overwrite` 逃生舱**。1.2.9 允许在**不改 schema** 的前提下，对单次写入绕过 reducer：

```python
def _get_overwrite(value: Any) -> tuple[bool, Any]:
    if isinstance(value, Overwrite):          # ① 类型化 dataclass
        return True, value.value
    if isinstance(value, dict):
        if len(value) == 1 and OVERWRITE in value:      # ② {"__overwrite__": v}
            return True, value[OVERWRITE]
        if value.get("type") == OVERWRITE and "value" in value:
            return True, value["value"]                  # ③ JSON 序列化后的退化形态
    return False, None
```

三种形态识别对应三种来路：进程内直接用 `langgraph.types.Overwrite(value=...)`；哨兵 dict；以及经 API server JSON 编码后 dataclass 类型被抹平的形态（`{"value": ..., "type": "__overwrite__"}`）。规则：**一个超步内最多一个 Overwrite**（第二个抛 `InvalidUpdateError`），Overwrite 生效为「整值替换」，且**同一步里排在它后面的普通写入被丢弃**（`if not seen_overwrite` 才折叠）——由于步内顺序任意，不要把 Overwrite 和普通写入混在同一步。

**（5）相等性判定有个 lambda 特例**：

```python
def _operators_equal(a: Callable, b: Callable) -> bool:
    """Lambdas all share the name '<lambda>' so identity comparison is
    unreliable; treat any pairing that includes a lambda as equal."""
    if a.__name__ == "<lambda>" or b.__name__ == "<lambda>":
        return True
    return a is b
```

`__eq__` 用它比较两个 reducer 通道是否「同型」。副作用：两个挂了**不同 lambda** 的通道会被判为相等（schema 合并检查会静默放行），用 lambda 当 reducer 时类型检查形同虚设——建议生产代码用**具名函数**。

**（6）持久化形态 = 累加值本身**。`checkpoint()` 返回整个 value（比如完整的 messages 列表）。这意味着长对话里**每一步的 checkpoint 都携带全量列表**——这正是 `DeltaChannel`（第 9 节）要解决的存储膨胀问题。

### 4.3 reducer 的注册机制回顾

`Annotated[list, add_messages]` 中的 `add_messages` 在编译期被 `_is_field_binop` 识别（源码见第 5 节），运行时被塞进 `BinaryOperatorAggregate.operator`。注册是**声明式的、一次性的**：图编译（`StateGraph.compile()` / 首次 `add_node`）时完成映射，之后 reducer 不可变。

---

## 5. Annotated[T, reducer] 如何映射到通道

### 5.1 入口：`_get_channels`

`langgraph/graph/state.py`：

```python
def _get_channels(
    schema: type[dict],
) -> tuple[dict[str, BaseChannel], dict[str, ManagedValueSpec], dict[str, Any]]:
    if not hasattr(schema, "__annotations__"):
        return ({"__root__": _get_channel("__root__", schema, allow_managed=False)}, {}, {})

    type_hints = get_type_hints(schema, include_extras=True)
    all_keys = {
        name: _get_channel(name, typ)
        for name, typ in type_hints.items()
        if name != "__slots__"
    }
    return (
        {k: v for k, v in all_keys.items() if isinstance(v, BaseChannel)},
        {k: v for k, v in all_keys.items() if is_managed_value(v)},
        type_hints,
    )
```

`include_extras=True` 是关键——保留 `Annotated` 元数据。每个字段的注解交给 `_get_channel`，产出的对象按类型分流：**`BaseChannel` 实例 → 通道字典；`ManagedValue` 子类 → 托管值字典**（后者见第 12 节）。

### 5.2 四级裁决：`_get_channel`

```python
def _get_channel(name, annotation, *, allow_managed=True):
    # Strip out Required and NotRequired wrappers
    if hasattr(annotation, "__origin__") and annotation.__origin__ in (Required, NotRequired):
        annotation = annotation.__args__[0]
    if manager := _is_field_managed_value(name, annotation):     # ① 托管值
        ...
        return manager
    elif channel := _is_field_channel(annotation):               # ② 显式通道
        channel.key = name
        return channel
    elif channel := _is_field_binop(annotation):                 # ③ reducer 函数
        channel.key = name
        return channel

    fallback: LastValue = LastValue(annotation)                  # ④ 兜底
    fallback.key = name
    return fallback
```

裁决优先级从高到低：**ManagedValue 子类 > BaseChannel 实例/类 > 二元可调用对象（reducer）> LastValue 兜底**。`channel.key = name` 把字段名注入通道，报错信息里的 `At key 'xxx'` 就来自这里。

### 5.3 第②级：`_is_field_channel` —— 显式通道

```python
def _is_field_channel(typ: type[Any]) -> BaseChannel | None:
    if hasattr(typ, "__metadata__"):
        meta = typ.__metadata__
        for item in meta:
            if isinstance(item, BaseChannel):
                if isinstance(item, DeltaChannel) and hasattr(typ, "__origin__"):
                    origin = typ.__origin__
                    if hasattr(origin, "__origin__") and origin.__origin__ in (Required, NotRequired):
                        origin = origin.__args__[0]
                    item = item.__class__(item.reducer, origin,
                                          snapshot_frequency=item.snapshot_frequency)
                return item
            elif isclass(item) and issubclass(item, BaseChannel):
                # ex, Annotated[int, EphemeralValue, SomeOtherAnnotation]
                # would return EphemeralValue(int)
                return item(typ.__origin__ if hasattr(typ, "__origin__") else typ)
    return None
```

两种写法都支持：

- **实例**：`Annotated[list, Topic(list, accumulate=True)]`、`Annotated[list, DeltaChannel(my_reducer)]` —— 直接采用；`DeltaChannel` 有特殊分支：用外层类型（如 `list[X]` 的 origin `list`）**重建实例**，让它拿到正确的 `typ`；
- **类**：`Annotated[int, EphemeralValue]` —— 自动用字段类型实例化 `EphemeralValue(int)`。

### 5.4 第③级：`_is_field_binop` —— reducer 函数

```python
def _is_field_binop(typ: type[Any]) -> BinaryOperatorAggregate | None:
    if hasattr(typ, "__metadata__"):
        meta = typ.__metadata__
        if len(meta) >= 1 and callable(meta[-1]):
            sig = signature(meta[-1])
            params = list(sig.parameters.values())
            if (sum(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                    for p in params) == 2):
                return BinaryOperatorAggregate(typ, meta[-1])
            else:
                raise ValueError(
                    f"Invalid reducer signature. Expected (a, b) -> c. Got {sig}")
    return None
```

识别规则精确到苛刻：只看**最后一个**元数据项；必须可调用；**位置参数恰好 2 个**（`*args`、纯关键字参数、1 个或 3 个位置参数都不行，直接 `ValueError: Invalid reducer signature`）。`operator.add`、`add_messages`、自定义 `def merge(a, b): ...` 都满足。

> 注意 `Annotated[list, add_messages]` 传给 `BinaryOperatorAggregate` 的 `typ` 是**整个 Annotated 类型**，构造函数里的 `_strip_extras` 会把它剥成 `list`，因此初始值仍是 `[]`。

---

## 6. Topic：发布订阅通道

### 6.1 源码摘录

`langgraph/channels/topic.py`：

```python
def _flatten(values: Sequence[Value | list[Value]]) -> Iterator[Value]:
    for value in values:
        if isinstance(value, list):
            yield from value
        else:
            yield value


class Topic(Generic[Value], BaseChannel[Sequence[Value], Value | list[Value], list[Value]]):
    """A configurable PubSub Topic.

    Args:
        typ: The type of the value stored in the channel.
        accumulate: Whether to accumulate values across steps. If `False`,
            the channel will be emptied after each step.
    """

    __slots__ = ("values", "accumulate")

    def update(self, values: Sequence[Value | list[Value]]) -> bool:
        updated = False
        if not self.accumulate:
            updated = bool(self.values)
            self.values = list[Value]()
        if flat_values := tuple(_flatten(values)):
            updated = True
            self.values.extend(flat_values)
        return updated

    def get(self) -> Sequence[Value]:
        if self.values:
            return list(self.values)
        else:
            raise EmptyChannelError

    def checkpoint(self) -> list[Value]:
        return self.values

    def from_checkpoint(self, checkpoint: list[Value]) -> Self:
        empty = self.__class__(self.typ, self.accumulate)
        empty.key = self.key
        if checkpoint is not MISSING:
            if isinstance(checkpoint, tuple):
                # backwards compatibility
                empty.values = checkpoint[1]
            else:
                empty.values = checkpoint
        return empty
```

### 6.2 解析

**（1）pub/sub 语义 = 「批写入 + 读列表 + 阅后即焚」**。Topic 的三个泛型参数就是它的性格：写入是 `Value | list[Value]`（单个发布或**一次发布一批**），读出是 `Sequence[Value]`（订阅者拿到本步全部消息），持久化是 `list[Value]`。

**（2）append 语义**。`_flatten` 把「多个写入者、每个写入者可能给单值也可能给列表」摊平成一维，`extend` 进 `values`——这就是 Topic 的 append：所有发布都被保留（在同一步内），顺序按写入序列（但记住：跨任务的写入顺序是任意的，不要依赖）。

**（3）remove / 清空语义**。1.2.9 的 Topic **没有按值删除（remove）的 API**——旧资料里提到的「remove 操作」在这个版本不存在。清空是**结构性**的，发生在 `update()` 开头：`accumulate=False`（默认）时，任何一次 `update` 调用都先把 `values` 整个扔掉。结合 `apply_writes` 的空通知机制（没收到写入的可用通道会被 `update(EMPTY_SEQ)`），效果就是：**非累积 Topic 的消息只活一个超步**，订阅者消费完、下一步到来时自动清空，通道回到 empty ⇒ 不再触发订阅者。这正是经典 pub/sub 的「阅后即焚」。

**（4）`accumulate=True`：变成追加日志**。不自动清空，值跨步累积、随 checkpoint 持久化，`get()` 返回历史全部消息。此时它近似 `Annotated[list, operator.add]`，差别在读出类型（Topic 读出永远是列表，写入可以是单值）。

**（5）空 Topic 是 empty 状态**。`get()` 在 `values == []` 时抛 `EmptyChannelError`——所以「本步无人发布」时订阅节点根本不会被调度，这是 Topic 与 `BinaryOperatorAggregate(list, operator.add)`（空列表也是可用值）在**触发行为**上的本质区别。

**（6）兼容性细节**：`from_checkpoint` 接受 `(dedupe, values)` 老式二元组；`__eq__` 比较 `accumulate` 标志。

**（7）引擎自用的实锤**：`pregel/main.py` 初始化时强制注册保留通道 `TASKS`：

```python
if TASKS in self.channels and not isinstance(self.channels[TASKS], Topic):
    ...
    self.channels[TASKS] = Topic(Send, accumulate=False)
```

`Send`（动态扇出的任务包）就是通过这个**非累积 Topic** 传递的：每个超步发布的 `Send` 只活一步，被消费后自动清空——这正是 6.2（3）「阅后即焚」语义在引擎内部的最大用户。

---

## 7. AnyValue：任意值通道

`langgraph/channels/any_value.py`：

```python
class AnyValue(Generic[Value], BaseChannel[Value, Value, Value]):
    """Stores the last value received, assumes that if multiple values are
    received, they are all equal."""

    def update(self, values: Sequence[Value]) -> bool:
        if len(values) == 0:
            if self.value is MISSING:
                return False
            else:
                self.value = MISSING
                return True

        self.value = values[-1]
        return True
```

解析：

- **无 guard 的 LastValue**：多路写入不报错，取 `values[-1]`。文档字符串说得很直白——「假设多个写入彼此相等」。适用场景：多个并行节点都会写同一个值（如都能推导出同一个 `db_name`），你不关心取哪个；
- **空通知清值**：与 `LastValue`（空通知不动）相反，`AnyValue` 收到空序列时把自己重置为 `MISSING` 并返回 `True`。也就是说它的值**只活一个超步**，这一点像 `EphemeralValue(guard=False)`；
- 它主要供引擎内部使用（例如需要「随便哪个都行」的内部控制通道），业务 schema 里很少直接写 `Annotated[X, AnyValue]`，但完全合法（走 `_is_field_channel` 的类形态分支）。

---

## 8. EphemeralValue：步级临时值

`langgraph/channels/ephemeral_value.py`：

```python
class EphemeralValue(Generic[Value], BaseChannel[Value, Value, Value]):
    """Stores the value received in the step immediately preceding, clears after."""

    __slots__ = ("value", "guard")

    def __init__(self, typ: Any, guard: bool = True) -> None:
        super().__init__(typ)
        self.guard = guard
        self.value = MISSING

    def update(self, values: Sequence[Value]) -> bool:
        if len(values) == 0:
            if self.value is not MISSING:
                self.value = MISSING
                return True
            else:
                return False
        if len(values) != 1 and self.guard:
            raise InvalidUpdateError(
                f"At key '{self.key}': EphemeralValue(guard=True) can receive only one "
                f"value per step. Use guard=False if you want to store any one of multiple values.")
        self.value = values[-1]
        return True

    def checkpoint(self) -> Value:
        return self.value
```

解析：

- **生命周期恰好一个超步**：第 N 步写入 → 第 N+1 步的订阅者读到 → 第 N+1 步末尾空通知清掉。「上一步的输出来龙去脉」类信号（handoff、一次性指令）用它最合适；
- **`guard` 参数**：`True`（默认）时同 `LastValue` 一样拒绝一步多写；`False` 时退化为 `AnyValue` 语义（多写取最后一个）。`__eq__` 也比较 guard；
- **它是会被 checkpoint 的**（`checkpoint()` 返回当前值）：若运行在「写入之后、清空之前」被打断/恢复，值仍在。它与 `UntrackedValue` 的区别就在这一点；
- **引擎自用（两处实锤）**：
  1. `state.py::attach_edge` 里普通（非 defer）节点的分支路由通道就是 `EphemeralValue(Any, guard=False)`——「路由到哪」这个信号只需要活一步；
  2. `state.py::compile` 组装最终 Pregel 时，**图的入口通道 `START` 本身就是 `EphemeralValue(self.input_schema)`**：

     ```python
     channels={
         **self.channels,
         **self.managed,
         START: EphemeralValue(self.input_schema),
     },
     input_channels=START,
     ```

     你 `invoke({...})` 传入的整份输入被写进这个通道、只触发第一步的入口节点，随后自动清空——输入不会被再次消费，这就是「一次性输入」语义的底层实现。

---

## 9. DeltaChannel：增量重放通道（Beta）

### 9.1 解决什么问题

`BinaryOperatorAggregate` 的 `checkpoint()` 返回**全量累加值**：一个 10 万条消息的会话，每一步的 checkpoint blob 都存 10 万条。`DeltaChannel` 的对策是**checkpoint 里什么都不存**（只留哨兵），恢复时用 reducer **重放祖先写入**重建状态，并周期性落一个快照来截断重放深度。

### 9.2 源码摘录

`langgraph/channels/delta.py`（docstring 节选 + 关键方法）：

```python
class DeltaChannel(Generic[Value], BaseChannel[Any, Any, Any]):
    """Reducer channel that stores only a sentinel in checkpoint blobs and
    reconstructs state by replaying ancestor writes through the reducer.

    !!! warning "Beta"
        `DeltaChannel` is in beta. The API and on-disk representation may
        change in future releases. ...

    The reducer receives the current accumulated value and a batch of writes
    in one call: `reducer(state, [write1, write2, ...]) -> new_state`.

    Reducers must be deterministic and batching-invariant (associative across
    folds): applying two consecutive write batches separately must produce the
    same state as applying their concatenation once:

        reducer(reducer(state, xs), ys) == reducer(state, xs + ys)

    Snapshot cadence is driven by two counters: per-channel update count and
    total supersteps since last snapshot. `create_checkpoint` writes a full
    `_DeltaSnapshot` blob when EITHER the update count reaches
    `snapshot_frequency` OR the supersteps count reaches the system-wide
    `DELTA_MAX_SUPERSTEPS_SINCE_SNAPSHOT` bound (default 5000) ...
    """

    def __init__(self, reducer, typ=None, *, snapshot_frequency: int = 1000):
        if snapshot_frequency <= 0:
            raise ValueError(...)
        if typ is None:
            typ = list  # placeholder; overridden by _is_field_channel
        ...

    def update(self, values: Sequence[Any]) -> bool:
        if not values:
            return False
        # ...（Overwrite 检测：一个超步最多一个，命中则整值替换后返回）
        base = self.typ() if self.value is MISSING else self.value
        self.value = self.reducer(base, list(values))
        return True

    def replay_writes(self, writes: Sequence[PendingWrite]) -> None:
        """Apply ancestor writes oldest-to-newest via a single reducer call.
        If any write is an Overwrite, the last one in the sequence acts as
        the reset point: its value becomes the new base and only writes
        after it are passed to the reducer."""
        values = [v for _, _, v in writes]
        if not values:
            return
        base = self.value
        start = 0
        for i, v in enumerate(values):
            is_ow, ow_value = _get_overwrite(v)
            if is_ow:
                base = _copy.copy(ow_value) if ow_value is not None else self.typ()
                start = i + 1
        remaining = values[start:]
        self.value = self.reducer(base, remaining) if remaining else base

    def checkpoint(self) -> Any:
        """Return stored representation: always `MISSING`.
        Snapshot decisions live in `create_checkpoint` ... For non-snapshot
        steps the channel does not appear in `channel_values`; reconstruction
        walks ancestor writes via the saver's `get_delta_channel_history`."""
        return MISSING
```

### 9.3 解析

**（1）reducer 签名不同**：不是 `(a, b) -> c` 而是 **`(state, [writes...]) -> new_state`**（批量）。注册写法是显式通道形态：`Annotated[list[X], DeltaChannel(my_batch_reducer)]`，`_is_field_channel` 会用外层类型重建实例（见 5.3）。

**（2）批不变性（batching-invariant）是硬约束**：`reducer(reducer(state, xs), ys) == reducer(state, xs + ys)`。因为重放时 LangGraph 可能把历史上分多步产生的写入**合并成一批**喂给 reducer，结果必须一致。确定 + 批不变的 reducer 才能安全用于 DeltaChannel（`langgraph/graph/message.py` 里的 `_messages_delta_reducer` 就是官方给 messages 写的批不变版本）。

**（3）持久化 = 哨兵 + 快照 + 写入日志三件套**：

- `checkpoint()` 恒返回 `MISSING` ⇒ 常规步的 `channel_values` 里**没有这个通道**；
- 每步的原始写入本来就会被 saver 记进 `checkpoint_writes` 表——这就是「增量」；
- 快照节奏由两个计数器决定（`pregel/_checkpoint.py::delta_channels_to_snapshot`）：**本通道累计更新数 ≥ `snapshot_frequency`（默认 1000）** 或 **距上次快照的超步数 ≥ `DELTA_MAX_SUPERSTEPS_SINCE_SNAPSHOT`（默认 5000）**，二者任一满足，`create_checkpoint` 就把 `_DeltaSnapshot(ch.get())` 全量写进 blob（`_DeltaSnapshot` 是 NamedTuple，经 msgpack ext code `EXT_DELTA_SNAPSHOT` 序列化），计数清零。

**（4）恢复 = 找种子 + 重放**。`channels_from_checkpoint`（`pregel/_checkpoint.py`）对缺值的 DeltaChannel 调 `saver.get_delta_channel_history` 沿 checkpoint 祖先链回溯：遇到最近的 `_DeltaSnapshot`（或迁移前的旧式普通值）作为 seed，`from_checkpoint(seed)` 后把 seed 与目标点之间的写入按时间序 `replay_writes` 一次性重放。

**（5）Overwrite 语义**：`update` 里一个超步最多一个 Overwrite（整值替换，`copy.copy` 防别名）；`replay_writes` 里**最后一个 Overwrite 是重放起点**（reset point），之前的写入全部作废。

**（6）迁移兼容**：`from_checkpoint` 接受三种 blob——`MISSING`（空起点，靠重放）、`_DeltaSnapshot`（直接取 `.value`）、普通值（从 `BinaryOperatorAggregate` 迁移过来的老数据，直接用）。

---

## 10. NamedBarrierValue：屏障同步通道

### 10.1 源码摘录

`langgraph/channels/named_barrier_value.py`：

```python
class NamedBarrierValue(Generic[Value], BaseChannel[Value, Value, set[Value]]):
    """A channel that waits until all named values are received before making
    the value available."""

    __slots__ = ("names", "seen")

    def __init__(self, typ: type[Value], names: set[Value]) -> None:
        super().__init__(typ)
        self.names = names
        self.seen: set[str] = set()

    def update(self, values: Sequence[Value]) -> bool:
        updated = False
        for value in values:
            if value in self.names:
                if value not in self.seen:
                    self.seen.add(value)
                    updated = True
            else:
                raise InvalidUpdateError(
                    f"At key '{self.key}': Value {value} not in {self.names}")
        return updated

    def get(self) -> Value:
        if self.seen != self.names:
            raise EmptyChannelError()
        return None

    def consume(self) -> bool:
        if self.seen == self.names:
            self.seen = set()
            return True
        return False

    def checkpoint(self) -> set[Value]:
        return self.seen
```

### 10.2 解析

- **它是「计数器」不是「存储器」**：`names` 是必须到齐的写入者名单，`seen` 是已到达集合。写入的值只用来打勾（不在名单里直接 `InvalidUpdateError`），`get()` 到齐后返回的也只是 `None`——**通道的「值」就是「全员到齐」这个事实**；
- **多节点汇聚**：只有 `seen == names` 时通道才 available，订阅它的节点才开始被调度 ⇒ 天然的 join/barrier。到齐可能跨多个超步（A 在第 1 步到、B 在第 2 步到），`seen` 会随 checkpoint（持久化形态就是 `set`）跨步累积；
- **`consume()` 复位**：屏障触发一次、订阅者跑完后，`seen` 清空，屏障可以再次使用（支持循环图里反复 join）；
- **`NamedBarrierValueAfterFinish`**：加 `finished` 标志，`get()` 要求 `finished and seen == names`，`finish()` 在到齐后置位，`consume()` 同时复位两者；持久化形态 `(seen, finished)`。用于 `defer=True` 的汇聚点节点；
- **实际用途——多入边 join 的编译产物**（`state.py::attach_edge`）：

```python
elif end != END:
    channel_name = f"join:{'+'.join(starts)}:{end}"
    if self.builder.nodes[end].defer:
        self.channels[channel_name] = NamedBarrierValueAfterFinish(str, set(starts))
    else:
        self.channels[channel_name] = NamedBarrierValue(str, set(starts))
    self.nodes[end].triggers.append(channel_name)          # end 订阅屏障
    for start in starts:
        self.nodes[start].writers.append(
            ChannelWrite((ChannelWriteEntry(channel_name, start),)))  # 各自写自己的名字
```

也就是说 `builder.add_edge(["a", "b"], "c")` 会生成名为 `join:a+b:c` 的屏障通道：a、b 各自在结束时把自己的**节点名**写进去，两个名字都到齐后 c 才触发。你也可以手动用实例形态注册：`Annotated[None, NamedBarrierValue(str, {"a", "b"})]`，节点返回 `{"barrier": "a"}` 即打勾。

---

## 11. UntrackedValue：不进 checkpoint 的值

`langgraph/channels/untracked_value.py`：

```python
class UntrackedValue(Generic[Value], BaseChannel[Value, Value, Value]):
    """Stores the last value received, never checkpointed."""

    def checkpoint(self) -> Value | Any:
        return MISSING

    def from_checkpoint(self, checkpoint: Value) -> Self:
        empty = self.__class__(self.typ, self.guard)
        empty.key = self.key
        return empty          # 注意：checkpoint 参数被无视

    def update(self, values: Sequence[Value]) -> bool:
        if len(values) == 0:
            return False      # 空通知不清值（与 EphemeralValue 不同！）
        if len(values) != 1 and self.guard:
            raise InvalidUpdateError(
                f"At key '{self.key}': UntrackedValue(guard=True) can receive only "
                f"one value per step. Use guard=False if you want to store any one "
                f"of multiple values.")
        self.value = values[-1]
        return True
```

解析：

- **持久化 = 永不**：`checkpoint()` 恒 `MISSING`（不落盘），`from_checkpoint()` 恒返回空通道（不恢复）。进程重启、线程恢复、时间旅行之后这个字段就是「从未写过」；
- **进程内跨步保留**：空通知返回 `False` 不清值——在单次运行内它像 `LastValue` 一样持续可见，只是不进存档；
- `guard` 语义与 `EphemeralValue` 相同（默认拒绝一步多写，`guard=False` 则任取其一）；
- 适用：大体积、可再生、且不想撑爆 checkpoint 的中间产物（如缓存的查询结果集、临时凭据）。**代价**：从 checkpoint 恢复的节点若依赖该字段，必须容忍它缺失（字段直接不在 state dict 里，读取要用 `.get()` 或接受 KeyError）。

### 与近亲通道的三向对比

| 行为 | LastValue | EphemeralValue | UntrackedValue |
|------|-----------|----------------|----------------|
| 一步多写 | 报错 | guard=True 报错 | guard=True 报错 |
| 空步通知 | 保留 | **清空** | 保留 |
| 进 checkpoint | ✅ | ✅ | **❌** |
| 典型用途 | 常规状态字段 | 步间信号 / 路由 | 可再生的大块临时数据 |

---

## 12. ManagedValue 托管值体系

### 12.1 1.2.9 的全部家当

先说结论：**1.2.9 的 ManagedValue 体系已极度精简**。旧版教程里的 `ManagedValueRegistry`、`ManagedState`、`SharedValue`、`IsLastStep` 的复杂注册机制都已不在，`langgraph/managed/base.py` 全文如下：

```python
class ManagedValue(ABC, Generic[V]):
    @staticmethod
    @abstractmethod
    def get(scratchpad: PregelScratchpad) -> V: ...


ManagedValueSpec = type[ManagedValue]


def is_managed_value(value: Any) -> TypeGuard[ManagedValueSpec]:
    return isclass(value) and issubclass(value, ManagedValue)


ManagedValueMapping = dict[str, ManagedValueSpec]
```

`langgraph/managed/__init__.py` 只导出两个内置托管值；`langgraph/managed/is_last_step.py` 全文：

```python
class IsLastStepManager(ManagedValue[bool]):
    @staticmethod
    def get(scratchpad: PregelScratchpad) -> bool:
        return scratchpad.step == scratchpad.stop - 1


IsLastStep = Annotated[bool, IsLastStepManager]


class RemainingStepsManager(ManagedValue[int]):
    @staticmethod
    def get(scratchpad: PregelScratchpad) -> int:
        return scratchpad.stop - scratchpad.step


RemainingSteps = Annotated[int, RemainingStepsManager]
```

### 12.2 托管值 vs 通道：本质区别

托管值**不是通道**——它没有存储、没有 `update`、不进 checkpoint。它是「**读取时现算**」的派生值，数据源是本次运行的 `PregelScratchpad`（`langgraph/_internal/_scratchpad.py`）：

```python
@dataclasses.dataclass(**_DC_KWARGS)
class PregelScratchpad:
    step: int            # 当前超步序号
    stop: int            # recursion_limit 换算出的总步数上限
    call_counter: Callable[[], int]
    interrupt_counter: Callable[[], int]
    get_null_resume: Callable[[bool], Any]
    resume: list[Any]
    subgraph_counter: Callable[[], int]
```

于是：`IsLastStep` = 「当前步 == 上限前一步」（再不收尾就要撞 `GraphRecursionError` 了，agent 常借此强制输出总结）；`RemainingSteps` = 剩余步数预算。

注入点有两处（都验证自源码）：

1. **任务输入组装**：`_algo.py::prepare_next_tasks` 给每个任务建 scratchpad，`_proc_input(proc, managed, channels, ..., scratchpad=scratchpad)` 把托管值算进节点看到的 state；
2. **运行中动态读**（条件边函数、节点内通过 `config[CONFIG_KEY_READ]` 读状态时）：`_algo.py::local_read` 末尾 `values.update({k: managed[k].get(scratchpad) for k in managed_keys})`。

schema 编译时托管值与通道分流（`_get_channels` 返回两个字典），并且**输出、流式、input 通道列表都会把它过滤掉**（`state.py` 多处 `[key for key, val in ... if not is_managed_value(val)]`）——所以你 `graph.invoke()` 的返回值里永远不会出现 `is_last_step` 字段，也不允许把它当 input schema。

### 12.3 自定义托管值

契约只剩一个静态方法，门槛极低：

```python
from langgraph.managed.base import ManagedValue
from langgraph._internal._scratchpad import PregelScratchpad

class StepParityManager(ManagedValue[str]):
    @staticmethod
    def get(scratchpad: PregelScratchpad) -> str:
        return "even" if scratchpad.step % 2 == 0 else "odd"

StepParity = Annotated[str, StepParityManager]

class State(TypedDict):
    x: int
    parity: StepParity      # 编译进 managed 字典，不是通道
```

注意 `_is_field_managed_value` 的识别规则：取 Annotated **最后一个**元数据项，`get_origin(meta[-1]) or meta[-1]` 后必须是 `ManagedValue` 子类（类本身，不是实例）。

---

## 13. 状态字段处理：_fields.py

`langgraph/_internal/_fields.py` 负责回答一个通道系统的外围问题：**schema 里每个字段的「默认值」是什么、哪些字段必填**——这决定了 state 视图里字段是缺席、是 `None`、还是有真默认值，进而影响输入 JSON schema 的生成。

### 13.1 `get_field_default`：默认值裁决

```python
def get_field_default(name: str, type_: Any, schema: type[Any]) -> Any:
    """Determine the default value for a field in a state schema.

    This is based on:
        If TypedDict:
            - Required/NotRequired
            - total=False -> everything optional
        - Type annotation (Optional/Union[None])
    """
    optional_keys = getattr(schema, "__optional_keys__", _DEFAULT_KEYS)
    irq = _is_required_type(type_)
    if name in optional_keys:
        if irq:
            return ...          # 显式 Required 压过 total=False
        return None
    if irq is not None:
        if irq:
            return ...          # Required[T]
        return None             # NotRequired[T]
    if dataclasses.is_dataclass(schema):
        field_info = next((f for f in dataclasses.fields(schema) if f.name == name), None)
        if field_info:
            if field_info.default is not dataclasses.MISSING and field_info.default is not ...:
                return field_info.default
            elif field_info.default_factory is not dataclasses.MISSING:
                return field_info.default_factory()
    # Note, we ignore ReadOnly attributes, as they don't make much sense. ...
    if _is_optional_type(type_):
        return None
    return ...
```

裁决表（返回值 `...` 即 Ellipsis，语义 = 「无默认值/必填/缺席」，与 `MISSING` 同族）：

| 声明形态 | 默认值 | 说明 |
|----------|--------|------|
| `total=False` 的 TypedDict 字段 | `None` | 除非又显式 `Required[T]`（返回 `...`） |
| `Required[T]` | `...` | 必填 |
| `NotRequired[T]` | `None` | |
| dataclass 字段带 `default` / `default_factory` | 该默认值 / 工厂产物 | 唯一能表达「非 None 真默认值」的 schema 形态 |
| `Optional[T]` / `T \| None` | `None` | `_is_optional_type` 递归穿透 Union/Annotated/TypeVar bound |
| 其他 | `...` | 必填 |

`ReadOnly[T]` 被**故意忽略**，源码注释写得很直白：「we don't care if you mutate the state in your node, and mutating state in your node has no effect on our graph state」——节点内改 state dict 本来就不会写回通道（只有 return 的字典才算写入）。

该函数在 `state.py::_get_json_schema` 中被调用，为 Pydantic `create_model` 提供每个字段的 `(UpdateType, default)`——即**图的输入 JSON schema** 里哪些键必填、哪些默认 `None`。注意 `UpdateType` 取自通道对象：`Topic` 字段的输入类型是「单值或列表」，`LastValue` 字段就是值本身。

### 13.2 其余三个工具

- **`get_update_as_tuples(input, keys)`**：把（Pydantic 模型形态的）state 更新转成 `[(key, value), ...]` 写入对。对 Pydantic 有个历史包袱行为：只保留 `model_fields_set` 里显式设置过的、或不同于默认值的、或非 None 的字段（源码注释承认 "somewhat inelegant, but we keep around for backwards compatibility"）；
- **`get_enhanced_type_hints(type)`**：从 Pydantic `model_fields` / 类 `__dict__` 里挖字段的 default 与 description，供 config schema 生成使用；
- **`get_cached_annotated_keys(obj)`**：沿 `__mro__` 逆序收集所有 `__annotations__` 的键（父类字段在前），结果缓存在 `weakref.WeakKeyDictionary`。它是「这个 schema 到底有哪些字段」的权威口径，兼容 Python 3.14+ Pydantic 把 `__annotations__` 变成描述符的情形。

---

## 14. Reducer 函数设计模式

### 14.1 官方旗舰 reducer：`add_messages`

`langgraph/graph/message.py` 的 `add_messages(left, right)` 是 `BinaryOperatorAggregate` 最重要的客户，值得拆解它示范了哪些设计原则：

```python
# coerce to list
if not isinstance(left, list):  left = [left]
if not isinstance(right, list): right = [right]
# coerce to message（dict/字符串/tuple → BaseMessage；chunk → message）
left  = [message_chunk_to_message(m) for m in convert_to_messages(left)]
right = [message_chunk_to_message(m) for m in convert_to_messages(right)]
# assign missing ids
for m in left:
    if m.id is None: m.id = str(uuid.uuid4())
for idx, m in enumerate(right):
    if m.id is None: m.id = str(uuid.uuid4())
    if isinstance(m, RemoveMessage) and m.id == REMOVE_ALL_MESSAGES:
        remove_all_idx = idx
if remove_all_idx is not None:
    return right[remove_all_idx + 1:]
# merge
merged = left.copy()
merged_by_id = {m.id: i for i, m in enumerate(merged)}
ids_to_remove = set()
for m in right:
    if (existing_idx := merged_by_id.get(m.id)) is not None:
        if isinstance(m, RemoveMessage):
            ids_to_remove.add(m.id)
        else:
            ids_to_remove.discard(m.id)
            merged[existing_idx] = m          # 同 id ⇒ 原位替换
    else:
        if isinstance(m, RemoveMessage):
            raise ValueError(f"Attempting to delete a message with an ID that doesn't exist ('{m.id}')")
        merged_by_id[m.id] = len(merged)
        merged.append(m)                       # 新 id ⇒ 追加
merged = [m for m in merged if m.id not in ids_to_remove]
return merged
```

它示范的模式：

1. **宽容输入**：单条消息、dict、chunk 都先归一成 list of BaseMessage——reducer 永远不要假设写入方形态完美；
2. **给数据补身份**：缺 id 的消息自动分配 UUID，因为 id 是合并的主键；
3. **按主键 upsert 而非无脑 append**：同 id 原位替换（支持「修正上一条 AI 消息」），新 id 追加——这让 reducer 对**重放是幂等的**（同一条写入折叠两次结果不变），checkpoint 恢复重跑不会翻倍；
4. **删除也是写入**：`RemoveMessage` 作为墓碑（tombstone）走同一个 reducer，而不是旁路 API；`REMOVE_ALL_MESSAGES` 哨兵支持清空重来（上下文压缩场景）；
5. **不改左值**：`merged = left.copy()`——reducer 必须返回新对象，就地修改旧值会污染 checkpoint 里共享引用的数据。

同文件还有 `_messages_delta_reducer(state, writes)`：`DeltaChannel` 版的批量 reducer，一次处理整批写入（去重、墓碑），官方注释明确标注它满足批不变性。

### 14.2 `operator.add`：最简 reducer

`Annotated[list, operator.add]`、`Annotated[int, operator.add]`、`Annotated[str, operator.add]`、`Annotated[set, operator.or_]`。特点是**依赖类型自带的运算符**，零成本，但没有任何去重/upsert 语义：并行重放时列表会重复追加（`dict` 没有 `+`，`operator.add` 会 TypeError，dict 合并必须自定义 reducer）。**要幂等就自己写**。

### 14.3 自定义 reducer 的设计清单

结合前文源码事实，自定义 reducer 应满足：

| 原则 | 依据（源码事实） |
|------|------------------|
| **签名恰好两个位置参数** `(a, b) -> c` | `_is_field_binop` 数 `POSITIONAL_ONLY + POSITIONAL_OR_KEYWORD == 2`，否则编译期 `ValueError` |
| **交换律**（同一步内多写入顺序任意） | `BaseChannel.update` docstring："The order of the updates in the sequence is arbitrary" |
| **确定性 / 幂等**（恢复时写入会被重放） | `apply_writes` 按 pending writes 重建；DeltaChannel 更是显式重放 |
| **不就地修改入参，返回新对象** | checkpoint 值与通道值共享引用；`add_messages` 的 `left.copy()` |
| **处理 `a` 为「初始值」的情形** | `typ()` 能构造 ⇒ 第一次调用是 `reducer([], x)`；构造失败 ⇒ 第一次直接 `value = values[0]`（reducer 不被调用） |
| **别用 lambda**（生产环境） | `_operators_equal` 把所有 lambda 视为相等，schema 冲突检查失效 |
| **批不变性**（若用于 DeltaChannel） | `reducer(reducer(s, xs), ys) == reducer(s, xs+ys)` |
| **想整值重置时用 `Overwrite`**，不要在 reducer 里识别魔法值 | `binop.py::_get_overwrite` 已有三种形态识别 |

一个典型的幂等合并 reducer：

```python
def merge_by_id(left: list[dict], right: list[dict]) -> list[dict]:
    """按 id upsert；顺序无关（交换律成立：以 id 为键，与到达顺序无关）。"""
    if not isinstance(right, list):
        right = [right]
    merged = {item["id"]: dict(item) for item in left}   # copy，不改 left
    for item in right:
        merged[item["id"]] = {**merged.get(item["id"], {}), **item}
    return list(merged.values())
```

---

## 15. 通道的序列化与 Checkpoint

### 15.1 落盘：`create_checkpoint`

`langgraph/pregel/_checkpoint.py`（节选）：

```python
values = {}
channel_versions = dict(checkpoint["channel_versions"])
for k in channels:
    if k not in channel_versions:
        continue
    ch = channels[k]
    if k in channels_to_snapshot:               # 仅 DeltaChannel 快照步
        values[k] = _DeltaSnapshot(ch.get())
    else:
        v = ch.checkpoint()
        if v is not MISSING:                    # MISSING ⇒ 不落盘
            values[k] = v
return Checkpoint(
    v=LATEST_VERSION, ts=ts,
    id=id or str(uuid6(clock_seq=step)),
    channel_values=values,
    channel_versions=channel_versions,
    versions_seen=checkpoint["versions_seen"],
    ...
)
```

要点：

- checkpoint 三要素：`channel_values`（每通道的序列化形态）、`channel_versions`（版本号，调度依据）、`versions_seen`（每个节点见过哪个版本，防重复触发）；
- **`checkpoint()` 返回 `MISSING` 的通道直接缺席**——空 `LastValue`、`UntrackedValue`、非快照步的 `DeltaChannel` 都不占 blob；
- `channel_values` 里的对象随后由 saver 的 serde（默认 `JsonPlusSerializer`，msgpack + 扩展类型码）序列化成字节；`_DeltaSnapshot` 有专属 ext code `EXT_DELTA_SNAPSHOT`。

### 15.2 各通道的持久化形态一览

| 通道 | `checkpoint()` 返回 | 备注 |
|------|---------------------|------|
| `LastValue` | 值本身（未写过为 `MISSING`） | 最简单形态 |
| `LastValueAfterFinish` | `(value, finished)`；未写过为 `MISSING` | 标志位随值一起存档 |
| `BinaryOperatorAggregate` | 全量累加值 | 长列表 ⇒ blob 膨胀 |
| `DeltaChannel` | 恒 `MISSING`；快照步由外部写入 `_DeltaSnapshot(value)` | 恢复靠 `get_delta_channel_history` 祖先回溯 + `replay_writes` |
| `Topic` | `list[Value]`（当前值列表） | 兼容读老式 `(dedupe, values)` 元组 |
| `AnyValue` / `EphemeralValue` | 值本身 | Ephemeral 存档的是「还没被下一步清掉」的窗口期值 |
| `NamedBarrierValue` | `seen` 集合 | 恢复后屏障进度不丢 |
| `NamedBarrierValueAfterFinish` | `(seen, finished)` | |
| `UntrackedValue` | 恒 `MISSING` | `from_checkpoint` 也无视入参 |

### 15.3 恢复：`channels_from_checkpoint`

```python
for k, spec in channel_specs.items():
    if k in histories:                      # DeltaChannel 需重放
        replay_ch = delta_spec.from_checkpoint(history.get("seed", MISSING))
        replay_ch.replay_writes(history["writes"])
        ch = replay_ch
    else:
        ch = spec.from_checkpoint(checkpoint["channel_values"].get(k, MISSING))
```

所有通道的 `from_checkpoint` 都遵守同一契约：**返回新实例，不改 self**；入参 `MISSING` 表示「空通道启动」。缺席于 `channel_values` 的普通通道（如 `UntrackedValue`）自然拿到 `MISSING`，恢复为未写状态。

---

## 16. 通道对比总表

| 通道 | 一步多写 | 值的寿命 | 空步通知后 | 读出类型 | checkpoint 形态 | 典型用途 |
|------|----------|----------|------------|----------|------------------|----------|
| `LastValue` | ❌ InvalidUpdateError | 永久（直到覆盖） | 保留 | 单值 | 值 | 状态字段默认通道 |
| `LastValueAfterFinish` | ✅ 取最后 | finish 后可读，consume 即清 | 保留 | 单值 | `(值, finished)` | defer 节点路由 |
| `BinaryOperatorAggregate` | ✅ reducer 折叠 | 永久累积 | 保留 | 单值（累加态） | 全量累加值 | `Annotated[T, reducer]` |
| `DeltaChannel` (Beta) | ✅ 批量 reducer | 永久（重放重建） | 保留 | 单值（累加态） | `MISSING`+周期快照 | 超大 append-only 状态 |
| `Topic` | ✅ extend 摊平 | 1 步（`accumulate=False`）/ 永久（True） | **清空**（False 时） | 列表 | `list` | pub/sub 消息总线 |
| `AnyValue` | ✅ 取最后 | 1 步 | **清空** | 单值 | 值 | 「谁都行」的内部信号 |
| `EphemeralValue` | guard 控制 | 1 步 | **清空** | 单值 | 值 | 步间 handoff / 路由 |
| `UntrackedValue` | guard 控制 | 进程内永久 | 保留 | 单值 | **恒 MISSING** | 可再生大块临时数据 |
| `NamedBarrierValue` | ✅ 打勾 | 到齐触发，consume 复位 | 保留 | `None`（事实通道） | `seen` 集合 | 多入边 join |
| `NamedBarrierValueAfterFinish` | ✅ 打勾 | 到齐 + finish 后触发 | 保留 | `None` | `(seen, finished)` | defer join |

---

## 17. 完整代码示例

### 示例 1：并行写冲突——从报错到自定义 reducer

```python
"""场景：扇出两个并行节点写同一字段。
第一版用默认 LastValue ⇒ InvalidUpdateError；第二版自定义 reducer 解决。"""
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END

# ---------- 第一版：LastValue，会炸 ----------
class BadState(TypedDict):
    findings: str

def scan_a(state: BadState) -> dict:
    return {"findings": "A: 发现空口令"}

def scan_b(state: BadState) -> dict:
    return {"findings": "B: 发现弱加密"}

g = StateGraph(BadState)
g.add_node("a", scan_a); g.add_node("b", scan_b)
g.add_edge(START, "a"); g.add_edge(START, "b")   # 同一步并行
g.add_edge("a", END);    g.add_edge("b", END)
try:
    g.compile().invoke({"findings": ""})
except Exception as e:
    print(type(e).__name__, e)
    # InvalidUpdateError: At key 'findings': Can receive only one value per step.
    # Use an Annotated key to handle multiple values.

# ---------- 第二版：自定义 reducer ----------
def merge_findings(left: list[str], right: list[str]) -> list[str]:
    """幂等 + 交换律：以「内容集合」为准，与到达顺序无关，重放不翻倍。"""
    seen = dict.fromkeys(left)          # 保序去重，不修改 left
    seen.update(dict.fromkeys(right))
    return list(seen)

class GoodState(TypedDict):
    findings: Annotated[list[str], merge_findings]

def scan_a2(state: GoodState) -> dict:
    return {"findings": ["A: 发现空口令"]}

def scan_b2(state: GoodState) -> dict:
    return {"findings": ["B: 发现弱加密", "A: 发现空口令"]}  # 故意重复

g2 = StateGraph(GoodState)
g2.add_node("a", scan_a2); g2.add_node("b", scan_b2)
g2.add_edge(START, "a"); g2.add_edge(START, "b")
g2.add_edge("a", END);    g2.add_edge("b", END)
print(g2.compile().invoke({"findings": []})["findings"])
# ['A: 发现空口令', 'B: 发现弱加密']  —— 两个写入被 merge_findings 折叠、重复项被去掉
# 注意：步内写入顺序是任意的，两条的先后不保证；本 reducer 只对「集合」敏感，结果恒定
```

要点：`findings` 初始值是 `list()` 即 `[]`（`BinaryOperatorAggregate.__init__` 的 `typ()` 机制）；两次写入在**同一个超步**内被一次 `update([w1, w2])` 折叠。

### 示例 2：多通道混合状态 + 托管值

```python
"""场景：一个 agent 状态同时使用 5 种通道机制。"""
import operator
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.channels import EphemeralValue
from langgraph.managed.is_last_step import IsLastStep, RemainingSteps

class AgentState(TypedDict):
    # ① reducer 通道：消息按 id upsert（BinaryOperatorAggregate + add_messages）
    messages: Annotated[list, add_messages]
    # ② reducer 通道：工具调用计数（operator.add，int 初始值 0）
    tool_calls: Annotated[int, operator.add]
    # ③ 默认通道：当前话题，后写覆盖（LastValue）
    topic: str
    # ④ 临时通道：只活一步的 handoff 信号（EphemeralValue）
    handoff: Annotated[str, EphemeralValue]
    # ⑤ 托管值：非通道，读取时从 scratchpad 现算
    is_last_step: IsLastStep
    remaining_steps: RemainingSteps

def router(state: AgentState) -> dict:
    print("  [router] handoff =", state.get("handoff"))     # 第一步后为空
    return {"topic": "sql", "handoff": "to_writer", "tool_calls": 1}

def writer(state: AgentState) -> dict:
    print("  [writer] handoff =", state["handoff"])          # 读到了上一步信号
    print("  [writer] is_last_step =", state["is_last_step"],
          "remaining =", state["remaining_steps"])
    return {
        "messages": [{"role": "assistant", "content": f"关于 {state['topic']} 的回答"}],
        "handoff": "done",
    }

def reporter(state: AgentState) -> dict:
    # reporter 运行时 handoff="done" 仍活着（writer 上一步写的）；
    # 本步没人再写它 ⇒ 步末空通知清空 ⇒ 最终状态里该字段缺席
    print("  [reporter] handoff =", state.get("handoff"))
    return {"messages": [{"role": "system", "content": f"共调用工具 {state['tool_calls']} 次"}]}

g = StateGraph(AgentState)
g.add_node("router", router); g.add_node("writer", writer); g.add_node("reporter", reporter)
g.add_edge(START, "router")
g.add_edge("router", "writer")     # writer 在下一超步读到 handoff="to_writer"
g.add_edge("writer", "reporter")   # 再下一步，"done" 也只剩一步寿命
g.add_edge("reporter", END)

final = g.compile().invoke({"messages": [{"role": "user", "content": "查一下销售额"}]})
# keys: ['messages', 'tool_calls', 'topic']
#   —— 没有 is_last_step / remaining_steps（托管值不进输出）；
#   —— 也没有 handoff：最后一步没人写它，步末空通知把它清空 ⇒ 通道 empty ⇒ 字段缺席
print("keys:", sorted(final.keys()))
print("tool_calls:", final["tool_calls"], "| topic:", final["topic"])
for m in final["messages"]:
    print(" ", m.type, ":", m.content)
```

要点：① `handoff` 在每个超步末尾被 `apply_writes` 的空通知清掉，所以只有紧邻的下一步能看到；② `is_last_step`/`remaining_steps` 不是通道，`invoke` 返回值里**没有**它们（`state.py` 输出通道列表用 `is_managed_value` 过滤），只在节点入参里现算注入；③ `tool_calls` 的 `operator.add` 从 `int() == 0` 起累加。

### 示例 3：屏障同步——多节点汇聚

```python
"""场景 A：声明式 join —— add_edge(["a","b"], "c") 自动编译出 NamedBarrierValue。
场景 B：屏障跨超步累积（两个写入者不在同一步到达）。
场景 C：手动注册 NamedBarrierValue 状态字段 —— 用『可用性』控制字段可见性。"""
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.channels import NamedBarrierValue

def append_logs(a: list[str], b: list[str]) -> list[str]:   # 具名 reducer（勿用 lambda）
    return a + b

# ---------- 场景 A：声明式 join ----------
class S(TypedDict):
    logs: Annotated[list[str], append_logs]

def fetch_orders(state: S) -> dict:  return {"logs": ["orders✓"]}
def fetch_users(state: S) -> dict:   return {"logs": ["users✓"]}
def merge(state: S) -> dict:
    print("  [merge] 屏障到齐后才执行, logs =", state["logs"])
    return {"logs": ["merged"]}

g = StateGraph(S)
g.add_node("orders", fetch_orders); g.add_node("users", fetch_users); g.add_node("merge", merge)
g.add_edge(START, "orders"); g.add_edge(START, "users")
g.add_edge(["orders", "users"], "merge")
g.add_edge("merge", END)

compiled = g.compile()
# 验证编译产物：join 边生成了一个内部屏障通道
print(type(compiled.channels["join:orders+users:merge"]).__name__)   # NamedBarrierValue
print(compiled.invoke({"logs": []})["logs"])
# ['orders✓', 'users✓', 'merged']

# ---------- 场景 B：屏障跨超步累积 ----------
# fast 第 1 超步到达（seen={"fast"}，屏障不放行）；
# slow 要经过 slow_prep、第 2 超步才到达（seen 补齐）；
# merge 第 3 超步才被触发 —— seen 集合在通道里跨步存活。
g2 = StateGraph(S)
g2.add_node("fast", lambda s: {"logs": ["fast✓(step1)"]})
g2.add_node("slow_prep", lambda s: {})          # 不写任何通道，纯占一步
g2.add_node("slow", lambda s: {"logs": ["slow✓(step2)"]})
g2.add_node("merge", merge)
g2.add_edge(START, "fast"); g2.add_edge(START, "slow_prep")
g2.add_edge("slow_prep", "slow")
g2.add_edge(["fast", "slow"], "merge")
g2.add_edge("merge", END)
print(g2.compile().invoke({"logs": []})["logs"])
# ['fast✓(step1)', 'slow✓(step2)', 'merged'] —— merge 压轴

# ---------- 场景 C：手动屏障字段 ----------
class GateState(TypedDict):
    gate: Annotated[None, NamedBarrierValue(str, {"worker_a", "worker_b"})]
    logs: Annotated[list[str], append_logs]

def worker_a(state: GateState) -> dict:
    return {"gate": "worker_a", "logs": ["a 打勾"]}     # 写入名单里的名字=打勾
    # 若写 {"gate": "someone_else"} ⇒ InvalidUpdateError: Value someone_else not in {...}

def worker_b(state: GateState) -> dict:
    return {"gate": "worker_b", "logs": ["b 打勾"]}

def observer_early(state: GateState) -> dict:
    # 此刻只有 worker_a 打过勾，seen != names ⇒ 通道 empty ⇒ gate 键整个缺席
    print("  [observer_early] 'gate' in state:", "gate" in state)      # False
    return {"logs": ["early 看不到 gate"]}

def observer_late(state: GateState) -> dict:
    # 两个名字都到齐 ⇒ 通道 available ⇒ gate 出现，值恒为 None（事实通道）
    print("  [observer_late] 'gate' in state:", "gate" in state,
          "| value:", state.get("gate"))                                # True | None
    return {"logs": ["late 看到 gate=None"]}

g3 = StateGraph(GateState)
g3.add_node("worker_a", worker_a); g3.add_node("observer_early", observer_early)
g3.add_node("worker_b", worker_b); g3.add_node("observer_late", observer_late)
g3.add_edge(START, "worker_a")
g3.add_edge("worker_a", "observer_early")   # step2：屏障还差一个名字
g3.add_edge("observer_early", "worker_b")   # step3：worker_b 补上最后一勾
g3.add_edge("worker_b", "observer_late")    # step4：gate 可读
g3.add_edge("observer_late", END)
print(g3.compile().invoke({"logs": []})["logs"])
```

要点：① 场景 A 的屏障是编译期自动生成的（`join:a+b:c`），每个上游节点结束时把自己的**节点名**写进去；`seen` 集合随 checkpoint 持久化，因此场景 B 里跨超步累积、恢复后进度不丢；被 merge 消费后 `consume()` 复位，支持循环图反复 join；② 手动屏障字段（场景 C）**不驱动调度**——StateGraph 里节点的触发器是各自的分支/join 通道——它驱动的是「这个键在 state 视图里是否可见」：没到齐时通道 empty，字段直接缺席；③ 屏障 `get()` 返回 `None`——它是事实通道，不携带数据。

### 示例 4：DeltaChannel + Overwrite（增量与整值重置）

```python
"""场景：超大 append-only 事件日志，用 DeltaChannel 避免每步全量落盘；
用 Overwrite 做上下文压缩式的整值重置。"""
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.channels import DeltaChannel
from langgraph.types import Overwrite

def batch_append(state: list[str], writes: list) -> list[str]:
    """批量 reducer：(累加态, [本步全部写入]) -> 新累加态。
    满足批不变性：分批折叠与合并折叠结果一致（纯追加 + 摊平）。"""
    out = list(state)                      # 不就地修改
    for w in writes:
        out.extend(w if isinstance(w, list) else [w])
    return out

class LogState(TypedDict):
    events: Annotated[list[str], DeltaChannel(batch_append, snapshot_frequency=100)]

def produce(state: LogState) -> dict:
    return {"events": ["evt-1", "evt-2"]}  # 普通写入 ⇒ 走 reducer

def compact(state: LogState) -> dict:
    # 压缩：整值替换，绕过 reducer；本超步只允许这一个 Overwrite
    return {"events": Overwrite(value=["<compacted>"])}

g = StateGraph(LogState)
g.add_node("produce", produce); g.add_node("compact", compact); g.add_node("produce2", produce)
g.add_edge(START, "produce"); g.add_edge("produce", "compact"); g.add_edge("compact", "produce2")
g.add_edge("produce2", END)
print(g.compile().invoke({"events": []})["events"])
# ['<compacted>', 'evt-1', 'evt-2'] —— Overwrite 把之前的累积清成哨兵值，之后继续增量
```

要点：① reducer 签名是 `(state, writes_batch)` 两参批量形态；② `snapshot_frequency=100` 表示每 100 次更新落一个 `_DeltaSnapshot` 全量快照，其余步骤 checkpoint 里该通道**缺席**，恢复时靠祖先写入重放；③ `Overwrite` 每超步最多一个，且同一步中它之后的普通写入会被丢弃（步内顺序任意，勿混用）。

---

## 18. 常见陷阱与调试

1. **`InvalidUpdateError: Can receive only one value per step`**
   并行节点写了无 reducer 的字段。修法：`Annotated[T, reducer]`。报错自带 error_code `INVALID_CONCURRENT_GRAPH_UPDATE`，可据此做程序化捕获。

2. **reducer 编译期就被拒：`Invalid reducer signature. Expected (a, b) -> c`**
   `_is_field_binop` 只认「恰好 2 个位置参数」。`def r(*args)`、`def r(a, b, c)`、`functools.partial` 后仍不满足计数的都不行。

3. **以为 Annotated 里随便放个 callable 就是 reducer**
   裁决顺序是 ManagedValue > BaseChannel > reducer。若元数据最后一项是 `ManagedValue` 子类或通道实例，reducer 分支根本轮不到。

4. **`EphemeralValue` 字段在节点里 KeyError**
   它是「只活一步」的通道：没人写的下一步，通道 empty，**字段整个从 state dict 里消失**（不是 `None`）。读取用 `state.get("handoff")`，或声明为 `NotRequired`（`get_field_default` 会给输入 schema 默认 `None`）。

5. **Topic 订阅者「收不到消息」**
   非累积 Topic 在下一步被空通知清空。订阅节点必须与被发布写入处在「写入的下一步」这个精确窗口；想要持久收件箱请用 `Topic(accumulate=True)` 或 `Annotated[list, operator.add]`。

6. **lambda reducer 让 schema 冲突检查失效**
   `_operators_equal` 对任意 lambda 对返回 True。两个不同 schema 里同名字段挂了不同 lambda reducer，编译期不会报「Channel already exists with a different type」。用具名函数。

7. **`UntrackedValue` 在恢复后神秘消失**
   这是特性不是 bug：`checkpoint()` 恒 `MISSING`。依赖它的节点必须能在字段缺席时重新生成它。

8. **reducer 不幂等导致重放后数据翻倍**
   从 checkpoint 恢复时，pending writes 会被重新应用。无脑 `left + right` 的列表追加在「节点重跑」场景会重复；参考 `add_messages` 用 id upsert。

9. **同一超步混用 `Overwrite` 与普通写入**
   Overwrite 之后的普通写入被静默丢弃，且步内顺序任意 ⇒ 结果不确定。一步只做一种。

10. **调试通道状态的手段**
    - `graph.get_state(config)` / `get_state_history(config)`：看每步 checkpoint 的 `values`（即所有非空通道 `get()` 的拼视图）；
    - `stream_mode="debug"`：能看到每步任务与写入；
    - 直接读底层：`checkpointer.get(config)["channel_values"]` 就是各通道 `checkpoint()` 的原始产物（`MISSING` 的通道缺席，`UntrackedValue` 永远不在里面）。

---

## 附：一分钟心智模型

- **状态字段 = 通道**；`Annotated` 元数据决定通道类型，无元数据 ⇒ `LastValue`。
- **reducer = `BinaryOperatorAggregate` 的折叠函数**，签名 `(a, b) -> c`，必须交换、确定、幂等。
- 通道四钩子按固定序驱动：`consume`（读后）→ `update(写入)` → `update(空)`（没收到写入的可用通道）→ `finish`（暂定收尾）。返回 `True` ⇒ 版本号提升 ⇒ 可能触发下游。
- `checkpoint()` 返回 `MISSING` ⇒ 不落盘；持久化形态因通道而异（值 / 列表 / seen 集合 / `(值, 标志)`）。
- **ManagedValue 不是通道**：无存储、不落盘，读取时从 `PregelScratchpad` 现算（`IsLastStep`、`RemainingSteps`）。
