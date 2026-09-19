# progress_boundary.py 代码解读

> 文件路径：`src/agent/middlewares/progress_boundary.py`（312 行）
> 解读日期：2026-09-18

## 一句话概括

`ProgressBoundaryMiddleware`（层2 监督机）：在 `after_model` 阶段**绕过模型、确定性写 state.todos**，按工具调用里程碑自动推进 write_todos 进度，解决前端任务卡长查询期间冻结在最旧快照的问题。

## 解决的问题（实测）

nl2sql 子智能体里 write_todos 是模型自发工具，实测长查询模型只在「开头初始化 + 结尾一次全勾」各调一次，中间 5-6 分钟（schema 检索 / run_sql 执行 / 纠错循环）不动 → 前端任务卡冻结。**系统提示词 + WRITE_TODOS_PROTOCOL 大量纪律文本仍无效 → 纯提示词已证伪，必须确定性兜底**。

## 机制事实（2026-09-04 核实）

- `write_todos` 实为返回 `Command(update={"todos": ...})` 的普通工具；todos 是无 reducer 的 state channel。
- `after_model` 是挂在 model 节点之后的独立 graph 节点，返回值作为 state 更新并入（langchain 库 TodoListMiddleware 用同一通道）→ 本中间件可绕过模型确定性写 `{"todos": ...}`。
- after_model 在**本轮工具执行之前**运行 → 看到的是模型刚发出的 tool_calls。

## 阶段桶分类 `classify_tool`（按工具名子串，兼容 wrenai_<库>_ 前缀）

| 桶 | 序 | 工具 |
|---|---|---|
| _B_KNOW | 1 | get_all_knowledge / get_instructions / list_knowledge / **get_context / recall_queries / list_stored_queries** |
| _B_SCHEMA | 2 | describe_schema / describe_model / describe_cube / get_mdl / list_models / get_db_info / list_cubes / list_functions / get_data_source |
| _B_EXEC | 3 | run_sql / query_cube / dry_run / dry_plan |

注意：get_context/recall_queries 刻意归 KNOW 不归 SCHEMA——它们是「理解建模」的知识检索步骤，否则 merged 流程第一步 get_context 就把「理解建模-清晰度与知识」误勾掉。

## 推进规则（保守单向，绝不提前全勾，与提示词「todo 纪律铁律」一致）

1. 本轮 AI 消息已含 write_todos → **跳过**（尊重模型自己的更新，不打架）。
2. `_seen_bucket`：按线程单调记录已见最高桶（跨轮保留，防 auto-compress 剪消息后倒退；threading.Lock + 2000 上限超限清空）。KNOW 桶只记录不触发；**仅首次到达 schema/exec 新里程碑才推进**（纠错循环重跑 run_sql 不重复触发）。
3. **schema 里程碑**（`advance_todos`）：当前 in_progress 步 completed、下一未完成步 in_progress；当前步内容已是 schema 语义别名（_SCHEMA_PHASE_ALIASES：结构链接/表结构理解...）→ 不动（避免误跳）。
4. **exec 里程碑**：说明已越过纯推理步（Subproblem/Query Plan/SQL 生成/性能优化）→ 把当前 in_progress 起至「查询执行」语义项（_EXEC_PHASE_ALIASES）之前全部 completed、该项 in_progress；找不到语义项则保守只前进一步；当前已是执行阶段 → 不动。
5. 状态异常防御：>1 个 in_progress → 不动；无 in_progress → 只点亮第一个未完成项（不跳远）；已是最后一步 → 不动。**匹配不中 → 不动（退化为现状，不更糟）**。
6. 从不把最后阶段误勾 completed。

todos 项内容匹配用 `_norm`（去空白+小写）子串匹配，兼容中英文阶段名。

## 附加动作

推进成功后调 `agent.subagents.track_progress.record_todos_progress(tid, advanced)` 同步进度文件（供 sync 端耗时/step_history 连续）。

## 配套改动（docstring 提及）

`sync_subagent_todos._extract_subagent_todos` 改为**优先读子线程 state.todos**（authoritative 实时值），使本中间件的确定性更新能立刻镜像到前端。

## 边界

- thread_id 取不到 → 宁可不动（避免跨 run 串扰）。
- todos 为空 → 跳过（策略 B 合法跳过 write_todos）。

## 关联文件

- `write_todos.py`：提示词层（层1）纪律，本中间件是层2 兜底
- `agent/subagents/track_progress.py`、`sync_subagent_todos`：进度镜像链路
