# query_gate.py 代码解读

> 文件路径：`src/agent/middlewares/query_gate.py`（311 行）
> 解读日期：2026-09-18

## 一句话概括

`QueryGateMiddleware`：nl2sql 子 agent 查询执行前的**双规则确定性闸门**（`wrap_tool_call`）——规则一「通道硬闸」禁止已建模库走 dbmcp 直连，规则二「顺序软提醒」要求先完成理解建模再执行查询。

## 解决的问题（生产 trace bbb0eda0 实证）

nl2sql 子 agent 有两条查询通道：`wrenai_<库名>_*`（Wren 语义层）与 `dbmcp_*`（db_mcp_server 直连，绕过语义层、丢业务口径）。WIT 这类**已建模**库上模型仍直连 `dbmcp_run_sql` ×21 次——`dynamic_prompt` 的「查询通道路由」只是文本指引，**模型不遵守**；且原「先获取后执行」软闸被 `dbmcp_get_db_info` 满足后即全放行。故升级为**通道 + 顺序**双规则。

## 规则一：通道硬闸（每次必拦，非 once）

- 当前查询库**已在 Wren 语义层建模**时，`dbmcp_*`（run_sql 与 get_db_info 都算）**一律不执行**，返回指到 `wrenai_<库>_run_sql` / `{prefix}_get_data_source` 等语义工具的 error 消息（prefix 由 `semantic_db.wrenai_server_name(db)` 推导）。
- **目标库解析**（`_active_db`，返回 `(db_name, modeled)`）：
  1. 优先工具参数 `db_name`（dynamic_prompt 要求 dbmcp 直连必须传）→ `detector.is_modeled(dn)`；
  2. 兜底：正则 `_ACTIVE_DB_RE` 从 state 系统提示中扫「当前数据库: `X` —— 已在/未在 Wren 语义层建模」标记（与 dynamic_prompt 注入同源），取最后一个匹配。
- 取不到 → `("", False)`，调用方按未建模放行（**勿误伤未建模库的合法直连**——未建模库 dbmcp 是唯一查询通道）。

## 规则二：顺序软提醒（每线程至多一次）

- 触发：后获取工具（`run_sql`/`*_run_sql`/`dry_run`/`dry_plan`）执行前，`_ever_fetched` 反扫历史**所有** assistant 消息的 tool_calls，从未出现任何获取工具（get_context/get_instructions/recall_queries/describe_schema/get_mdl/get_db_info...）→ 拦下，返回 nl2sql-understand 顺序指导（含"[需要澄清] 先追问"提醒）。
- **同批并行天然放行**：最后一条 AIMessage 的 tool_calls 里可能既有 get_context 又有 run_sql，`_ever_fetched` 扫 messages 列表时会命中。
- **每线程至多一次**：`_REMINDED` set（threading.Lock + 4000 上限超限清空）——首次拦下给指导，此后该线程放行；不无限弹回、不 interrupt、不走 permission 卡。
- describe_model 也算获取证据（它只在 describe_schema/get_context 之后才被允许出现）。

## 豁免与兜底

- **Cube 通道完全豁免**（list_cubes/describe_cube/query_cube）：策略 C 无需 A/B 检索。
- thread_id 取不到 → fail-open 放行（与 fs_thread_guard 一致）。thread_id 优先 `request.runtime.execution_info`（子 agent 自己的线程，跨 run 自动隔离），兜底 configurable.trace_parent_thread_id/thread_id。
- 用户直接给出精确 SQL 时若被误拦，只浪费一轮（模型读指导后重发即放行），可接受。

## 与评分层的配合

`status="error"` + `Error:` 前缀让 `langfuse_span._maybe_score` 把它当 exec 失败（`looks_like_exec_error`），避免被误记成 `sql_exec_success=1`（与 SqlReadOnly._deny 同款约定）。

## 挂载顺序

`nl2sql_agent._middleware`，位于 sql_approval_middleware **之前**（LangfuseSpan 内层、SqlReadOnly 硬闸外层）→ 被拦的 run_sql 仍在 span 里可见，写/DDL 仍由硬闸兜底。与 `tool_filter.py` 构成纵深：tool_filter 让模型"看不见" dbmcp，QueryGate 对存量历史调用/幻觉工具名"拦得住"。

## 关联文件

- `tool_filter.py`：出站 payload 层的第一道（语义层独占）
- `sql_approval.py`：内层的只读硬闸
- `langfuse_span.py`：deny 消息评分识别约定
