# message_slimmer.py — MessageSlimmerMiddleware 代码解读

> 源文件：`src/agent/middlewares/message_slimmer.py`
> 类别：**上下文管理**
> 挂载点：`wrap_tool_call` / `awrap_tool_call`（后处理）

## 1. 解决的问题

LangGraph 消息列表 O(n²) 膨胀：实测单条 73.4KB `read_file` 占消息总大小 24%、还有 2×14KB `write_todos` 完全重复。大消息拖慢 LLM 调用、浪费 token、撞超时。

## 2. 两个动作

### 2.1 截断超大结果

文本 > 阈值（`LARGE_RESULT_TRUNCATE_CHARS`，默认 8000 字符）→ 复用 deepagents `_offload_tool_message_content` 把全文落盘到 `large_tool_results/<tool_call_id>`，消息只留 **head + tail 预览 + 路径指针**：

```
[内容过大，已截断。全文已保存到: /workspace/large_tool_results/call_xxx]

--- 前 4000 字符 ---
{头部内容}

--- 后 4000 字符 ---
{尾部内容}
```

落盘前缀：CompositeBackend 时用 `/workspace/large_tool_results`（路由到前端选择的工作区，与 Summarization 溢出、langfuse VFS 指针同口径）。

### 2.2 去重完全重复

新 ToolMessage 与历史某条**完全重复**（同工具名 + 文本 md5 全等）→ 内容替换为小占位：

```
[与 tool_call_id=call_yyy 的结果完全相同，已去重]
```

**保留** `tool_call_id` / `name` / `id`——tool 配对与前端步骤关联不受影响。

## 3. Command 返回值处理

`Command` 返回值（含 `update` 和 `goto`）需要特殊处理：

1. 解包 `command.update.messages`
2. 逐条执行瘦身逻辑
3. 保留/还原 `REMOVE_ALL_MESSAGES` 哨兵（LangGraph 内部用的清空标记）

异步路径注意必须 `await _aoffload_tool_message_content`——协程对象泄漏进 messages 通道会崩 reducer（2026-08-14 实证）。

## 4. 关键设计决策

### 4.1 为什么截断 head+tail 而非只 head

- tail 通常包含汇总信息（SQL 查询结果的最后几行、文件末尾的代码）
- head+tail 保留了结果的"形状"——模型知道开头是什么、结尾是什么
- 中间部分大多是重复数据行，丢失影响最小

### 4.2 为什么去重保留 tool_call_id

- LangGraph 要求每个 `tool_call_id` 都有对应的 ToolMessage
- 删掉消息会导致 `tool_call_id` 缺失 → 400 错误
- 替换内容保留 id → 配对关系完整

### 4.3 注册顺序

必须注册在 `LangfuseSpan` **之前（外层）**——langchain 按 first=outermost 组装：

```
请求流：MessageSlimmer → LangfuseSpan → 工具
响应流：工具 → LangfuseSpan → MessageSlimmer
```

LangfuseSpan 的 span output 吃到**原始全量 payload**，只有进 state 的消息被瘦身。

## 5. 阈值同源

`LARGE_RESULT_TRUNCATE_CHARS` 被三处共读：

| 使用者 | 用途 |
|---|---|
| `message_slimmer` | 截断触发阈值 |
| `query_result_offload` | 大结果落盘触发阈值 |
| `langfuse_span` | span output 截断阈值 |

注释明确记录过 16000 vs 8000 漂移导致 VFS 指针文件名造假的历史——三处必须读同一个常量。

## 6. 错误处理

全程 fail-open：

- 截断失败 → 保留原始内容
- 去重比较异常 → 不替换
- Command 解包失败 → 透传原 Command
- 异步落盘协程异常 → 捕获并记日志，不泄漏
