# dangling_tool_calls.py 代码解读

> 文件路径：`src/agent/middlewares/dangling_tool_calls.py`（136 行）
> 解读日期：2026-09-18

## 一句话概括

`DanglingToolCallsMiddleware`：在**每次 model 调用前**（`before_model`）扫描最后一个 AIMessage，为未被应答的 orphan `tool_call_id` 补合成 ToolMessage，防止 LLM API 400 错误。

## 解决的问题（生产 trace 513489a0，2026-09-05）

调 LLM 报 400：`An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'`。

**根因链**：
1. 模型（deepseek-v4-flash thinking）单轮并发发 4 个 tool_call；
2. 其中 write_todos 的 arguments JSON 生成坏（键值二次转义 `""content""`）→ langchain `json.loads` 失败 → 记为 `invalid_tool_call`，工具节点不执行、无 tool 响应；
3. 下一轮 model 调用时 `langchain_openai 1.3.5 _convert_message_to_dict` 把 valid + invalid **合并**进 payload（tool_calls=4），但只跟了 3 条 tool 消息 → 400（本地转换实验已复现）。

## 挂点选择论证（docstring 核心，最具价值的部分）

| 候选挂点 | 结论 | 原因 |
|---|---|---|
| `before_agent`（deepagents 自带 PatchToolCalls 所在） | 不够 | 是 entry 节点，**每 run 只一次**——只能清理 run 起点存量悬空，够不着 run 中途新产生的 orphan |
| `wrap_model_call` | 不选 | override 只改出站 payload **不落 state**；要落 state 得走 ExtendedModelResponse+Command，消息会排到 model 回复之后（乱序） |
| `before_model` | ✅ 唯一正确 | 独立 graph 节点，每次 model 调用前必触发（START 与 tools 回环都先进它）；返回 `{"messages":[...]}` 经 add_messages reducer 落进 state 正确位置，本轮即被 model 读到，后续轮次不重复补 |

**只扫最后一个 AIMessage 的论证**：mid-run orphan 由模型本轮输出引入，只在紧随的下一次 before_model 处于"尾部 assistant"位置；若漏补则退居历史存量——那部分由 run 起点的 PatchToolCallsMiddleware 兜底。两者同步上线时 orphan 一定在产生当轮被补掉。扫描全部历史会造成 add_messages 落位错乱（插入点失控），故不做。

## 核心实现

`_collect_dangling_tool_messages(messages)`（纯函数，供单测）：
1. 倒序找最后一个 AIMessage；
2. 收集全部 ToolMessage 已应答的 `tool_call_id` 集合；
3. 分两次扫描：`last_ai.tool_calls`（valid 未应答 → "was cancelled" 模板）与 `last_ai.invalid_tool_calls`（→ "arguments were malformed or truncated" 模板）；
4. 兼容 dict / pydantic 两种 ToolCall 形态（按来源字段判别 invalid 而非 in-band type 键，版本更稳）。

合成消息统一 `status="error"`：OpenAI payload 序列化只取 content/role/tool_call_id，status 不入 payload，仅作语义标记（与 QueryGate/SqlReadOnly 的 deny 消息同构）。合成文本与 deepagents PatchToolCallsMiddleware 一致（可读且不触发 exec 误判）。

## 挂载与行为

- `nl2sql_agent._middleware` 与 `main_agent.middleware` 各一实例（两 graph 独立）。
- 每轮必触发但无 orphan 时零开销（返回 None）。
- fail-open：扫描异常只 warning，不影响正常流程。

## 附：为什么不选 `wrap_model_call`——langchain 源码核验（2026-09-18 补充）

上面表格中「override 只改出站 payload **不落 state**；要落 state 得走 ExtendedModelResponse+Command，消息会排到 model 回复之后（乱序）」这句话，已在本机安装的 langchain 源码（`.venv/Lib/site-packages/langchain/agents/`）中逐条核实：

### 数据流全景

```text
model_node (factory.py)
 │  request = ModelRequest(messages=state["messages"], state=state, ...)   ① 只是拷贝引用
 ▼
你的 wrap_model_call(request, handler)
 │  new_request = request.override(messages=...)                           ② 造新对象，原对象不动
 │  result = handler(new_request)                                          ③
 ▼
_execute_model_sync (factory.py L1406)
 │  output = model_.invoke(new_request.messages)   ← 改过的消息只发给模型    ④
 ▼
_build_commands (factory.py L193)
 │  Command(update={"messages": model_response.result})                    ⑤ 落盘内容=模型输出
 ▼
add_messages reducer 写入 checkpoint                                       ⑥
```

### 证据 1：override 是纯函数式「造新对象」（对应②）

`ModelRequest.override`（`middleware/types.py` L201-267）最后一行就是 `return replace(self, **overrides)`——dataclass `replace` 生成**全新** ModelRequest，docstring 明说 "leaving the original request unchanged"。整条链上没有任何代码把 override 后的 messages 写回 graph state。**结论：改动是"一次性"的，只影响本次 API 调用的出站 payload，本轮结束即消失，state 里的历史消息原封不动。**

### 证据 2：落 state 的唯一通道是模型输出（对应⑤）

`_build_commands`（factory.py L193-232）：第一个 Command 只装 `model_response.result`（本轮新生成的 AIMessage）；中间件经 `ExtendedModelResponse` 附带的 Command 只是**原样追加在后面**：

```python
commands: list[Command[Any]] = [Command(update=state)]        # 先：模型回复
commands.extend(middleware_commands or [])                     # 后：中间件 Command
```

### 证据 3：ExtendedModelResponse 的 Command 是"追加"不是"插入"（乱序根源）

`ExtendedModelResponse` docstring（types.py L289-300）官方表述：command 是 "applied as an **additional state update after the model node completes**"，其 messages "**added alongside** the model response messages"。而 `add_messages` reducer 只支持**尾部追加或按 id 原位覆盖**，不支持插入历史中间。所以经此通道补的合成 ToolMessage 在 state 里必然排成：

```text
[..., 孤儿AIMessage(tool_calls), 本轮新AIMessage, 合成ToolMessage]   ← 插不回孤儿消息后面
```

悬空修复语义要求 ToolMessage **紧跟孤儿 AIMessage**——这就是不选 `wrap_model_call` 的原因；`before_model` 是 model 调用**之前**的独立 graph 节点，返回 dict 经 reducer 落 state 位置天然正确。

### 类比与项目内对照

> override 是考试时改「题目卷」（发给模型的输入），state 是「成绩册」（checkpoint 存档）。改卷子不会自动记进成绩册；要记录必须走 Command「登记」通道，而登记只会记在最新一页（追加），塞不回中间某页（插入）。

- 利用「不落 state」特性的：`current_db_context` / `query_keywords`（临时注入不污染原始消息，每轮重做）、`thinking_toggle`（换 model）、`tool_filter`（改 tools 列表）。
- 需要落 state 的：`token_meter` 走 `ExtendedModelResponse + Command`（写 token_stats，追加语义无害）；而 dangling_tool_calls 对**位置敏感**，追加语义不可接受 → 只能挂 `before_model`。

### 追问：那能否 override 的同时也修改 state？（两条路径的实验核验，2026-09-18）

用最小复现实验（`create_agent` + `GenericFakeChatModel` + `InMemorySaver`，run 后从 checkpoint 重读 state）验证过两种做法，**均不可行**：

**路径 A：就地变异 `request.state`（绕过返回通道）**

```python
request.state["messages"].append(合成消息)   # 不经过任何合法更新通道
```

实验现象：改完再读 checkpoint，竟然"落盘"了（新 agent 实例读同一 saver 也看得到）——因为 `request.state` 就是 `model_node` 传入的当前状态对象**引用**（factory.py `state=state`），内存变异被收尾的 checkpoint 序列化捕获。但这是**未定义行为**：绕过 reducer、违反"节点只能经返回值报告更新"的框架契约，依赖对象别名这一实现细节，在并发写、replay、time-travel、跨 worker 恢复等场景下会丢更新或错乱，版本升级即碎。不可用。

**路径 B：正规通道 `override(修出站) + ExtendedModelResponse(Command)(落盘)` 双改**

模拟孤儿场景实测，最终 checkpoint 里的消息序：

```text
[0] HumanMessage: 查一下
[1] AIMessage: 上轮  tool_calls=['call_orphan_1']   ← 孤儿
[2] AIMessage: 本轮新回复                             ← 模型刚生成的
[3] ToolMessage: [SYNTH-TOOLMSG]                     ← Command 补的，落到最尾
```

合成 ToolMessage 确实落进了 state，但**排在 `[2]` 新回复之后、落在列表末尾**，而不是紧跟它要应答的孤儿 `[1]`——坐实了上面的"乱序"结论。对悬空修复而言位置错误即无效：下一轮若不再叠加 override，序列化出的 payload 仍是"tool 响应隔在下条 assistant 消息之后"，严格 provider 照样拒。

**归纳**：

| 做法 | 能落 state 吗 | 位置对吗 | 可用吗 |
|---|---|---|---|
| A 就地改 `request.state` | 内存里看似能 | 靠不住 | ❌ 未定义行为，违反契约 |
| B `override + Command` | ✅ 能 | ❌ 追加到末尾，非紧跟孤儿 | ❌ 对悬空修复无效 |
| **`before_model` 返回 dict** | ✅ 能 | ✅ model 调用前经 reducer 落在正确位置 | ✅ 唯一正解 |

核心矛盾：悬空修复要求"把消息**插入**历史中间某条 AIMessage 之后"这个精确位置，而 `wrap_model_call` 的两条落 state 通道都只能"尾部追加"——这个需求天然不属于 `wrap_model_call`。

### `request.state` 的真实用途：只读快照，不是写入口

上面路径 A 的诱惑正来自对 `request.state` 定位的误读。它的契约是**当前 Agent 完整状态的只读快照**（`ModelRequest.state: AgentState[Any]`，types.py L101），供中间件"看"而非"改"：

- **与 `request.messages` 是包含关系**：`messages` 只是发给模型的对话列表；`state` 里还有全部自定义 channel 字段（todos、token_stats、structured_response...），这些不在 messages 里，只能经 `state` 读到。
- **本项目实例**：`token_meter._current_round` 数 `request.state["messages"]` 里的 human 消息得轮号；`progress_boundary`（after_model）读 `state["todos"]` 判断怎么推进；`query_gate._active_db` 从 state 系统提示正则扫当前库建模标记。
- 合法的"改 state 视图"只有 `override(state=...)`——但同样只作用于本次出站请求链，依旧不落盘。

一句话闭合三个概念：

```text
request.state        →  读：当前状态快照，中间件据此判断（只读契约）
Command(update=...)  →  写：唯一合法的落 state 通道（经 reducer，只能追加/按 id 覆盖）
就地改 request.state  →  ✗ 绕过 reducer 的未定义行为，别用
```

### 版本漂移备注

源码 docstring 引用的落点 `factory.py:1234 _execute_model_sync → handle_model_output` 在当前安装版本中对应 `_execute_model_sync` @ L1406 与已改名的 `_handle_model_output` @ L1168——行号与函数名前缀随版本漂移，机制不变。

## 关联文件

- deepagents `PatchToolCallsMiddleware`（before_agent，run 级兜底，互补）
- langchain `factory.py` / `langchain_openai base.py`（机制事实来源）
