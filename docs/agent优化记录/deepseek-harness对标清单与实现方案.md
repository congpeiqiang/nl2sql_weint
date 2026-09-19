# DeepSeek Harness 对标清单与实现方案（最终版）

> 2026-08-15 定稿 · 2026-08-16 更新：**P1 九项已全部实施并随 2026 后端重启上线**（实施进度见第十节）
> 对标对象：`D:/code_work_space/llm/deepseek-harness`（DeepSeek AI 开源 agent harness，前端 http://127.0.0.1:3080/）
> 本版整合了：初版 17 项清单 + 模型配置对标附录 + 源码级交叉核验修正。初版备份于同目录 `.bak-20260815`，改动缘由见文末「修订说明」。

## 一、对标对象简介

DeepSeek Harness（`dsh`）是 DeepSeek AI 的开源 agent 运行时，核心特征：

- **一切皆插件**：基于 Cordis，每个能力是「capability seam」三件套（Service Definition 抽象接口 + Provider 可替换实现 + Consumer 工具）。
- **包结构**：两级嵌套 `packages/<类别>/<叶子>`，叶子包名均 `@deepseek-ai/dsh-*`（本文 `dsh-xxx` 均指叶子包）。
- **事件溯源会话**：会话日志是追加式 `SessionEvent`，是交互历史唯一事实源；LLM 消息历史由日志派生（`deriveEventMessage`/`foldSurface`），永不单独存储。
- **工具执行管线**：所有工具调用过三层 waterfall（`tools/pre-execute → tools/execute → tools/post-execute`）+ 单调 guards（`ToolGuard`）+ 审批接缝 + 结果归一化。
- **技术栈**：TypeScript monorepo（pnpm）。UI 是插件/插槽架构，单页应用 + 弹窗，无路由库。

**重要前提：技术栈不兼容（TS 插件系统 vs Python LangGraph），对标 = 借鉴设计模式与交互范式，不搬代码。** 每条对标要么落地为 LangGraph 中间件/工具，要么落地为前端组件。

## 二、最终对标清单总表（按优先级）

| # | 能力 | 对标来源 | nl2sql 现状 | 可行性 | 优先级 |
|---|------|---------|------------|--------|--------|
| P1-1 | 消息反馈：点赞/点踩 + 备注 | `dsh-message-feedback` + `MessageFeedbackActions` | 无任何反馈通道 | 可行 | **高** |
| P1-2 | 工具调用 IN/OUT 卡片 + SQL 结果分类渲染 | `ui-tool/ToolRow` + `ui-primitives/*Block` | 有 `ToolCallBox`，SQL 结果纯文本 | 可行（纯前端） | **高** |
| P1-3 | SQL 执行审批闸门 | `interaction/user-approval` + `ApprovalPanel` | 后端零 interrupt，SQL 任意执行 | 可行（确定性最高） | **高** |
| P1-4 | 会话重命名 + LLM 自动标题 | `ui-workspace` 重命名 + `dsh-session-title` | 无标题/重命名 | 可行 | **高** |
| P1-5 | 会话分叉（fork） | `ui-workspace` fork | 无 | 可行（SDK `copy` + 回退） | **高** |
| P1-6 | 会话全文搜索 | `dsh-session-query` + SQLite FTS5 | 无；SDK 不支持内容搜索 | **需新建检索层** | **高** |
| P1-7 | 统一后台任务表（jobs 升级） | `dsh-jobs` | 有 5 工具（deepagents），取消善后有缺陷 | 可行（修善后） | **高** |
| P1-8 | 模型配置管理（provider + 密钥 + 自定义 provider） | `ui-settings-models` + `llm-pi-ai` + `credentials` + `settings` | 纯 `.env`，无 UI | 可行 | **高** |
| P1-9 | composer 内模型选择 | `ui-model-selection/ModelSelect` | 无（仅 DB 下拉） | 可行（依赖 P1-8） | **高**（可选） |
| P2-1 | **token 计量接缝（前置项）** | `dsh-token-meter` | 全项目无 token 估算 | 可行 | 中（前置） |
| P2-2 | 轨迹台账 | `ui-trajectory/TrajectoryTable` | 有进度条，无诊断轨迹 | 可行 | 中 |
| P2-3 | 子 agent 树形目录 + 状态点 + token/时长 | `ui-subagent/SubagentCatalogAction` + `ui-jobs` | 有并发进度卡片 | 可行（依赖 P2-1） | 中 |
| P2-4 | 上下文压缩 + 大结果溢出（spill） | `dsh-compaction` + `dsh-spill` + `output-retention` | 有 `MessageSlimmerMiddleware`（仅截断） | 可行 | 中 |
| P2-5 | 长期目标 + 轮次上限 | `dsh-goal` | 无 | 可行 | 中 |
| P2-6 | 计划审批卡（+ plan-mode 后端） | `PlanReviewPanel` + `dsh-plan-mode` | 无 | 可行（复用 P1-3 范式） | 中 |
| P2-7 | 上下文占用环 | `ContextMeter` | 无 | 可行（依赖 P2-1） | 中 |
| P2-8 | 思考折叠增强（流式跟随） | `ReasoningRow` | 有 `ThinkingBlock` | 可行 | 低 |
| P2-9 | 消息操作行：复制/分叉/性能元数据 | `MessageIconActions` | 无 | 可行 | 低 |
| P3-1 | 密钥引用托管（配置只存 ref 热更新） | `dsh-credentials` + `dsh-settings` | DB 密钥已 AES-GCM，LLM key 在 `.env` | 需架构调整 | 低 |
| P3-2 | 会话事件溯源 | `dsh-session` | LangGraph checkpoint | 与现有架构冲突 | 低 |
| P3-3 | workflow 编排（parallel/pipeline） | `dsh-workflow` + `workflow-worker-thread` | 单子 agent 委派 | 远期 | 低 |
| P3-4 | 定时提醒（**非 cron**） | `dsh-schedule`（at/after/every） | 无 | 需自建 cron | 低 |

**已具备、无需对标**：todo 条（`write_todos` 整表替换，`subagent_steps_map` 已有进度回写）；流式输出；思考开关；DB 配置 UI（`DbConfigDialog`）；PDF/图片上传。

---

## 三、P1：高价值，建议优先

### P1-1 消息反馈：点赞/点踩 + 备注（后端 + 前端）

**对标来源**：`dsh-message-feedback`——逐条 assistant 消息 `positive/negative` + 可选 note；`ifVersion` 乐观并发 CAS；`maxNoteBytes` 上限。UI `MessageFeedbackActions`（插在消息操作行，撤销/替换/版本冲突处理）。

**nl2sql 现状**：全前端无任何反馈组件（grep 确认）。

**实现方案**：
1. 后端：新 API `/threads/{tid}/messages/{mid}/feedback`（PUT/DELETE），存储带 `version` 的反馈记录（先内存/JSON 落盘，后续 SQLite）。主 agent 生成 SQL/结论的那条消息绑定 feedback。
2. 前端：`ChatMessage` 操作行加赞/踩 + 备注编辑（弹层输入），乐观更新 + 冲突重试。
3. 用途：反馈数据回流做 NL2SQL 质量评测（现有 imdb/chinook 验证集可接），应用闭环见下。

**反馈应用闭环（含案例）**：

反馈本身不自动改善系统——它是自动积累的「错题本」，通过三条路径被消费：

> **案例**：用户问「2020 年之后有多少部电影？」，系统执行 `SELECT COUNT(*) FROM titles WHERE start_year > 2020` 回答 234 部；用户点👎备注「2020 年应该包含在内，而且只统计电影，别算电视剧」。该条以「问题 + 错的 SQL + 备注」落盘为 bad case 记录。

| 路径 | 消费者 | 动作 | 改善效果 |
|------|--------|------|---------|
| ① 开发者修系统 | 开发者 | 定期导出👎记录并按失败模式聚类：「之后」年份边界歧义 → 改澄清/生成 prompt；titles 表影视混存 → 补 knowledge 加 `title_type='movie'` 过滤 | 真正的改善来源：反馈指明该修管线的哪一环 |
| ② 回归评测 | 评测集 | 该「问题 + 正确答案」入回归评测集（与 imdb/chinook 验证集合并），每次改 prompt/换模型前后各跑一遍 | 防回退，优化从凭感觉变成数据可验证 |
| ③ 即时重试（可选，加开关） | 当轮会话 | 用户继续对话时把备注带给主 agent（「上次回答被否定，用户指出…」）重跑管线 | 单次失败变多轮修正 |

失败模式 → 管线阶段映射：选错表/列 → Schema linking（改 prompt/补 knowledge）；口径误解 → 澄清阶段（往「问题清晰度判定与澄清追问方案」补 few-shot）；SQL 语法/方言错 → 生成+纠错链路；图表不对 → 可视化段。👍样本同样有用，作为该问题类型的正例（后期可建相似例 few-shot 库，依赖 P1-6 检索层）。

注意：负反馈天然稀疏，冷启动期需人工标注/合成 bad case 补足样本。

**工作量**：低-中。独立、低风险，先立反馈闭环。

### P1-2 工具调用 IN/OUT 卡片 + SQL 结果分类渲染（纯前端）

**对标来源**：`ui-tool/ToolRow`（单行摘要 + 展开卡内 IN/OUT 标注分段 + 错误行首行红字）+ `ui-primitives/*Block`（`TerminalBlock`/`DiffBlock`/`JsonTree`/`CodeBlock`，另有 `ReadBlock`/`SearchBlock`/`WebBlock`，按工具意图分类渲染）。

**nl2sql 现状**：`ToolCallBox` 有状态图标 + 参数树 + 结果预览（交互式图表走 echarts iframe，其余含 `run_sql` 一律 `<pre>` 纯文本/JSON），无表格/错误/耗时分类。

**实现方案**：
1. 按工具名/结果结构分派渲染器：`run_sql`/`dbmcp_run_sql` → 数据表格卡（列头 + 行数 + 取数耗时）；`dry_run` 失败 → 错误卡（语法错误高亮）；图表工具 → 已有 iframe。
2. 新增 `SqlResultBlock` 组件（markdown 表格 or 虚拟化表格），复用 `MarkdownContent` 已有表格样式。
3. 错误态：错误图标 + 错误分类（语法/列不存在/权限）→ 与子 agent 纠错链路呼应。

**工作量**：低-中。纯前端，立竿见影。

### P1-3 SQL 执行审批闸门（后端 + 前端）

**对标来源**：`interaction/user-approval`——`ApprovalService.request()`，`ApprovalPolicy='ask'|'never'` 持久化为会话事件；UI `ApprovalPanel`（位于 `ui-conversation`，需确认时接管 composer，展示理由 + 命令 + Allow once/Reject）。

**nl2sql 现状**：后端**零 interrupt**（grep 确认，`FILE_PERMISSIONS` 无 interrupt 模式、`HumanInTheLoopMiddleware` 未挂载），`run_sql`（WrenAI/dbmcp MCP）无审批直接执行。前端 `ToolApprovalInterrupt`（approve/reject/edit）齐全但无触发路径；**注意 `InterruptActions.tsx` 是全仓库无人引用的死代码，接线用 `ToolApprovalInterrupt`**。

**可行性实测**：确定性最高——langgraph 1.2.9 原生 `interrupt` 可用、deepagents `HumanInTheLoopMiddleware`/`interrupt_on` 可用、前端组件就绪，只差把三者接起来。

**实现方案**：
1. 后端：在 nl2sql 子 agent 的 `run_sql` 工具前挂审批判断——**只对非 SELECT（写/DDL）或超阈值查询（全表扫描、跨库）触发 `interrupt`**，返回 `approve`/`reject` 后继续。
2. 前端：把 SQL 审批做成 **pre-execute 确认卡**（SQL 预览 + 影响估算 + 确认/取消），复用 `ToolApprovalInterrupt` 驱动 LangGraph interrupt 恢复。
3. 配置：`ApprovalPolicy` 持久化（每用户/每库 ask/never），写库与只读默认不同策略；**默认放行只读，避免打扰正常查询，需回归「清晰查询零打扰」基线**。

**工作量**：中。行为有风险，建议放在前端纯展示项之后、充分回归后再上。

### P1-4 会话重命名 + LLM 自动标题（前端 + 轻后端）

**对标来源**：`ui-workspace/WorkspaceBrowser` 重命名（工作区 + 会话两个 Modal）；`dsh-session-title`(-llm) 从首轮问题自动生成会话标题。

**nl2sql 现状**：`ThreadList` 有状态过滤 + 时间分组 + 批量删除，但无标题/重命名（thread 只有默认 id）。

**实现方案**：
1. 后端 thread metadata 加 `title` 字段（SDK `threads.update(threadId, {metadata})` 支持）。
2. 自动标题：首轮用户消息后异步调一次 LLM 生成短标题写回 metadata（仿 `dsh-session-title-llm`），前端列表展示。
3. 前端：`ThreadList` 行内重命名编辑。

**工作量**：低-中。是分叉/搜索/分组的前置，先做。

### P1-5 会话分叉（fork）（后端 + 前端）

**对标来源**：`ui-workspace` fork（`forkSession`/行菜单 onFork）。

**nl2sql 现状**：无。

**可行性实测（修正）**：langgraph-sdk v1.0.3 有 `threads.copy(threadId)`（POST /threads/{id}/copy，后端路由真实存在；nl2sql 的 AsyncSqliteSaver 未实现 `acopy_thread`，走通用 checkpoint 重放 fallback）。**但 copy 是整线程复制，「从某消息分叉」需 copy 后 `getHistory` 定位 checkpoint 再 `updateState` 回退**，不是现成 fork 调用。

**实现方案**：
1. 后端 `/threads/{tid}/fork?at=<checkpoint>`：`copy` + `getHistory` 定位 + `updateState` 回退到指定消息。
2. 前端：`ThreadList` 行菜单「从此处分叉」+ `ChatMessage` 操作行分叉按钮（与 P2-9 共用）。
3. 对 NL2SQL「改口径/改条件重新验证」迭代场景最实用。

**工作量**：中。**注意**：长线程走重放 fallback 可能偏慢，需实测性能。

### P1-6 会话全文搜索（需新建检索层）

**对标来源**：`dsh-session-query` + SQLite **FTS5 全文搜索** + 模型可用的历史检索工具。

**nl2sql 现状**：无搜索。

**可行性实测（修正）**：langgraph-sdk `threads.search()` 过滤器仅 `metadata`（精确 kv）/`ids`/`status`/`values`（state 顶层键，非全文），**不支持按消息内容全文检索**。原方案「后端 /threads/search 按 content 过滤」**不成立**。

**实现方案**（二选一，推荐前者）：
1. **自建 FTS 检索层**（仿 `dsh-session-query`）：新增 SQLite FTS5 表，会话消息写入时同步建索引；新 API `/threads/fts?q=` 返回命中 thread + 片段。副产品：可做「历史 SQL 会话复用」。
2. 轻量过渡：前端拉取 thread 列表后本地扫描（仅适合会话量小）。

**工作量**：中-高（新建检索层）。**可独立立项、后置**，不阻塞 P1 其他项。

### P1-7 统一后台任务表（jobs 升级，重点修正为「修取消善后」）

**对标来源**：`dsh-jobs`——`JobRegistry` `start/get/list/read/kill/wait`，`JobStatus=running|stopping|completed|killed|failed`，`job_output` 增量读取，owner session 隔离，`maxConcurrentJobsPerOwner=10`。

**nl2sql 现状（修正）**：5 个异步工具来自 **deepagents 0.6.12**（`async_subagents.py`），nl2sql 用 3 个 monkey-patch 增强。**关键修正**：
- `cancel_async_task` **已发真实 kill**（`client.runs.cancel` → 服务端 cancel_run），不是 no-op。
- 真实缺陷在**善后**：① sync 守护线程（`sync_subagent_todos.py`）不认 `cancelled` 终态，300s 后把状态覆写成 `error`（cancelled 被盖掉）；② cancel 仅 async 路径可用；③ deepagents 终态集本含 `cancelled/timeout/interrupted`，并非"只有 running/success/error"。

**实现方案**（工作量比原估**小**）：
1. 修 `sync_subagent_todos.py`：识别 `cancelled/timeout/interrupted` 终态，不再覆写为 `error`。
2. 给 `check_async_task` 加增量（`since`/cursor 参数只回新 chunk）。
3. 加超时上限（子 agent 最长 10min，对齐 guards `timeout-policy`）+ 失败分类。
4. 可选：任务 owner 隔离、统一"job"抽象（对齐 `dsh-jobs`）。

**工作量**：中（在现有 monkey-patch 上扩展，需回归并发多查询）。

### P1-8 模型配置管理（后端 API + 前端 UI）

**对标来源**：`ui-settings-models` + `llm-pi-ai` + `credentials` + `settings`（源码声称已全部核实）。

**deepseek-harness 的模型配置 UI 分 6 层**（界面层对标细则）：

| 界面层 | dsh 组件 | 干什么 | nl2sql 落点 |
|---|---|---|---|
| 设置壳 | `SettingsRoot` | 居中 modal + 左侧分区导航 rail（模型/预设/插件/通用），Esc/遮罩关闭 | 统一现有分散弹窗 |
| Provider 列表 | `ModelsSection` | provider 行 + **密钥状态点（configured/missing）** + 编辑/删除二次确认 | 新建 |
| Provider 编辑卡 | `ProviderEditor` | **只写 API key 输入框**（不回显值，只显示已配置状态）+ 折叠自定义设置（baseURL/模型列表/协议） | 复用 `DbConfigDialog` 密钥范式 |
| 模型列表编辑 | `ModelListEditor`/`DeepSeekModelsEditor` | 模型行含 capacity（contextWindow/maxTokens）+ **「获取模型」discoverModels 探活导入** | 新建 |
| 自定义 Provider 卡 | `CustomProviderCard` | 声明 OpenAI-compatible 网关：route id 正则校验 + baseURL + 协议 + key + models | 新建 |
| 首次引导 | `DeepSeekOnboardingDialog` | 首次打开引导配置第一个 API key，版本号持久化已确认状态 | 可选加分项 |

**后端 seams**：`llm-pi-ai` 按 route 键控 provider profile（catalog route vs declared route，`apiKeyEnv` 密钥引用）；`credentials` 配置只存 env-var ref、`describe()` 不暴露值；`settings` 三层（schema-default→base→user）+ revision CAS + `describe({redactSecrets})`。

**nl2sql 现状**：`model.py` 纯读 `.env`（`_detect_provider` 判 deepseek/qwen/openai_compat）、模块级单例 `deepseek_model`；前端 `ConfigDialog` 仅部署 URL + 助手 ID（LangSmith 输入框被注释）。

**实现方案**：
1. 后端新增 `src/agent/settings/model_config_store.py`（仿 `db_config_store.py`）：JSON 落盘 provider 列表，key 值 AES-256-GCM 加密（复用 `DB_CONFIG_SECRET` 或新增 `LLM_CONFIG_SECRET`）。
2. 新增 REST API `/api/model-configs`（CRUD + 连接/discoverModels 探活），挂进 `src/api/custom_app.py`。
3. 改 `model.py`：`create_model()` 优先读配置 store 选 route，fallback `.env`；移除/降级模块级单例，改每次调用创建（`ThinkingToggleMiddleware` 已在做）+ 配置变更缓存失效。
4. 前端：新增 `ModelConfigDialog`——provider 行 + 密钥状态点 + 编辑卡 + 自定义 provider 卡 + 删除确认，复用 `DbConfigDialog` 密钥输入/状态点范式。

**工作量**：中。**注意**：切模型影响主 agent 与子 agent，需评估「流式/思考开关/子 agent 进度」回归。

### P1-9 composer 内模型选择（可选，依赖 P1-8）

**对标来源**：`ui-model-selection/ModelSelect`——输入框旁**两级下拉**（按 provider 分组的模型列表 + 每模型 reasoning effort），触发按钮同显「模型名 + effort」。

**实现方案**：前端 composer 加模型下拉（读 `/api/model-configs`，按 provider 分组），落点参照现有 `DatabaseSelector`（DB 下拉经 `configurable.db_name` 传入）；选中值走 `configurable.llm_route`，`model.py` 按 route 解析，后端免重启（每次调用重建）。

**工作量**：低-中。demo 高频需求。

---

## 四、P2：中价值（按需推进）

### P2-1 token 计量接缝（前置项，建议最先做）
**对标**：`dsh-token-meter`（重放感知，请求压力 + surface 占用，是 `ContextMeter` 数据源）。
**nl2sql 现状**：全项目无 token 估算（`MessageSlimmerMiddleware` 纯字符数 `len()`）。
**方案**：中间件层统计每次请求/surface 的 token（估算或 `usage_metadata`），写入 state，供 P2-2/P2-3/P2-7 复用。**是这三项的前置，先做。**

### P2-2 轨迹台账（Trajectory）
**对标**：`ui-trajectory/TrajectoryTable`（turn-aware 事件台账，每步 = 模型请求编号/provider/model/状态/用量 + 工具调用 + 子调用，`trajectory-search-index` 全文索引）。
**方案**：复用 `async_tasks`/`subagent_steps_map` + `MessageSlimmerMiddleware` 记录的 tool 结果，前端新增「轨迹视图」tab（模型请求/工具调用/耗时/token），先用现有 state 渲染。

### P2-3 子 agent 树形目录 + 状态点 + token/时长
**对标**：`ui-subagent/SubagentCatalogAction`（树形 popover、`StateDot`、token 汇总、活动时长、进入子会话）+ `ui-jobs/JobListAction`。
**方案**：现有并发进度卡片升级为树形（任务→阶段→步骤），加 token/时长列；token 由 P2-1 提供，写入 `async_tasks`。

### P2-4 上下文压缩 + 大结果溢出（spill）
**对标**：`dsh-compaction`（`compactIfNeeded`，`CompactionTrigger='pressure'|'context-overflow'`，surface 替换为单一 summary 节点；工具结果先剪枝——`pruneSession` 在同族包 `dsh-compaction-tool-result-pruner`）+ `dsh-spill`（`saveText` 落盘返回 locator，head/tail 预算切分）+ `dsh-output-retention`。
**nl2sql 现状**：`MessageSlimmerMiddleware` 只做 >16K 落盘截断 + md5 去重，无 LLM 摘要。
**方案**：阶段一 spill（大 SQL 结果/explain 超阈值写 `/workspace/nl2sql_process_data/{tid}/spill/{n}.txt`，消息只留 head/tail + 路径，`read_file` 可取回，纯中间件）；阶段二 compaction（`UpdateState` 摘要替换旧消息，保留最近 N 轮，处理 checkpointer 兼容）。

### P2-5 长期目标 + 轮次上限
**对标**：`dsh-goal`（`GoalPhase=active|paused|blocked|complete` + `maxGoalRounds` + `block(code)` 分类阻塞）。
**方案**：主 agent 加 `goal` 状态字段（目标文本 + round 计数），多轮改需求 round 超阈值 → 提示收敛或 block；前端 GoalBar（composer 上方常驻目标 + 暂停/恢复/编辑）。

### P2-6 计划审批卡（+ plan-mode 后端）
**对标**：`PlanReviewPanel`（`ui-user-questions`，计划 markdown + Approve/Decline/Discuss）+ `dsh-plan-mode`（每 agent 计划模式 + `exit_plan_mode` 送审，前端面板的后端接缝）。
**方案**：与 P1-3 共用「composer 接管」范式——子 agent 计划生成后、`run_sql` 前把 Query Plan 呈现给用户确认，复用 `ToolApprovalInterrupt` 骨架。

### P2-7 上下文占用环（依赖 P2-1）
**对标**：`ContextMeter`（send 按钮旁圆环占用 % + system/tools/messages 三段分解）。
**方案**：token 由 P2-1 提供，前端加占用环。**修正**：原「MessageSlimmerMiddleware 已在算」不成立，需先有 P2-1。

### P2-8 思考折叠增强
**对标**：`ReasoningRow`（流式时显示最新一行并右对齐跟随滚动）。
**方案**：现有 `ThinkingBlock` 加「流式中尾部跟随」（`useEffect` scrollIntoView 末行）。

### P2-9 消息操作行
**对标**：`MessageIconActions`（复制/branch 分叉/时间 clock + `Ran for Xs`·`TTFT`·`tok/s`）。
**方案**：`ChatMessage` 加复制 + 分叉按钮（复用 P1-5 fork 端点）+ 耗时/token 元数据。

---

## 五、P3：远期 / 需架构调整

| 能力 | 对标 | 说明 |
|------|------|------|
| P3-1 密钥引用托管 | `dsh-credentials` + `dsh-settings` | 配置只存 env ref、热更新、describe 不暴露值；`settings` 支持 YAML 文件后端（热更新/跨进程锁/注释保留 diff）。nl2sql 已有 AES-GCM `db_config_store`，可加「每库每用户密钥」+ UI 状态点 |
| P3-2 会话事件溯源 | `dsh-session` | 历史=日志派生、可审计/重放。与 LangGraph checkpoint 架构冲突，先评估能否在 custom_app 层做镜像日志 |
| P3-3 workflow 编排 | `dsh-workflow` + `workflow-worker-thread` | `parallel/pipeline` 组合子（实现在 worker-thread 引擎）。nl2sql 当前单子 agent，多子 agent（多库并行）时引入 |
| P3-4 定时提醒 | `dsh-schedule` | **修正：dsh-schedule 不是 cron**，是 at/after/every 一次性/固定频率提醒。nl2sql 若要真「定时周报/重新校验」需自建 cron（如 APScheduler），并确认 server 后台任务持久性 |

## 六、不建议对标的项

- **Sandbox/终端/code-runtime/shell/e2b/lsp/subprocess 沙箱类**：nl2sql 是只读查询场景（WrenAI 语义层隔离），无代码执行需求；已有 `FILE_PERMISSIONS`。
- **多 provider 子 agent（acp/codex/claude-code/subagent 全家桶）**：nl2sql 是单模型产品线。
- **身份/匿名遥测（identity）**：除非接多用户/审计平台。
- **事件溯源完整架构（P3-2 之前）**：LangGraph 已是事件驱动，重造会话事件层收益低、风险高。

## 七、实施顺序建议

**P1 推荐顺序**（依赖少→依赖多，行为风险后置）：

1. P1-1 消息反馈（独立、低风险）
2. P1-2 工具 IN/OUT 卡片（纯前端，立竿见影）
3. P1-4 会话重命名 + 自动标题（易，metadata）
4. P1-8 模型配置管理（后端 API + 前端 UI，独立）
5. P1-9 composer 模型选择（依赖 P1-8）
6. P1-7 统一任务表（修取消善后，需并发回归）
7. P1-5 会话分叉（copy + 回退，需性能实测）
8. P1-3 SQL 审批闸门（确定性最高但行为风险，回归「清晰查询零打扰」后上）
9. P1-6 会话全文搜索 FTS5（难，需新建检索层，可独立立项后置）

**P2 打包建议**：先 P2-1 token 计量（前置）→ P2-2/P2-3/P2-7 共用 token 数据一次补齐；P2-4 spill 可先做（纯中间件）；P2-6 与 P1-3 共用 composer 接管范式；P2-5/P2-8/P2-9 按需。

**每项实施前**：确认是否需重启 2026（后端改动）及前后端发布节奏；涉及并发状态机（P1-7）必须先读 `sync_subagent_todos.py` 与并发多查询方案文档。

## 八、风险与注意

- **技术栈隔离**：所有对标都是「借鉴设计」，无代码复用；每项按 LangGraph/deepagents 现有中间件体系重新落地。
- **后端改动需重启**：P1-3/P1-7/P1-8/P2-1/P2-4 涉及 graph/中间件，按既定流程改完重启 2026。
- **前端 types.ts 被 DLP 加密**：`src/app/types/types.ts` 是二进制（Esafenet 头）但**确被实际 import**（ToolCallBox/ToolApprovalInterrupt 等 `import type` 引用），改类型时从 git 恢复或从 `useChat.ts` 用法反推，注意编译风险。
- **审批（P1-3）行为风险**：默认放行只读，仅拦截写/DDL/超阈值，避免影响正常查询。
- **分叉（P1-5）性能**：AsyncSqliteSaver 走通用 checkpoint 重放，长线程需实测。
- **死代码**：前端 `InterruptActions.tsx` 无人引用，勿误用；用 `ToolApprovalInterrupt`。

## 九、修订说明（相对初版 `.bak-20260815`）

本版基于源码级交叉核验（deepseek-harness 20 项功能声称 18 确认 + nl2sql 15 项现状声称 + langgraph-sdk v1.0.3 实测）做了如下修订：

**可行性修正（影响方案）**：
- P1-6 会话全文搜索：SDK `threads.search` 不支持内容全文检索 → 改为自建 FTS5 检索层（仿 `dsh-session-query`）。
- P1-5 分叉：SDK 有 `threads.copy` 但为整线程复制 → 方案改为 copy + `getHistory` + `updateState` 回退。
- P1-7 任务表：`cancel_async_task` 已发真实 kill，重点是修「cancelled 被覆写为 error」的善后 → 工作量下调。
- P2-7 占用环：`MessageSlimmerMiddleware` 无 token 计量 → 新增前置项 P2-1 token 计量。

**事实修正**：
- P3-4 `dsh-schedule` 非 cron（at/after/every 提醒）。
- `ThreadList` 为手动「加载更多」按钮，非无限滚动。
- `ConfigDialog` LangSmith key 输入框被注释，实际仅部署 URL + 助手 ID。

**结构调整**：
- 原「会话列表增强」拆为 P1-4（重命名/自动标题）/ P1-5（分叉）/ P1-6（全文搜索）三档。
- 模型配置从附录升为正式 P1-8/P1-9，补入 6 层界面映射表。
- 新增前置项 P2-1 token 计量；补入遗漏对标 `dsh-session-title`（并入 P1-4）、`dsh-plan-mode`（并入 P2-6）、`dsh-output-retention`（并入 P2-4）、`dsh-token-meter`（P2-1）。

---

## 十、P1 实施进度记录（2026-08-16）

9 项 P1 已全部实现，后端改动随 2026 重启上线、前端随 next dev 热更。下表为「状态 → 落点 → 验证」对照，方案细节见第三/七节。

| # | 状态 | 后端落点 | 前端落点 | 验证 |
|---|------|---------|---------|------|
| P1-1 | ✅ 上线 | `src/api/message_feedback.py`：`PUT/DELETE /api/threads/{thread_id}/feedback` + `GET /api/feedback/export` | `src/lib/feedback.ts` + `ChatMessage` 赞/踩/备注操作行 | tsc 基线一致 |
| P1-2 | ✅ 上线 | —（纯前端） | `SqlResultBlock.tsx`：`run_sql`/`dbmcp_run_sql` → 数据表格卡，dry_run 失败 → 错误卡 | tsc 基线一致 |
| P1-3 | ✅ 上线 | 中间件 `src/agent/middlewares/sql_approval.py`（`classify_sql` 只读/写/DDL/全表拉取）；API `src/api/sql_approval.py`：`POST /api/threads/{thread_id}/sql-approval` | `src/lib/sqlApproval.ts` + `SqlApprovalCard.tsx` + 设置弹窗「SQL 写操作执行审批」开关（ask/never） | 分类 13/13（Chinook_Aliyun 真实 SQL）；运行时「清晰查询零打扰」回归待跑 |
| P1-4 | ✅ 上线 | `src/api/auto_title.py`：`POST /api/auto-title`（LLM 自动标题） | `src/lib/threadMeta.ts` + `ThreadList` 行内重命名 | 标题跨重启持久化实测通过 |
| P1-5 | ✅ 上线 | `src/api/thread_fork.py`：`POST /api/threads/{thread_id}/fork` | `src/lib/threadFork.ts` + `ThreadList` 分叉按钮 | — |
| P1-6 | ✅ 上线 | `src/api/thread_search.py`：`GET /api/threads/fts?q=`（FTS5 trigram + ≤2 字 LIKE 回退，索引落 `src/agent/workspace/checkpoint/fts.sqlite`） | `src/lib/threadSearch.ts` + `ThreadList` 搜索框 | 188 会话索引；「销售额」trigram /「客户」LIKE 双路径 live 验证 |
| P1-7 | ✅ 上线 | `src/agent/subagents/sync_subagent_todos.py`：识别 `cancelled/timeout/interrupted` 终态、`check_async_task` 增量、超时上限 | `TasksFilesSidebar.tsx` 统一任务表 | 并发回归待跑 |
| P1-8 | ✅ 上线 | `src/agent/settings/model_config_store.py` + `src/api/model_config.py`：`/api/model-configs`（CRUD + activate + test） | `src/lib/modelConfigs.ts` + `ModelConfigDialog` | — |
| P1-9 | ✅ 上线 | `model.py` 按 `configurable.llm_route` 解析（每次调用重建，免重启） | composer 模型下拉（`DatabaseSelector` 同范式） | — |

**待办回归（需真实子 agent run，走 LLM + Chinook_Aliyun）**：
- P1-3「清晰查询零打扰」：默认放行只读，写/DDL/全表拉取弹审批卡。
- P1-7 并发多查询：取消/超时后状态不被覆写为 error。

**关键实现备注**：
- P1-3 决策链路：子 run 的 `run_sql` 被 HITL interrupt → sync 环路中继到主线程 `async_tasks[task].awaiting_approval` → `POST /api/threads/{sub}/sql-approval` 恢复子 run；`InterruptActions.tsx` 是死代码，接线走 `ToolApprovalInterrupt`。
- P1-6 索引不手工反序列化 msgpack，复用 langgraph-api 自身 HTTP 接口（`POST /threads/search` 枚举 + `GET /threads/{tid}/state` 拉全量消息）；增量按 `updated_at` + 标题归一化，避免无标题会话（`metadata.title=None`）被误判变更导致每次全量重建（该 bug 已在 2026-08-16 重启时修复并回归）。
- P1-3/P1-6 前端均复用 `ToolApprovalInterrupt` / 现有 `Input` 组件，无新增第三方依赖。
