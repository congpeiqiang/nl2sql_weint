# trace_recorder.py 代码解读

> 文件路径：`src/agent/middlewares/trace_recorder.py`（195 行）
> 解读日期：2026-09-18

## 一句话概括

`TraceRecorderMiddleware`：本地自包含追踪——在模型/工具调用边界把事件写入 SQLite EventStore（`agent.trace.store`），生产不依赖 LangSmith/Langfuse 也能回溯 agent 执行过程；并负责**子 agent 谱系登记**（SUBAGENT_SPAWN）。挂载在中间件链**最外层**。

## 事件采集

### `wrap_model_call` → LLM_CALL_START / END / ERROR
- START：model 类名、messages 数、tools 数；
- END：duration_ms、input/output/total tokens（从响应 AIMessage 的 `usage_metadata` 提取）、message_count；
- 异常：记 LLM_CALL_ERROR（含 error_type + message）后**原样上抛**。

### `wrap_tool_call` → TOOL_CALL_START / END
- START：tool 名、args 摘要（截 200 字符）；
- 结果摘要 `_summarize_result`：ToolMessage 取 content 前 200；**Command 型返回值**（如 `start_async_task` 返回 Command）取 `update` 的 keys 列表。

## 子 agent 谱系登记（核心增值）

检测 `start_async_task`：从 `Command.update["async_tasks"]` 提取每个子任务的 `thread_id` / `task_instructions`，然后：
1. 记 **SUBAGENT_SPAWN** 事件（subagent_thread_ids + 任务指令截 200）；
2. `upsert_lineage(sub_thread_id, main_thread_id, task_instructions)` 建立**父子线程谱系**——后续按子线程 id 即可回溯到主会话。

## thread_id 策略

wrap_tool_call 拿不到 model request，无法直接取 state：
- LLM 调用时缓存 `_cached_thread_id`（来自 `request.state["messages"]` 最后一条消息的 `thread_id`）；
- 工具调用时**复用该缓存**；
- parent_thread_id 读 `configurable.trace_parent_thread_id`（deepagents_async_config_patch 注入——子 agent 事件中区分主/子线程）。

## 与 TokenMeter 的分工（防双写）

**不写 `state.token_stats`**——token 统计统一由 TokenMeterMiddleware 写 state（实测双写导致 reducer 累加重复、统计翻倍），本中间件**只记事件**（token 用量进事件 data 供回溯）。

## 容错

`_record_event` 全程 try/except，记录失败只 warning（`[TraceRecorder]`），绝不影响 agent 执行。

## 关联文件

- `agent/trace/store.py`：EventStore / upsert_lineage
- `token_meter.py`：token 写 state 的唯一出口
- `deepagents_async_config_patch.py`：trace_parent_thread_id 注入方
