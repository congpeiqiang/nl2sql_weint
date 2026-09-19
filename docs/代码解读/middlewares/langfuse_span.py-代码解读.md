# langfuse_span.py 代码解读

> 文件路径：`src/agent/middlewares/langfuse_span.py`（1156 行，middlewares 目录最重文件）
> 解读日期：2026-09-18

## 一句话概括

`LangfuseSpanMiddleware`：Langfuse 三层观测方案的 **② 结构化层 + ③ 存量数据层**——在关键工具边界包 skill 级 span（嵌套回主 trace），并承担 M3 五维评分、中间产物服务端代写、P1 评估单元证据采集。全部旁路容错，异常绝不影响工具执行。

## 背景（docs/langfuse平台/Langfuse接入实现方案.md §2.9）

- ① CallbackHandler 自动层已捕获每步 LLM/tool 调用，但按产物散落、未按 skill 分组，且**丢失 OTel 上下文后叶子调用成了无 session 的孤儿 trace**。
- 本中间件按确定性的产物/工具边界分段建 span：skill 名作 metadata（启发式，用于筛选）、小中间数据写 span output、vfs_path 写 metadata → UI 看结构摘要 + 磁盘文件看全量。

## 核心数据结构

### `TOOL_SKILL_MAP`（L61）：工具名后缀 → 启发式 skill 分类
schema-linking / knowledge-retrieval / recall-queries / cube-query / sql-execution / sql-generation / artifact-write / artifact-read。knowledge-retrieval 族不进 `_maybe_score` 分支 → 不产生打分。

### `_TOOL_OWNER_SKILLS`（L183）：工具 → 正向使用它的**真实编排 skill**
从各 SKILL.md 抽取（排除"不要/禁止"否定引用）。值长度 1 = 唯一归属 → 展示直接采用（**覆盖过期活动 skill**）；>1 = 共享工具（如 dry_run）→ 活动 skill 是 owner 才继承。owner 拼写必须 = 真实 skill 目录名（曾写错 `nl2sql-sql-of-thought` 导致同一 skill 两种 tag，按 skill 聚合被拆开）。

### `_THREAD_ACTIVE_SKILL`（L154）：进程级 thread→skill 表
deepagents 渐进披露要求执行某 skill 前先 read_file 其 SKILL.md——该调用是「当前在跑哪个 skill」的权威信号；但模型常不重读 SKILL.md 就切换（活动 skill 会过期），故配合 owner 表纠偏。

## 上下文取值函数群（多级优先级兜底）

| 函数 | 用途 | 优先级链 |
|---|---|---|
| `_thread_id` | **会话级**线程 id（session 分组） | metadata.langfuse_session_id（HTTP 层注入的唯一权威）→ configurable.trace_parent_thread_id → configurable.thread_id → execution_info（子 agent 场景是子线程，故放最后） |
| `_exec_thread_id` | 实际执行线程（LLM-judge 读问题用） | execution_info.thread_id（与 _thread_id 刻意区分） |
| `_parent_trace_id` / `_parent_obs_id` | 子线程嵌套主 trace | 读 deepagents patch 注入的 metadata（异步线程 OTel context 已丢失） |
| `_question_id` | 问题归属（每问题=一条 chat-turn trace） | langfuse_parent_trace_id → langfuse_trace_id → 当前 OTel span |
| `_active_workspace_path` / `_data_root_path` | 落盘根 | metadata.workspace.path → workspace_manager |

## 主流程 `_invoke` / `_invoke_async`（wrap_tool_call）

1. `_classify_skill` 未命中 → 直接透传（零开销）。
2. `_resolve_display_skill` 定展示名，5 级优先：read/write_file 命中 SKILL.md（权威，且记为活动 skill）> 文件读写未命中 SKILL.md（归当前活动 skill）> 工具唯一归属（覆盖 stale）> 共享工具（活动 skill 在 owners 内才继承）> 启发式兜底。**评分仍走 heuristic，展示与评分解耦**。
3. `_start_span` → 执行工具 → `_finish_span` / `_dump_process_data` / `_record_subject_evidence` / `_maybe_score`。

### `_start_span` 三条嵌套路径（M-T2/T3b/T6/T6b/T8b 系列修复的沉淀）
- **Path A（子 agent skill span）**：metadata 有 `langfuse_parent_trace_id` → `trace_context={trace_id, parent_span_id}` 显式嵌套。parent_obs_id 优先读 `_ROOT_OBS_MAP`（此时已被子 agent on_chain_start 写入 → 形成 chat_agent→nl2sql_agent→skill 标准层级），兜底读 metadata 快照（`_wrap_runs_create` 在 orig_create 前抓的主 agent root obs）。
- **Path C（主 agent 直调工具）**：先探 OTel 上下文——仍活跃则自然挂载（不显式嵌套，防 v4 误判 root）；已丢失才用 `_THREAD_TRACE_MAP[thread_id]`（会话最近新查询 trace）显式嵌套，再兜底 `handler.last_trace_id`（有跨会话竞态，仅 map 未命中时用）。
- **无 parent**：裸 `start_observation` + 给根 span 设 `session.id`/`langfuse.trace.tags` OTel 属性（exporter 从根 span 读作 trace 的 session，孤儿兜底）。

两个 v4 兼容补偿：
- **M-T8b**：显式 trace_context 的 span 父观测早于它导出 → 被误判 is_app_root → Traces 列表同一 trace 多一行；创建瞬间 `_attach_app_root_claim`（baggage 认领）触发 suppressed_by_parent_claim 归巢。
- **M-T6b 修复 A**：v4 trace 名 = 最新 root observation 的 trace_name → 给显式嵌套 span 补设 `langfuse.trace.name` 属性；path A 必须用常量 `_MAIN_TRACE_NAME="chat-turn"`（子上下文里 metadata 的名是 "nl2sql-agent"，直接读会污染主 trace 名）。

`_finish_span`：output ≤ `LARGE_RESULT_TRUNCATE_CHARS`（与 MessageSlimmer 读**同一环境变量**，历史曾因 16000/8000 漂移导致占位符里文件名造假）→ 结构化 payload 直进 span（UI 可折叠 JSON 树）；超限 → 写 `[large result truncated, see /workspace/large_tool_results/<tool_call_id>]` vfs 指针。

### `_result_payload` 细节
DB 工具返回 AIMessage（content=[{'type':'text','text':'<结果JSON>'}]），直接 str(content) 是 Python repr（单引号）UI 无法折叠——解析 content-block 内层 text 为 dict/list 再上送。

## 旁路评估

### `_maybe_score`（M3 五维评分，NL2SQL_EVAL_ENABLED 总开关）
- `sql-execution`：`sql_valid_score`（静态规则）+ `sql_exec_success`（注意 dbmcp 把 DB 报错当**返回值**非异常——须 `looks_like_exec_error` 从结果文本识别；被 QueryGate 拦截的 deny 也因 `Error:` 前缀被记为失败）；执行成功且 `should_sample()` → 调度 LLM-judge `sql_biz_correct`（失败不调，省成本，badcase 按 exec_success=0 命中）。
- `schema-linking`：`schema_match_score`。
- `artifact-write/read` 且 `_is_report_artifact`（路径含 report 或 .md/.txt 结尾，**排除 SKILL.md**——否则读 skill 指令会误打 report 分）→ report 维度 judge，正文取 write_file 的 `content` **参数**（返回值只是"已写入"确认）。

### P1 评估单元（`_record_subject_evidence` + `after_agent._assemble_subject`）
- 子线程工具边界：run_sql 完整数字载荷 / 报告头部写**证据 sidecar**（主线程看不到子线程未瘦身数据——check 摘要只有文本且 MessageSlimmer 已瘦身），subject_id=`_question_id()` 归一到同一次查询。
- 主 agent run 收尾：读证据 + 消息组装 subject 落盘；`agent_name != "chat_agent"` 直接空转（子图实例安全）。auto-continue 多 run 幂等：非终结 run 组不出 subject → 不写。
- **落盘根在 data_root 而非 workspace**（S2-2）：对应 VFS `/eval_runs/...`，在 /shared、/workspace 之外，静态层 deny——物理封堵在线 agent 读到 eval 参考答案（泄题）。

### `_dump_process_data`（服务端代写中间产物）
`NL2SQL_PROCESS_DATA_DUMP`（默认开）：模型不一定 write_file，但 span 已捕获 input/output → 工具边界直接代写 `nl2sql_process_data/{thread}/{skill}/{qid_}{tool}-{seq}.json`，让 vfs_dir 指向的目录真实存在可排查。`NL2SQL_PROCESS_DATA_DUMP_OUTPUT` 默认关（run_sql 结果可能极大会撑爆磁盘，落盘只留 `_output_marker` 行数标记，完整 output 在 Langfuse span 里）；单字段 >8MB 截断为 `{_truncated, size_chars, head}`。artifact-write/read 启发式跳过（产物本身已落盘不重复）。

## 挂载与顺序

- 挂 nl2sql_agent 与 main_agent 两处（`agent_name` 区分：nl2sql_agent / chat_agent）。
- **必须注册在 MessageSlimmer / QueryResultOffload 之后（内层）**：langchain first=outermost，外层后处理看到的是未瘦身原始 payload → span/评分/落盘吃全量，只有进 state 的消息被瘦身。
- QueryGate 在其内层（SqlReadOnly 外层）：被拦的 run_sql 仍在 span 可见。

## 关联文件

- `deepagents_async_config_patch.py`：metadata.langfuse_parent_* 的注入方
- `agent/trace/langfuse_client.py`：client / handler 单例 / _ROOT_OBS_MAP / register_task_trace_context
- `agent/eval/evaluators.py`、`agent/eval/eval_subject.py`：评分与评估单元实现
- `query_result_offload.py` / `message_slimmer.py`：注册顺序约束对象、阈值同源
