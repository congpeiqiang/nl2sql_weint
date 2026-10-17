# dangling_tool_calls.py — DanglingToolCallsMiddleware 代码解读

> 源文件：`src/agent/middlewares/dangling_tool_calls.py`
> 类别：**上下文修复**
> 挂载点：`before_model` / `abefore_model`

## 1. 解决的问题

**生产 400 报错**：`"An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'"`。

**根因链**（trace `513489a0bdc46e3cc130489d90880bb2`，2026-09-05）：

1. 模型单轮并发 4 个 tool_call
2. 其中 `write_todos` 的 arguments JSON 生成坏（键值二次转义 `""content""`）
3. langchain `json.loads` 失败 → 记为 `invalid_tool_call`（id `call_00_klZo…`）
4. 工具节点不执行它 → 无 tool 响应
5. 下一轮 `langchain_openai` 1.3.5 把 valid + invalid **合并**进 payload `tool_calls=4`
6. 但只有 3 条 tool 消息 → 400

## 2. 挂点选择论证

| 挂点 | 评估 | 结论 |
|---|---|---|
| `before_agent`（deepagents `PatchToolCallsMiddleware`） | 每 run 只一次，只能清理 run 起点的存量悬空 | 兜底层 |
| `before_model`（本中间件） | 每次 model 调用前必触发，返回值经 `add_messages` reducer 落进 state 正确位置 | **主层** |
| `wrap_model_call` | override 只改出站 payload 不落 state；要落 state 需 `ExtendedModelResponse+Command`，消息会排到 model 回复之后（乱序） | 不选 |

## 3. 核心机制

### 3.1 纯函数扫描

`_collect_dangling_tool_messages(messages)` 只扫**最后一个 AIMessage**：

- mid-run orphan 由本轮输出引入，只在紧随的下一次 `before_model` 处于尾部位置
- 漏掉的成为历史存量，由 `PatchToolCalls.before_agent` 兜底
- 扫描全部历史会造成 `add_messages` 落位错乱，故不做

### 3.2 合成消息

| 来源 | 合成文本 |
|---|---|
| `invalid_tool_calls`（arguments 格式错误） | `"Tool call {name} with id {id} could not be executed - arguments were malformed or truncated."` |
| `tool_calls`（valid 但未应答） | `"Tool call {name} with id {id} was cancelled - another message came in before it could be completed."` |

均带 `status="error"`：
- 语义标记该 call 未能执行
- OpenAI payload 序列化只取 `content/role/tool_call_id`，`status` 不入 payload
- 与 QueryGate/SqlReadOnly 的 deny 消息同构

### 3.3 已应答 id 判定

```python
answered_ids = {
    msg.tool_call_id for msg in messages if isinstance(msg, ToolMessage)
}
```

遍历全部消息中的 ToolMessage（不只是最后一个 AIMessage 后面的），确保不误判已有响应的 call。

## 4. 返回值

- 有 orphan → 返回 `{"messages": patches}`，经 `add_messages` reducer 追加到消息尾部
- 无 orphan → 返回 `None`（零开销）
- 扫描异常 → fail-open，记 warning，返回 `None`

## 5. 与 deepagents PatchToolCalls 的协作

| 层 | 挂点 | 职责 |
|---|---|---|
| `PatchToolCallsMiddleware` | `before_agent` | 清理 run 起点的历史存量 orphan |
| `DanglingToolCallsMiddleware` | `before_model` | 清理 run 中途新产生的 orphan |

两者互补：orphan 在产生当轮被补掉，永不成为存量。

## 6. 关键设计决策

### 6.1 为什么只扫最后一个 AIMessage

- orphan 一定由最近的 AIMessage 引入
- 扫全部历史会合成重复消息（id 冲突）
- `add_messages` 按追加位置排列，全历史扫描导致插入点失控

### 6.2 为什么不修 langchain_openai 的合并行为

- `langchain_openai` 是第三方库，修改不可控
- 中间件层修补比改底层更稳
- 合成消息的成本远低于修框架的风险
