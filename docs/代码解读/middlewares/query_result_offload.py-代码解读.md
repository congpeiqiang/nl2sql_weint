# query_result_offload.py 代码解读

> 文件路径：`src/agent/middlewares/query_result_offload.py`（323 行）
> 解读日期：2026-09-18

## 一句话概括

`QueryResultOffloadMiddleware`：在数据表型工具（run_sql / query_cube）边界由**代码确定性**把大结果全量表转 markdown 落盘（0 模型开销），并把进 state 的消息瘦身为「前 N 行样例 + 文件指针」，从物理上杜绝模型重打全表。

## 解决的问题（生产故障，会话 01a064e1）

「有多少部门，每个部门人数」：子 agent 拿到 431 行 / 42.5KB 的 run_sql 结果后，在最终回复里**把整张表重新生成了一遍**（markdown），单次超长生成撞上模型 60s 超时 ×3 重试 = **228s 静默**（前端进度条冻结）。

**关键认知**：让模型「自己把全表写文件」行不通——`write_file(content=431行)` 仍要模型输出 431 行 tokens，照样撞 60s。正确治本 = 代码在工具边界代写 + 消息瘦身，模型手里只有 20 行样例 + 文件指针，**物理上不可能再全表重打**；最终回复改为「摘要 + Top20 样例 + 全量文件链接」（提示词侧 NL2SQL_SYSTEM_PROMPT.md 配合）。

## 触发条件（阈值以下行为不变，可完整贴表）

- `row_count > QUERY_RESULT_OFFLOAD_ROWS`（默认 50）**或** 文本 > `LARGE_RESULT_TRUNCATE_CHARS`（默认 8000，与 message_slimmer/langfuse_span 同源）。
- 样例行数 `QUERY_RESULT_OFFLOAD_PREVIEW`（默认 20）；落盘文件行数上限 `QUERY_RESULT_OFFLOAD_FILE_MAX_ROWS`（默认 10000，超出只存前 N 行并在文件内注明口径，防撑爆磁盘）。

## 适用工具

一切返回 `{columns, rows, row_count}` 的「数据表型」工具——权威清单在 `agent.utils.query_tools.DATA_TOOL_SUFFIXES`（`_run_sql` 与 Cube 快速通道 `_query_cube` 同构；本清单被落盘闸门、check_progress 指针收集、path_resolver 工具超时三处共用）。**历史教训**：2026-09-14 之前只认 run_sql → Cube 通道大结果既不落盘也无 full_result_file 指针 → 报告缺「完整数据表」节（同日同题对照：run_sql 报告 16087 字含全量明细，Cube 报告仅 8450 字模型自写汇总表）。

## 核心流程 `_maybe_offload`

1. content 归一化 → JSON 解析 → 校验标准 `{columns(list), rows(list)}` 结构（非标准/空 rows 不处理）；
2. `_write_full_table`：rows（兼容 records dict 与 list 行两种形态）转 markdown 全量表（`_esc_cell` 转义管道/折叠换行），写到与 langfuse_span._dump_process_data **同源目录**：
   `{active_workspace}/nl2sql_process_data/{session_thread_id}/query_result/{问题id前8位}_result-{seq}.md`
   （VFS 视角 `/workspace/nl2sql_process_data/...` 经 composite backend 路由可 read_file 读到同一文件）；
3. 消息替换为瘦身 JSON：保留原结构其它字段（statement_count 等），`rows` 截为前 20 样例（单元格 >300 字符截断，防"行数少但单格超大"撑爆预览）、`rows_truncated:true`、`full_result_file` 指针、`note` 内嵌行为指引（"只给结论/样例/路径，禁止全量重打、禁止 read_file 照抄"）。

## 注册顺序（关键）

必须注册在 `LangfuseSpanMiddleware` **之前（外层）**——langchain 按 first=outermost 组装（Request 流 first→last→tool，Response 流 tool→last→first）。外层在 handler(request) 返回**之后**做后处理 → LangfuseSpan 的 span output / LLM-judge / process_data dump 仍吃**原始全量 payload**，只有进 state 的消息被瘦身（与 MessageSlimmer 先于 LangfuseSpan 注册同构）。

## 安全

- 全程 fail-open：任何异常（含落盘 OSError）只记日志并返回原结果，绝不阻断 agent 循环。
- Command / interrupt 审批等非 ToolMessage 返回值不处理。

## 关联文件

- `message_slimmer.py`：通用版瘦身（本文件是数据表型专用版，策略不同：结构化样例+行为指引）
- `agent/utils/query_tools.py`：DATA_TOOL_SUFFIXES 权威清单
- `langfuse_span.py`：_thread_id/_question_id/_active_workspace_path 目录口径来源
