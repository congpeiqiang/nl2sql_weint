# src/agent/middlewares 目录代码解读

> 目录路径：`src/agent/middlewares/`
> 解读日期：2026-09-18
> 共 19 个 Python 文件。本目录是 NL2SQL 系统的 **Agent 中间件层**：基于 LangChain 1.x `AgentMiddleware`
> 的 hook（`before_model` / `after_model` / `wrap_model_call` / `wrap_tool_call` / `after_agent`）
> 与 Monkey-Patch，实现配置注入、安全护栏、上下文瘦身、执行监督、错误翻译、可观测性六大类能力。

## 总览：文件分类

| 类别 | 文件 | 挂载点 | 一句话职责 |
|---|---|---|---|
| 配置注入 | current_db_context.py | wrap_model_call | 当前库名注入最新用户消息，防切库后模型按旧库委派 |
| 配置注入 | query_keywords.py | wrap_model_call | 前端关键词动态替换系统提示词触发词，前后端一致 |
| 配置注入 | thinking_toggle.py | wrap_model_call | 前端单开关控制模型思考 + 模型路由切换（带缓存） |
| 工具面控制 | tool_filter.py | wrap_model_call | 按当前库过滤 wrenai/dbmcp 工具，语义层独占 |
| 安全护栏 | execute_guard.py | wrap_tool_call | shell 命令护栏：禁破坏性命令与工作区外路径 |
| 安全护栏 | sql_approval.py | wrap_tool_call | SQL 只读硬拦截 + Wren LIMIT/Cube 窗口参数归一 |
| 安全护栏 | filesystem_thread_guard.py | wrap_tool_call | 子 agent 禁止跨线程读 process_data 中间产物 |
| 执行监督 | query_gate.py | wrap_tool_call | 查询通道硬闸 + 「先理解后执行」顺序软闸 |
| 执行监督 | progress_boundary.py | after_model | 确定性推进 write_todos 进度（防任务卡冻结） |
| 执行监督 | write_todos.py | wrap_model_call | 追加 write_todos 分级使用协议到 system prompt |
| 上下文管理 | message_slimmer.py | wrap_tool_call | 超大 tool 结果截断落盘 + 完全重复结果去重 |
| 上下文管理 | query_result_offload.py | wrap_tool_call | 大查询结果表代码代写落盘 + 消息瘦身为样例+指针 |
| 上下文管理 | vfs_path_resolver.py | wrap_model_call 后处理 | 模型输出里的 VFS 路径改写为真实磁盘路径 |
| 上下文修复 | dangling_tool_calls.py | before_model | 为孤儿 tool_call 补合成 ToolMessage，防 400 |
| 错误翻译 | model_timeout.py | wrap_model_call | LLM 超时 → 友好中文 AIMessage（界面不卡） |
| 错误翻译 | quota_error.py | wrap_model_call | 额度耗尽 → 友好中文 AIMessage（界面不卡） |
| 可观测性 | langfuse_span.py | wrap_tool_call + after_agent | skill 级 span、五维评分、中间产物落盘、评估单元 |
| 可观测性 | token_meter.py | wrap_model_call | 每次 LLM 调用 token/耗时采集，累积到 state |
| 可观测性 | trace_recorder.py | wrap_model/tool_call | 本地 SQLite 事件追踪 + 子 agent 谱系记录 |
| Monkey-Patch | deepagents_async_config_patch.py | 模块导入时 | 透传父 configurable 与 Langfuse 上下文到异步子 agent |
| （包声明） | __init__.py | - | 空模板文件，无逻辑 |

整体中间件链的相对顺序约束散见于各文件注释，核心三条：
1. `QueryResultOffload` / `MessageSlimmer` 必须注册在 `LangfuseSpan` **之前（外层）**——保证 span 吃到原始全量 payload，只有进 state 的消息被瘦身。
2. `QueryGate` 位于 `sql_approval`（SqlReadOnly）之前——被拦的 run_sql 仍在 span 里可见，写/DDL 由硬闸兜底。
3. `deepagents_async_config_patch` 必须在 `create_deep_agent` 之前导入。

---

## 1. `__init__.py`

PyCharm 模板生成的空包声明文件（仅 docstring 头注释），无任何逻辑。

---

## 2. `current_db_context.py` — CurrentDbContextMiddleware

**解决的问题**：实证 bug（2026-08-11）——前端切库到 clickhouse 后问"有多少表"，system prompt 已写明当前库，但模型采信对话历史里自己上轮说的"当前数据库是 imdb"，导致委派子任务时写错库名。

**机制**：每次模型调用前（`wrap_model_call`），把 `configurable.db_name` 以 `【当前数据库：{db}】` 前缀**拼进最新一条用户消息头部**——用户消息是当轮最高优先级信号，无法被陈旧历史覆盖。

**关键细节**：
- 读 db_name 必须走 `langgraph.config.get_config()`：langchain 官方 `ModelRequest.runtime.config` 恒为空 dict（实证），代码保留 runtime 路径仅作旧框架兜底。
- **只当最新消息是 HumanMessage 时注入**：工具回环/续跑/auto-continue 阶段（最新消息非用户消息）不注入，避免污染。
- 幂等：content 已以前缀开头则跳过；content 为 block 列表时在末尾追加文本块。
- 注入时用 `id=last.id` 重建消息 → add_messages reducer 原位替换而非追加。

---

## 3. `dangling_tool_calls.py` — DanglingToolCallsMiddleware

**解决的问题**：生产 400 报错 "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'"。根因链：模型单轮并发 4 个 tool_call，其中一个（write_todos）arguments JSON 生成坏 → langchain 记为 `invalid_tool_call` 不执行 → 下一轮 `langchain_openai` 把 valid+invalid **合并**进 payload（4 个 tool_calls）但只有 3 条 tool 响应 → 400。

invalid_tool_call也需要对应的toolmessage

**挂点选择的论证**（docstring 核心内容）：
- deepagents 自带的 `PatchToolCallsMiddleware` 挂在 `before_agent`——**每 run 只一次**，只能清理 run 起点的存量悬空，够不着 run 中途新产生的 orphan。
- 本中间件挂 `before_model`——每次 model 调用前必触发，且返回值经 add_messages reducer **落进 state 正确位置**（后续轮次不再重复补）。
- 不选 `wrap_model_call`：那里 override 只改出站 payload 不落 state；要落 state 需 ExtendedModelResponse+Command，消息会排到 model 回复之后（乱序）。

**机制**：纯函数 `_collect_dangling_tool_messages` 只扫**最后一个 AIMessage**——mid-run orphan 由本轮输出引入，只在紧随的下一次 before_model 处于尾部位置；漏掉的成为历史存量由 PatchToolCalls 兜底。对未应答的 id：invalid → 合成 "arguments were malformed" 消息；valid 但未应答 → 合成 "was cancelled" 防御消息。均带 `status="error"`（不入 OpenAI payload，仅语义标记）。

**设计**：fail-open（扫描异常只 warning）；无 orphan 零开销；合成文本与 deepagents 保持一致。

---

## 5. `execute_guard.py` — ExecuteGuardMiddleware

**解决的问题**：deepagents `LocalShellBackend` 用 `subprocess.run(shell=True)` 直跑宿主机，`FilesystemPermission` 只管文件工具**管不住 execute**——实测 agent 用 `execute("rm /tmp/xxx.txt")` 绕过文件权限删了工作区外文件。

**两条拒绝规则**（`_check_command`）：

1. **破坏性命令**：正则词边界匹配 rm/rmdir/del/erase/format/fdisk/shutdown/taskkill/kill/remove-item/rmtree 等（POSIX + Windows），词边界设计避免误伤 "delete"/"model"。
2. **工作区外路径**（`EXECUTE_GUARD_STRICT=0` 可关）：从命令中提取 POSIX `/xxx`、`~/xxx`、Windows `X:\xxx`、越界 `../` token，判断是否落在「活跃工作区 / /shared / /workspace VFS 前缀」之外；解析失败按越界处理（宁可多拦）。

被拒返回 `status="error"` 的 ToolMessage 带中文原因，LLM 可读后自我修正，不死循环。docstring 明确定位：**这是护栏不是安全边界**（命令混淆可绕过），彻底隔离需换沙箱后端（OpenSandboxBackend）。

---

## 6. `filesystem_thread_guard.py` — FilesystemThreadGuardMiddleware

**解决的问题**（同题多跑不一致归因 S2-1）：nl2sql 子 agent 此前可读整条 `/workspace/**`，能翻到**其它会话**的 `conversation_history/`、`report/`、`nl2sql_process_data/{其它线程}/`（含 eval 参考答案）→ 跨线程上下文泄漏，答案变成"之前哪次会话怎么答的"的函数。

**两层治理中的运行时层**（静态层在 `file_permissions.py` 收窄读范围）：静态规则无法按线程 id 限权（线程 id 运行时才知），故在工具调用边界裁决——对 `/workspace/nl2sql_process_data/{thread}/...` 的读，仅 `thread == 当前会话线程 id` 放行。

**关键一致性设计**：
- 线程 id 与 `langfuse_span._thread_id` **同源**（metadata.langfuse_session_id → trace_parent_thread_id → configurable.thread_id → execution_info.thread_id），保证"自有目录"判定与产物落盘目录用同一把尺子。
- 只管 4 个只读工具（read_file/ls/glob/grep）；写工具由静态层收口。
- 放行场景：不在 process_data 下（交静态层）、ls 顶层目录（只暴露线程名无内容）、自有目录、线程 id 解析不到（fail-open）。
- deny 消息明确指路：业务口径只准来自语义层 MCP 工具，禁止翻其它会话产物。

---

## 7. `langfuse_span.py` — LangfuseSpanMiddleware（1156 行，目录内最重文件）

Langfuse 三层观测方案中的 **② 结构化层 + ③ 存量数据层**（① CallbackHandler 自动层已有）。在关键工具边界包 skill 级 span，同时承担评分与产物落盘。

### 核心数据结构
- `TOOL_SKILL_MAP`：工具名后缀 → **启发式 skill 分类**（schema-linking / sql-execution / recall-queries / cube-query / artifact-write...），用于筛选与评分定位。
- `_TOOL_OWNER_SKILLS`：工具 → **正向使用它的真实编排 skill**（从各 SKILL.md 抽取）。值长度 1 = 唯一归属可直接覆盖过期活动 skill；>1 = 共享工具需活动 skill 在 owners 内才继承。
- `_THREAD_ACTIVE_SKILL`：进程级 thread→skill 表。deepagents 渐进披露下 read_file 命中 `.../{skill}/SKILL.md` 是"当前在跑哪个 skill"的权威信号，但模型常不重读就切 skill，所以需要 owner 表纠偏。

### 上下文取值函数群（都带多级优先级兜底）
- `_thread_id`：**会话级**线程 id（session 分组唯一权威：metadata.langfuse_session_id → trace_parent_thread_id → configurable.thread_id → execution_info 兜底），与执行线程 id 区分。
- `_parent_trace_id` / `_parent_obs_id`：读 deepagents patch 注入的 metadata——异步子线程 OTel context 已丢失，靠显式注入嵌套回主 trace。
- `_question_id`：每问题 = 一条 chat-turn trace，同会话多问题的产物靠它区分归属（文件名前缀）。

### 主流程 `_invoke`（wrap_tool_call）
1. `_classify_skill` 不命中的工具直接透传（零开销）。
2. `_resolve_display_skill` 定展示名（优先级：SKILL.md 读取信号 > 文件读写归活动 skill > 唯一归属 > 共享工具条件继承 > 启发式兜底）。
3. `_start_span`：**三条嵌套路径**——
   - **Path A**（子 agent）：metadata 有 langfuse_parent_trace_id → trace_context 显式嵌套到主 trace；parent_obs_id 优先读 `_ROOT_OBS_MAP`（此时是子 agent root obs → 形成 chat_agent→nl2sql_agent→skill 层级），兜底读 metadata 快照。
   - **Path C**（主 agent 直调工具）：OTel 上下文仍在则自然挂载；丢失时用 `_THREAD_TRACE_MAP`（会话线程→最近新查询 trace）显式嵌套，再兜底 handler.last_trace_id。
   - 无 parent → 裸 start_observation + session.id 属性兜底。
   - 两个 v4 兼容补偿：M-T8b `_attach_app_root_claim` 防归巢 span 被误判 is_app_root 产生重复 UI 行；M-T6b 给显式嵌套 span 设 `langfuse.trace.name` 属性防 v4 用 skill span 名污染主 trace 名（path A 必须用常量 `_MAIN_TRACE_NAME="chat-turn"`）。
4. 工具执行后：`_finish_span`（output ≤ LARGE_RESULT_TRUNCATE_CHARS 直接进 span，超限写 vfs 指针——与 MessageSlimmer 同源对齐）、`_dump_process_data`（服务端代写中间产物，0 模型开销）、`_record_subject_evidence`、`_maybe_score`。

### 旁路评估（全部 try/except，异常绝不影响工具执行）
- `_maybe_score`（M3 五维评分）：sql-execution → `sql_valid_score` + `sql_exec_success`（注意 dbmcp 把 DB 报错当返回值，需 `looks_like_exec_error` 从文本识别）+ 成功抽样调度 LLM-judge；schema-linking → `schema_match_score`；artifact 类且是报告文件（排除 SKILL.md）→ report 维度 judge（正文取 write_file 的 content 参数而非返回值）。
- `_record_subject_evidence` + `after_agent._assemble_subject`（P1 评估单元）：子线程工具边界把 run_sql 完整载荷/报告头部写 sidecar（主线程看不到子线程未瘦身数据），主 agent run 收尾读证据组装 subject 落盘；落盘根在 **data_root 而非 workspace**（`/eval_runs/...`，VFS deny 范围外，物理封堵在线 agent 读到参考答案泄题）。非终结 run 组不出 subject → 天然幂等。

---

## 8. `message_slimmer.py` — MessageSlimmerMiddleware

**解决的问题**：LangGraph消息列表（O(n²) 膨胀），实测单条 73.4KB read_file 占消息总大小 24%、还有 2×14KB write_todos 完全重复。

**两个动作**（都在工具结果进 state 之前，`wrap_tool_call` 后处理）：

1. **截断超大结果**：文本 > 阈值（`LARGE_RESULT_TRUNCATE_CHARS`，默认 8000）→ 复用 deepagents `_offload_tool_message_content` 把全文落盘到 `large_tool_results/<tool_call_id>`，消息只留 head+tail 预览+路径指针。落盘前缀区分：CompositeBackend 时用 `/workspace/large_tool_results`（路由到前端选择的工作区，与 Summarization 溢出、langfuse vfs 指针同口径）。
2. **去重完全重复**：新 ToolMessage 与历史某条（同工具名 + 文本 md5 全等）→ 内容替换为小占位（引用首次出现的 tool_call_id），**保留 tool_call_id/name/id**，tool 配对与前端步骤关联不受影响。

还处理了 `Command` 返回值形态：解包 update.messages 逐条瘦身，并保留/还原 `REMOVE_ALL_MESSAGES` 哨兵。异步路径注意必须 await `_aoffload_tool_message_content`（协程对象泄漏进 messages 通道会崩 reducer）。全程 fail-open。

---

## 9. `model_timeout.py` — ModelTimeoutMiddleware

**解决的问题**：模型 API 超时（httpx ReadTimeout / openai APITimeoutError）被 LangGraph 序列化进 run stream 的 error 事件，而前端 SDK 不渲染 `stream.error` → 表现为"卡住、无提示"（trace ef83792 实证：4×60s 重试后界面空白）。

**机制**：与 QuotaErrorMiddleware 同构——**不抛异常**，捕获超时后返回一条带友好中文文案的 AIMessage（`ModelResponse`）。AIMessage 不带 tool_calls → 模型节点后直接 END，不触发工具节点、不会缺工具结果再抛错；消息写入 checkpoint，刷新仍可见。

**超时识别** `is_timeout_error`：四级判定链（httpx.TimeoutException → 内置 TimeoutError → 类名含 Timeout → 正则匹配消息文本），并**递归遍历 `__cause__`/`__context__` 异常链**（带 seen 集合防环）。其他错误原样上抛保持失败语义。

---

## 10. `progress_boundary.py` — ProgressBoundaryMiddleware

**解决的问题**：实测长查询模型只在开头初始化 + 结尾一次全勾 write_todos，中间 5-6 分钟不动 → 前端任务卡冻结最旧快照。提示词纪律已证伪，必须**确定性兜底**（层2 监督机）。

**机制**（`after_model`，在本轮工具执行**之前**运行，看到模型刚发出的 tool_calls）：
1. 本轮 AI 消息已含 write_todos → 跳过（尊重模型，不打架）。
2. 按工具名分桶判阶段：knowledge(get_all_knowledge/get_context/recall_queries...) < schema(describe_schema/get_mdl/list_models...) < exec(run_sql/query_cube/dry_run...)。注意 `get_context/recall_queries` 归 KNOW 不归 SCHEMA——否则 merged 理解建模第一步就误勾掉第一个 todo。
3. `_seen_bucket`：按线程单调记录已见最高桶（跨轮保留，防 auto-compress 剪消息后倒退；带锁 + 2000 上限清空防泄漏），**只有首次到达新里程碑才推进**（纠错循环重跑 schema/run_sql 不重复触发）。
4. `advance_todos`（纯函数供单测）：schema 里程碑 → 当前步 completed、下一未完成步 in_progress（当前已在 schema 语义项则不动）；exec 里程碑 → 跳过纯推理步，把 in_progress 起到「查询执行」语义项之前全 completed、该项 in_progress，找不到则保守前进一步。
5. **保守单向**：pending→in_progress→completed，绝不提前全勾最后阶段；状态异常（>1 个 in_progress）或匹配不中 → 不动（退化现状不更糟）。

推进成功后同步调 `record_todos_progress` 写进度文件，供 sync 端耗时/step_history 连续。

---

## 11. `query_gate.py` — QueryGateMiddleware

nl2sql 子 agent 查询执行前的**双规则确定性闸门**（`wrap_tool_call`）。

**规则一：通道硬闸（每次必拦）**——生产 trace 实证：WIT 这类已建模库上模型仍直连 `dbmcp_run_sql` ×21 次（prompt 路由只是文本指引，模型不遵守）。当前查询库**已在 Wren 语义层建模**时，`dbmcp_*`（run_sql 和 get_db_info 都算）一律不执行，返回指到 `wrenai_<库>_run_sql` 的 error 消息。目标库解析：优先工具参数 `db_name` → 兜底用正则从 state 系统提示里读 dynamic_prompt 注入的「当前数据库: `X` —— 已在/未在 Wren 语义层建模」标记。未建模库仍放行 dbmcp（唯一查询通道）。

**规则二：顺序软提醒（每线程至多一次）**——run_sql/dry_run/dry_plan 前，若历史所有 assistant tool_calls 中从未出现任何"获取工具"（`_ever_fetched`，含同批次并行的 get_context，天然放行同批并行），拦下并给出 nl2sql-understand 顺序指导；`_REMINDED` set（带锁 + 4000 上限）记录后放行，不无限弹回、不 interrupt。

**豁免与兜底**：Cube 通道三工具完全豁免（策略 C）；thread_id/库标记取不到 → fail-open；`status="error"` + `Error:` 前缀让 langfuse_span 把它记为 exec 失败而非成功。

---

## 12. `query_keywords.py` — QueryKeywordsMiddleware

**解决的问题**：「前后端关键词一致性」——前端 localStorage 配置的查询触发关键词要同步影响 LLM 的委派判断。

**机制**：每次模型调用前读 `configurable.query_keywords`（同样必须走 `langgraph.config.get_config()`，runtime.config 恒空），**替换系统提示词中 `**触发关键词**【数据查询】:` 标记行**；无标记行则追加兜底。marker 特意带【数据查询】限定——文档处理/报告生成/图表可视化的触发词行不能被替换，否则破坏意图识别。列表形态用「、」join；缺失回退与提示词一致的默认关键词。

---

## 13. `query_result_offload.py` — QueryResultOffloadMiddleware

**解决的问题**（生产会话 01a064e1）：子 agent 拿到 431 行/42.5KB 结果后，在最终回复里**重新生成整张 markdown 表**，单次超长生成撞 60s 超时 ×3 = 228s 静默。关键认知：让模型自己 write_file 全表行不通（仍要输出 431 行 tokens）——**正确治本 = 代码在工具边界代写**。

**机制**（`wrap_tool_call` 后处理，仅针对返回 `{columns, rows, row_count}` 的数据表型工具，`is_data_tool` 覆盖 run_sql 与 query_cube——2026-09-14 前只认 run_sql 导致 Cube 报告缺全量表）：
- 触发：row_count > 50（`QUERY_RESULT_OFFLOAD_ROWS`）或文本 > 8000；阈值以下行为不变。
- 动作：全量 rows 转 markdown 落盘 `{workspace}/nl2sql_process_data/{thread}/query_result/{qid}_result-{seq}.md`（目录口径与 langfuse_span._dump_process_data 同源），进 state 的消息替换为瘦身 JSON：`{columns, row_count, rows:[前20样例], rows_truncated, full_result_file, note}`——note 内嵌行为指引"只给结论+样例+文件链接，禁止全表重打/禁止 read_file 照抄"。
- 样例单元格超 300 字符截断（防行数少但单格超大撑爆预览）；落盘文件行数超 10000 只存前 N 并注明。
- **顺序关键**：必须注册在 LangfuseSpan **之前（外层）**——外层后处理在 handler 返回后执行，span/score/process_data 仍吃原始全量 payload，只有进 state 的消息被瘦身。
- 落盘失败 fail-open 保留原结果。

---

## 14. `quota_error.py` — QuotaErrorMiddleware

与 ModelTimeoutMiddleware 同构的**错误翻译器**：模型账号额度耗尽（HTTP 402/403、"Free quota exhausted"/"insufficient_quota"/"Insufficient Balance"）→ 返回友好中文 AIMessage 而非抛异常（前端 SDK 不渲染 stream.error）。

**识别策略防误伤**：强关键词（insufficient_quota/quota exhausted/额度/余额不足/欠费等）单独命中即判定；弱关键词（quota/balance/余额）必须同时出现 402/403/429 状态码（文本中或 `status_code` 属性）——避免把含 "balance" 的 SQL 业务报错误判为额度问题。401 鉴权错误等原样上抛。`QuotaExhaustedError` 类供非 agent 场景（auto_title 等自带 try/except 的调用点）手动翻译。**只翻译文案，不读/回退 .env 模型配置**（P1-9 配置权威性约束）。

---

## 15. `sql_approval.py` — SqlReadOnlyMiddleware（435 行）

三合一职责：

**① SQL 只读硬拦截**：run_sql 类工具执行前 `classify_sql` 确定性分类——先剥注释/字符串字面量，按顶层分号拆多语句取最严结论；首关键词在写/DDL 黑名单（INSERT/UPDATE/DROP/SET/USE/COPY...）→ 直接拒绝不执行不审批；`WITH` 开头再查 CTE 后是否跟 INSERT/UPDATE（WITH...INSERT 形态）；无法识别的语句一律按写处理（保守）。只读 SELECT 无 WHERE/LIMIT/聚合/GROUP BY 判 `full_dump` 但**仍放行**（仅分类信息）。历史：曾用 HumanInTheLoopMiddleware 人工审批，2026-08-28 产品要求升级为硬拦截，前端"SQL 审批"开关对写/DDL 不再生效。

**② Wren run_sql 双重 LIMIT 归一**（`normalize_semantic_limit`）：Wren 服务端无条件追加 `LIMIT {limit+1}` 探测截断，SQL 自带尾部 LIMIT 会变成双 LIMIT → MySQL 1064。在工具边界剥掉**最外层尾部**整数 LIMIT 并折算进 limit 参数（取 min，保 top-N 语义；offset 形态无法表达则跳过；先循环剥尾部分号/注释再匹配）。仅作用于 wrenai_*_run_sql，dbmcp 通道自身幂等无需处理。

**③ Cube limit/offset 剥离 + 平台侧截窗**：`query_cube` 的 limit/offset 会被编译进 Cube SQL，连接器又追加 LIMIT → 必语法错（生产 thread 01a0a850 实测）。调用边界就地剥离参数，结果返回后 `_apply_cube_window` 在结果集上按 `(offset, limit)` 切片补齐语义（只在 `{columns,rows,...}` 标准形态动手；窗口覆盖全部行则零改动不加噪音；fail-open——宁可不截也不篡改数据），并附注记说明是平台截的。

`build_sql_approval_middleware` 工厂：无 run_sql/query_cube 工具时返回 None 跳过挂载。

---

## 16. `thinking_toggle.py` — ThinkingToggleMiddleware

**功能一：思考开关**。前端 `configurable.enable_thinking` → wrap_model_call 时 `create_model(enable_thinking=...)` 重建模型实例并 `request.override(model=...)`。时序依据：langchain 在替换**之后**才执行 bind_tools，工具绑定不受影响。configurable 缺失 → 不替换走默认。

**功能二：模型路由（P1-9）**。`llm_route`/`llm_model` 指向前端 CRUD 的 model_config.json 中的 provider/模型，每次调用按 route 重建 → **前端切模型免重启后端**。

**进程级缓存**：`ChatDeepSeek` 构造实测 ~8.2s，同 (enable, route, model_name) 三元组复用实例；缓存以 model_config.json 的 **mtime 自动失效**（配置变更即清）；上限 8 条 FIFO 淘汰。

**P1-10 auto-continue 模型继承**：同 thread 后续续跑 run 不携带配置（前端只发系统通知消息）→ 查 `_last_thread_key`（thread_id → 最近显式配置的 cache_key，上限 200）复用上次的模型实例，避免续跑回退到可能已欠费的模块级默认模型，保证整会话同模型。

---

## 17. `token_meter.py` — TokenMeterMiddleware

对标 deepseek-harness session-stats projection：`wrap_model_call` 记录每次 LLM 调用的 wall-clock 耗时与 `usage_metadata`（input/output/cache_read/reasoning tokens），经 **`ExtendedModelResponse(command=Command(update={"token_stats": ...}))`** 写入 state。

- 累积由 state reducer `_accumulate_token_stats` 完成（数值字段累加、step_count 递增、steps 只留最近 50 条防 state 膨胀）。
- `_current_round`：数 state 里 human 消息数得当前轮号（1-based），供前端按轮聚合；取不到返回 0。
- usage 提取不到（如流式无 usage）→ 原样返回不写 state。
- **与 TraceRecorder 的分工**：token 计量只在此写 state，TraceRecorder 只记事件不写 token_stats——注释明确实测过双写导致统计翻倍。

---

## 18. `tool_filter.py` — ToolFilterMiddleware

**路线 A**（多 MCP server 常驻 + 按库过滤）：所有 wrenai server 全部加载，每次模型调用前按 `configurable.db_name` 用 `request.override(tools=...)` 只暴露当前库工具。

**过滤规则**：`wrenai_<当前库>_*` 保留、其他库 wrenai 过滤、非 wrenai/dbmcp 工具（图表等）保留。**语义层独占**（2026-09-09）：当前库**实际存在** wrenai 工具时直接不绑 `dbmcp_*`——模型看不见就不会试（此前靠 QueryGate 事后拦，每次白费一轮），与 dynamic_prompt 的"已建模只讲 wrenai"二分支对齐。

**关键防误伤**：判定用**实际存在的工具名**（`name.startswith(prefix)`）而非 `is_modeled()` 配置——2026-09-08 预检事故证明 wrenai server 加载失败时配置仍显示已建模，据此移除 dbmcp 会让模型一个查询工具都没有（必须 fail-open 保留）。QueryGate 硬闸保留为第二道防线（存量历史调用/幻觉工具名），"看不见 + 拦得住"是纵深。

前缀推导统一走 `semantic_db.wrenai_server_name`（唯一净化源）——避免中文库名时 `\W+` 不折叠 CJK 导致前缀匹配不上。`NL2SQL_SEMANTIC_EXCLUSIVE_TOOLS` 环境变量可秒回退旧行为。

---

## 19. `trace_recorder.py` — TraceRecorderMiddleware

**本地自包含追踪**：事件写 EventStore（SQLite），生产无 LangSmith/Langfuse 仍可回溯。挂载在中间件链**最外层**。

- `wrap_model_call`：LLM_CALL_START（模型名/消息数）→ 执行 → LLM_CALL_END（耗时/usage），异常记 ERROR 事件后上抛。
- `wrap_tool_call`：TOOL_CALL_START（参数摘要 ≤200 字符）/ END（结果摘要）；**检测 `start_async_task`**：从返回的 `Command.update.async_tasks` 提取子 thread_id，记 SUBAGENT_SPAWN 事件并 `upsert_lineage` 建立父子线程谱系。
- thread_id 策略：LLM 调用时缓存 `_cached_thread_id`，工具调用复用它（wrap_tool_call 拿不到 model request）；parent_thread_id 读 `configurable.trace_parent_thread_id`（deepagents patch 注入）。
- **不写 state.token_stats**——避免与 TokenMeter 双写导致 reducer 累加重复（实测翻倍）。

---

## 20. `vfs_path_resolver.py` — VfsPathResolverMiddleware

**解决的问题**：agent 全程在 VFS 命名空间工作（`/workspace/`、`/shared/memory/`、`/shared/skills/`），模型在聊天里回显"结果在 /workspace/report/xxx"，对用户既不知道是哪个工作区也不可打开。

**机制**：模型响应后处理（wrap_model_call 的 handler 返回之后），把 **AIMessage 文本**中的 VFS 路径按 `_VFS_PREFIXES` 映射改写为真实磁盘路径（统一正斜杠，Windows 可直接导航）。

**安全边界**：
- 只改 AIMessage 文本，**绝不碰 ToolMessage 和 tool_call 参数**——write_file 的 path 必须保持 VFS 形态（backend `_resolve_path` 按 `/` 前缀解析）。
- 正则负向前瞻 `(?<![A-Za-z0-9_:/\\])` 排除 `D:/workspace/...` 这类已是真实路径的片段，避免二次改写；路径字符排除空白与中英文标点（路径后常跟"（可悬停查看）"等注解）。
- fail-open + 幂等（改写后不再匹配 VFS 前缀）。

---

## 21. `write_todos.py` — WriteTodosProtocolMiddleware

**背景**：deepagents 基础栈 TodoListMiddleware 默认指引写着"simple 请求不要用 write_todos"，此前为进度条强制所有查询执行；但简单查询（策略 B）为此多付 1 次初始化 + 1-2 次更新的 LLM 轮次延迟，且前端已有 `deriveStepsFromSubMessages` 从工具序列推导步骤兜底。

**机制**：放在子智能体 middleware 列表**末尾**，把 `WRITE_TODOS_PROTOCOL` 追加到 system prompt（与默认指引兼容共存而非覆盖）：策略 A/C（7-8 步流水线）必须维护 todos；策略 B（≤3 步）可跳过。协议还规定了**关键节奏**："中途更新是免费的"——write_todos 必须与完成该步的实质工具调用放同一条 assistant 消息并行发出（唯一禁止是同一条消息 ≥2 个 write_todos），并给出与 progress_boundary 里程碑一致的更新时机（首个 schema 检索工具 / 首个执行工具发出时怎么勾）。

注意 `awrap_model_call` 必须 await handler——不 await 会把 coroutine 对象外泄，下游 `_build_commands` 报 `'coroutine' object has no attribute 'result'`（2026-08-14 实证）。仅用于 nl2sql 子 agent；主 agent 的 todos 稳定不需要本协议。

---

## 附：目录级共性模式

1. **配置读取统一走 `langgraph.config.get_config()`**：多处实证 `request.runtime.config` 恒为空 dict，这是本目录最重要的框架坑。
2. **deny 消息三件套**：`status="error"` + `Error:` 前缀 + 中文可执行指引——让模型自我修正不死循环，且被评分层正确识别为失败。
3. **fail-open 哲学**：观测/瘦身类异常绝不影响主流程；拦截类（execute/写 SQL）则宁可多拦。
4. **进程级 dict + 锁 + 容量上限清空的轻量缓存**：progress_boundary `_BUCKETS`、query_gate `_REMINDED`、thinking_toggle 双缓存、langfuse_span `_THREAD_ACTIVE_SKILL` 皆此模式（防内存泄漏）。
5. **阈值同源**：`LARGE_RESULT_TRUNCATE_CHARS` 被 message_slimmer / query_result_offload / langfuse_span 三处共读，注释明确记录过 16000 vs 8000 漂移导致 vfs 指针文件名造假的历史。
6. **目录口径同源**：`nl2sql_process_data/{thread}/` 落盘（langfuse_span、query_result_offload）与线程护栏（filesystem_thread_guard）共用 `_thread_id` 解析链。
