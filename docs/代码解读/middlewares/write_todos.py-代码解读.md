# write_todos.py 代码解读

> 文件路径：`src/agent/middlewares/write_todos.py`（89 行）
> 解读日期：2026-09-18

## 一句话概括

`WriteTodosProtocolMiddleware`：通过 `wrap_model_call` 把「write_todos 分级使用协议」（`WRITE_TODOS_PROTOCOL` 常量）**追加**到系统提示词末尾——仅用于 nl2sql 子智能体，恢复「简单查询可跳过 todos」的默认行为、同时约束复杂查询必须按节奏维护 todos。

## 背景

`src/agent/middlewares/` 是项目自建目录。deepagents 基础栈自带 `TodoListMiddleware`，其默认指引写着「3 步以上的复杂多步任务才用 write_todos、simple 请求不要用」；此前为让前端进度条实时反映子智能体流水线阶段，**强制所有查询（哪怕 2 步）必须** write_todos。

**代价（2026-09-14 重新评估）**：简单查询（策略 B）为此多付 1 次初始化 + 1-2 次更新的 **LLM 轮次延迟**；而前端 `deriveStepsFromSubMessages` 已能从子线程工具调用序列**兜底推导步骤**，简单查询不再需要 todos 驱动进度。

## 协议内容要点（WRITE_TODOS_PROTOCOL）

1. **默认规则**：预计 3 步以上或多阶段 → 必须用；策略 A/C（7-8 步流水线：理解建模→结构链接→子问题→计划→生成→执行→优化→回答）必须维护；**策略 B（≤3 步：单表/单指标/明确 SQL）可跳过**，直接取数→回答。
2. **关键纠正——"中途更新是免费的"**：此前把"更新 todos"与"发起实质工具调用"错误二选一，导致任务卡冻结 2 分钟。正确做法：write_todos **必须**与完成该步的实质工具调用放在**同一条 assistant 消息**里并行发出（并行 tool_calls）；唯一禁止的是同一条消息里出现 ≥2 个 write_todos。
3. **更新时机**（与 `progress_boundary.py` 的确定性里程碑一致）：首个 schema 检索工具（describe_schema / wrenai_*_get_context / recall_queries / *_get_instructions）发出时 → 勾「理解建模」completed、「结构链接」in_progress；首个执行工具（*_run_sql / *_query_cube）发出时 → 纯推理步（Subproblem/Query Plan/SQL 生成/性能优化）整段勾 completed、「查询执行」in_progress。
4. content 保持稳定阶段名（与 write_todos 默认指引"任务完成即可移除"相反——这里为前端进度条刻意要求不删条目）。

## 实现

- `_append_protocol`：在 system_message.content 末尾拼接 `\n\n---\n\n{协议}`，**与框架默认指引兼容共存而非覆盖**；无 system_message 时新建（注意 SystemMessage 必填 content 参数）。
- 挂子智能体 middleware 列表**末尾**（协议追加在包括框架默认指引在内的所有 system 加工之后）。
- 主智能体不需要（todos 稳定，main_agent 未实例化）。

## 框架坑（注释显式记录）

`awrap_model_call` 中 handler 是协程，**必须 await**——否则把 coroutine 对象当 AIMessage 外泄，下游 `sync_subagent_todos._build_commands` 报 `'coroutine' object has no attribute 'result'`（2026-08-14 实证）。

## 关联文件

- `progress_boundary.py`：层2 确定性兜底（本协议是层1 提示词纪律）
- deepagents `TodoListMiddleware`：默认指引来源（协议与之兼容共存）
