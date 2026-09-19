# message_slimmer.py 代码解读

> 文件路径：`src/agent/middlewares/message_slimmer.py`（294 行）
> 解读日期：2026-09-18

## 一句话概括

`MessageSlimmerMiddleware`：在工具结果**进入 state/checkpoint 之前**（`wrap_tool_call` 后处理）做两个动作——①截断超大 ToolMessage（全文落盘留指针）②去重完全重复的 ToolMessage（替换为小占位），治理 LangGraph checkpoint 膨胀。方案 B 第一阶段（L1，低风险高收益）。

## 解决的问题

LangGraph 每个 checkpoint 都存「截至当前的完整消息列表」→ **O(n²) 膨胀**。最大头实测：
- 单条 73.4KB 的 read_file 结果占消息总大小 24%；
- 完全重复的 tool 结果（2×14KB write_todos + 2×8.8KB SKILL 读取）。

## 动作 1：截断超大结果

- 阈值：文本 > `LARGE_RESULT_TRUNCATE_CHARS`（环境变量，默认 8000；main_agent 不再传死值）。**该变量同时被 query_result_offload / langfuse_span 共读**——保证「落盘阈值」与「span output 截断阈值」对齐，否则占位符里写的文件名是假的。
- 实现：复用 deepagents `_offload_tool_message_content`（同步）/ `_aoffload_tool_message_content`（异步）把完整内容落盘到 `large_tool_results/<tool_call_id>`，消息内只留 head+tail 预览 + 路径指针。
- 覆盖面：故意选在工具结果入口，**覆盖 read_file / execute 等 deepagents 主动驱逐豁免的工具**——那些正是本系统的膨胀源。
- 落盘前缀：backend 是 CompositeBackend 时用 `/workspace/large_tool_results`（命中 `/workspace/` 路由 → 前端选择的工作区后端，与 SummarizationMiddleware 溢出落盘、langfuse vfs 指针同路径口径）；否则 `/large_tool_results`。
- 构造参数 `max_chars_before_truncate=None` 可关闭截断（只做去重）。

## 动作 2：去重完全重复

- 判定：与线程内历史某条 ToolMessage **同工具名 + 文本 md5 全等**。
- 动作：content 替换为 `_DEDUP_STUB` 占位（引用首次出现的 tool_call_id，指路"向上查阅"），**保留 tool_call_id / name / id** → LangGraph tool_call 配对与前端 `deriveStepsFromSubMessages` 的步骤关联都不受影响。

## Command 形态处理

工具可能返回 `Command(update={"messages": [...]})`（如 write_todos）：
- `_unwrap_command_messages`：检测并摘出 `REMOVE_ALL_MESSAGES` 哨兵（首条 RemoveMessage 且 id 为哨兵）；
- 逐条瘦身其中的 ToolMessage；
- `_rewrap_command_messages`：还原哨兵，重建 Command（保留 goto/graph）。

## 边界与约束

- **异步路径必须 await** `_aoffload_tool_message_content`——协程对象泄漏进 messages 通道会导致 reducer 崩溃（注释显式提醒）。
- 不触碰 AI/Human 消息（AI 消息瘦身属 L2，暂缓）。
- 全程 fail-open：落盘失败保留原结果；wrap 异常时原始异常照常上抛（瘦身失败不阻断 agent）。
- **注册顺序**：在 main_agent 中先于 LangfuseSpan 注册（外层）——span 仍吃原始全量 payload，只有进 state 的消息被瘦身。

## 关联文件

- `query_result_offload.py`：同思路的数据表型专用版（0 预览保留结构不同）
- deepagents `_message_eviction.py`：落盘工具函数来源（文件名 sanitize 对齐）
- `langfuse_span.py`：阈值同源、vfs 指针口径同源
