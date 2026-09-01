# NL2SQL 项目架构文档（面向新人）

> **最后更新**：2026-08-29
> **适用读者**：新入职后端开发 / 运维 / 接手本项目的工程师。
> **本文目标**：回答四件事——① 项目是什么；② 怎么最快跑起来；③ 代码怎么组织、一次查询怎么走；④ 日常开发在哪改什么。带「代码逻辑解析」，尽量给 `文件:函数/类` 定位。
>
> 配套文档：快速命令见 [docs/langfuse平台/项目脚本命令手册.md](docs/langfuse平台/项目脚本命令手册.md)；Langfuse 闭环操作见 [docs/langfuse平台/NL2SQL-Langfuse全流程实现手册.md](docs/langfuse平台/NL2SQL-Langfuse全流程实现手册.md)；trace 层级规范见 [docs/langfuse平台/Langfuse标准Trace层级方案.md](docs/langfuse平台/Langfuse标准Trace层级方案.md)。

---

## 0. 项目是什么

**NL2SQL 智能体**：用户用自然语言提问，系统自动完成「Schema 理解 → SQL 生成 → 执行 → 结果/报表/报告输出」，并对每一次查询做**全链路可观测（Langfuse）**、**自动评估（五维评分）**、**BadCase 采集与迭代闭环**。

一句话概括架构：

> **1 主 Agent（意图路由 + 结果汇总）＋ 1 子 Agent（nl2sql 数据执行）＋ 8 个 NL2SQL Skill（SQL-of-Thought 流水线）＋ WrenAI 语义层 / 直连双通道 ＋ Langfuse 全链路闭环。**

技术栈（[pyproject.toml](pyproject.toml)）：

| 层 | 技术 |
|---|---|
| LLM | `langchain-deepseek` / 自研 glm、kimi 封装（[llms/model.py](src/agent/llms/model.py)） |
| Agent 框架 | `deepagents ≥ 0.6.12`（`create_deep_agent` 构建 agent） |
| 编排运行时 | `langgraph-api ≥ 0.11.1`（uvicorn，:2026） |
| MCP | `langchain-mcp-adapters` + `fastmcp`；WrenAI 语义层 + 自建 db_mcp_server |
| 可观测 | `langfuse ≥ 4.14.4`（trace/scores/prompt/dataset）+ 本地事件日志 |
| 数据库 | MySQL / ClickHouse / PostgreSQL / SQLite（db_mcp_server 实际可路由 4 引擎） |
| 持久化 | AsyncPostgres / AsyncSqlite（checkpoint）、SQLite（feedback/event） |
| 前端 | `harness-deep-agents-ui`（独立仓库，Next.js） |

**一个查询的端到端旅程（30 秒版）**：

```
用户提问 → 前端 POST /threads/{id}/runs → LangGraph API → 主 Agent(chat_agent)
  → 判断是「数据查询」→ start_async_task 委派 nl2sql 子 Agent(独立 thread/run)
  → nl2sql 子 Agent 跑 SQL-of-Thought 流水线（skill 逐步执行）→ run_sql 查库
  → 主 Agent check_async_task 拿结果 →（可）generate_echarts 图表 / build_report 报告
  → 前端 SSE 流式收到最终答复；每条查询同时写入 Langfuse trace 用于评估/迭代
```

---

## 1. 快速上手（新人第一件事）

### 1.1 三分钟启动

```bash
# 0. 依赖（Python ≥ 3.13 + uv）
uv sync

# 1. 配置环境变量（.env 已含 LLM 密钥、数据库、Langfuse；前端选库/选模型由界面设置）
cp .env.example .env 2>/dev/null || true   # 本项目无 .env.example，.env 本身即配置

# 2. 启动后端（端口 2026）
python start_server.py
```

启动后：

| 地址 | 说明 |
|---|---|
| http://localhost:2026 | LangGraph API（`/ok` 健康检查，`/docs` OpenAPI） |
| http://localhost:2026/ui | LangGraph Studio UI（可单独调试两个 graph） |
| http://localhost:3000 | 前端（独立仓库 `harness-deep-agents-ui`，`npm run dev`） |

> 启动日志会打印就绪门控：MCP 工具列表为空则**拒绝启动**；Langfuse 连不上仅告警不阻断（[start_server.py:88](start_server.py#L88) `preflight_check`）。

### 1.2 目录导读（记住这几条就够起步）

```
nl2sql/
├── start_server.py             # 唯一入口：起 LangGraph API server（读 graph.json）
├── graph.json / langgraph.json # 图注册（两个文件相同，分别给 start_server / langgraph dev）
├── docker-compose.yml          # 生产编排：nginx + frontend + langgraph-api
├── Dockerfile                  # 后端镜像（uv sync 装依赖 + nodejs/mcp-echarts）
│
└── src/
    ├── agent/
    │   ├── main_agent.py              ★ 主 Agent（意图路由、异步委派、图表/报告）
    │   ├── graphs/nl2sql_agent.py     ★ NL2SQL 子 Agent（独立 graph）
    │   ├── skills/{main,nl2sql}/      ★ Skill 定义（SKILL.md + references + scripts）
    │   ├── middlewares/               ★ 中间件（15 个，见 §3.5）
    │   ├── tools/mcp_tool.py          MCP 多 server 客户端（wrenai_<库>_* / dbmcp_*）
    │   ├── tools/report_builder.py    build_report 报告工具
    │   ├── subagents/                 nl2sql.yaml、进度追踪、异步任务同步 watcher
    │   ├── trace/                     ★ Langfuse 客户端/span/v4 读封装 + 本地事件日志
    │   ├── eval/                      ★ collect_badcase / badcase_status / feedback_gate / run_experiment
    │   ├── prompt/                    MAIN_AGENT_PROMPT.md / NL2SQL_SYSTEM_PROMPT.md
    │   │                              （线上由 Langfuse prompt 版本管理接管，本地是兜底基线）
    │   ├── settings/                  env_loader / file_permissions / model_config_store
    │   ├── workspace_manager/         多工作区管理（AGENT_DATA_ROOT 外置）
    │   ├── checkpoint/                checkpointer_factory（PG/SQLite）
    │   ├── llms/                      create_model 模型工厂（deepseek/glm/kimi/qwen）
    │   ├── backends/                  DynamicFilesystemBackend 等
    │   └── feedback/                  FeedbackStore（SQLite 反馈库）
    ├── api/                           ★ 自定义 API 组合根 custom_app.py + 15 个路由模块
    ├── mcp_server/db_mcp_server/      DB MCP 服务（FastMCP，dbmcp_run_sql / dbmcp_get_db_info）
    └── test/                          测试脚本
```

### 1.3 文档导航（按需读）

| 场景 | 读哪篇 |
|---|---|
| 想改 Agent 行为 / 加 skill / 加工具 | 本文 §3~§5 |
| 想加监控指标 / 看 trace 层级 | [docs/langfuse平台/Langfuse标准Trace层级方案.md](docs/langfuse平台/Langfuse标准Trace层级方案.md) |
| 想跑评估 / 采集 badcase / A/B / 发版 | [docs/langfuse平台/NL2SQL-Langfuse全流程实现手册.md](docs/langfuse平台/NL2SQL-Langfuse全流程实现手册.md) |
| 所有管理命令速查 | [docs/langfuse平台/项目脚本命令手册.md](docs/langfuse平台/项目脚本命令手册.md) |
| 部署 / 发版 / 回滚 / 每日调度 | [docs/weint环境/NL2SQL-部署与更新手册.md](docs/weint环境/NL2SQL-部署与更新手册.md) |
| 历史优化方案的来龙去脉 | `docs/agent优化记录/*.md` |

### 1.4 日常开发「在哪改什么」速查

| 想做什么 | 改哪里 |
|---|---|
| 改主 Agent 意图/委派规则 | [prompt/MAIN_AGENT_PROMPT.md](src/agent/prompt/MAIN_AGENT_PROMPT.md)（线上经 Langfuse UI 改，见 §3.9） |
| 改 NL2SQL 流水线规则 | [prompt/NL2SQL_SYSTEM_PROMPT.md](src/agent/prompt/NL2SQL_SYSTEM_PROMPT.md) + [skills/nl2sql/sql-of-thought/SKILL.md](src/agent/skills/nl2sql/sql-of-thought/SKILL.md) |
| 改子 Agent 工具集/技能 | [subagents/configs/nl2sql.yaml](src/agent/subagents/configs/nl2sql.yaml) |
| 加一个 skill | 在 `src/agent/skills/{main,nl2sql}/<name>/SKILL.md` 新建（§5.3） |
| 加一个自定义 API | 在 `src/api/` 建模块暴露 `routes`，`custom_app.py` 里展开一行（§5.2） |
| 加一个中间件 | 继承 `AgentMiddleware`，挂到 `main_agent.py:239` / `nl2sql_agent.py:177` 的 middleware 列表（§5.1） |
| 加一个数据库连接 | 前端界面配置（落 `db_config.json`）；代码侧无需改（§3.7） |
| 加一个 Wren 语义库 | `api/wren_semantic.py` 提供全套管理 API（§3.11） |

---

## 2. 整体架构

### 2.1 架构图

```
┌───────────────────────────────────────────────────────────────────────────────┐
│  前端 harness-deep-agents-ui（独立仓库，Next.js）                               │
│   POST /threads/{id}/runs(stream) SSE 流式 · 1s 轮询 async_tasks · 反馈 👍/👎     │
└───────────────────────────────────────────────────────────────────────────────┘
                                  │  :2026
                                  ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  LangGraph API Server（uvicorn，langgraph_api.server:app）                      │
│   ├─ graph.json 注册 2 个 graph：                                                │
│   │    chat_agent   → main_agent.py:agent（主 Agent）                           │
│   │    nl2sql_agent → graphs/nl2sql_agent.py:agent（子 Agent 独立 graph）       │
│   ├─ LANGGRAPH_HTTP.app → src/api/custom_app.py（57 条自定义路由 + Langfuse 中间件）│
│   ├─ checkpointer → checkpoint/checkpointer_factory.py（生产 PG / 开发 SQLite）  │
│   └─ store（可选）→ 长期记忆 Store                                              │
└───────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  主 Agent（chat_agent）— deepagents create_deep_agent 单节点 agent               │
│   system prompt（MAIN_AGENT_PROMPT.md + 动态注入 db_name/关键词）               │
│   意图路由 → start_async_task 异步委派 → check_async_task 汇总                    │
│   → generate_echarts(图表, mcp-echarts) → build_report(报告, report_builder)     │
│   12 个中间件（QuotaError…vfs_path_resolver）· VFS 复合后端 · FILE_PERMISSIONS   │
└───────────────────────────────────────────────────────────────────────────────┘
                                  │ start_async_task（deepagents 异步子 Agent，独立 thread/run）
                                  ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  NL2SQL 子 Agent（nl2sql_agent）— 独立 graph，prompt 驱动的 skill 流水线          │
│   SQL-of-Thought：澄清门(Step0) → 策略C(Cube)>B(快速)>A(标准流水线) → run_sql     │
│   → 分类法纠错(≤3 次)                                                          │
│   双通道路由：语义层 wrenai_<库>_* / 直连 dbmcp_*                               │
│   只读硬拦截 sql_approval · 8 个 skill 按需 load_skill                          │
└───────────────────────────────────────────────────────────────────────────────┘
                │                     │                     │
                ▼                     ▼                     ▼
┌──────────────────────┐  ┌─────────────────────┐  ┌───────────────────────┐
│ WrenAI 语义层         │  │ DB MCP Server         │  │ 目标数据库             │
│ wrenai_<库>_* 工具链   │  │ dbmcp_*（stdio 子进程）│  │ MySQL/PG/CH/SQLite     │
│ MDL / Cube / 语义检索  │  │ run_sql / get_db_info │  │（db_config.json 定义） │
└──────────────────────┘  └─────────────────────┘  └───────────────────────┘

    支撑系统（横切）：
    ├─ Langfuse（trace/五维评分/prompt 版本/Dataset:badcase）—— 监控评估迭代闭环
    ├─ WorkspaceManager + AGENT_DATA_ROOT —— 多工作区数据隔离
    ├─ 本地事件日志 traces.sqlite（event_store）—— trace_routes API 数据源
    └─ 每日 cron：collect_badcase → feedback_gate → badcase_status
```

### 2.2 两个 graph、一个组合根

- **graph 不是手写 `StateGraph`**：主/子 Agent 都是用 `deepagents.create_deep_agent` 构建的「单节点 agent」，节点/边的行为由 **system prompt + skill 流水线 + 中间件** 驱动，不是显式图节点。找「流程逻辑」请去 prompt 和 SKILL.md，找「横切行为」去中间件。（新人不理解这一点会找不到 Node/Edge。）
- **两个 graph 各自独立注册**（[graph.json](graph.json)，[langgraph.json](langgraph.json) 内容相同）：
  - `chat_agent` → [src/agent/main_agent.py:232](src/agent/main_agent.py#L232) `agent`
  - `nl2sql_agent` → [src/agent/graphs/nl2sql_agent.py:199](src/agent/graphs/nl2sql_agent.py#L199) `agent`
- **主 Agent 通过 `AsyncSubAgent(name="nl2sql", graph_id="nl2sql_agent")`**（[main_agent.py:171](src/agent/main_agent.py#L171)）异步委派到另一个 graph，跨 thread 独立 run。
- **自定义 API 不另起服务**：`.env` 里 `LANGGRAPH_HTTP={"app": "src/api/custom_app.py:app"}`，LangGraph server 把组合根与原生路由合并进同一 Starlette app/端口。

### 2.3 一次查询的完整时序（代码路径）

```
1. 前端  POST /api/threads/{tid}/runs/stream
   → langgraph_api 原生路由；custom_app 的 LangfuseMetadataMiddleware 在请求体注入
     config.metadata（langfuse_session_id=thread_id、trace_name=chat-turn、db_name…）
   → 主 Agent 图开始执行（chat_agent graph）

2. 主 Agent 模型调用（循环）
   → system prompt = MAIN_AGENT_PROMPT.md 意图表 + dynamic_prompt 注入【当前数据库】
     + query_keywords 注入触发关键词 + current_db_context 注入最新用户消息
   → 判定「数据查询」→ 调用 start_async_task(description, "nl2sql")
     （deepagents 异步子 Agent 工具；sync_launcher patch 后启动同步 watcher）
   → 主 Agent 本轮回复「查询已提交」→ run 结束（state 里 active_queries[task_id]=true）

3. nl2sql 子 Agent（独立 thread/run，graph_id=nl2sql_agent）
   → dynamic_prompt 按 SemanticDbDetector 注入「语义层/直连」双通道指引（§3.3）
   → SQL-of-Thought 流水线按策略执行 skill（§3.4）→ run_sql 查库
   → SqlReadOnlyMiddleware 对 run_sql 做只读硬拦截（写/DDL 直接拒绝）
   → 子 Agent 返回摘要；sync watcher 写回主线程 state（async_tasks/query_headers）

4. 前端 1s 轮询 async_tasks → 见终态 → 发 auto-continue run（[系统自动通知] 消息）
   → 主 Agent 新一轮：check_async_task 拿结果 →（如需要）generate_echarts / build_report
   → 最终答复经 SSE 流式返回

5. 旁路（不影响主流程）：
   → LangfuseSpanMiddleware 给工具调用包 skill span，写确定性分 + 采样调度 LLM-judge
   → TraceRecorder 写本地事件日志；TokenMeter 累积 token_stats
```

---

## 3. 详细架构：分层解析

### 3.1 入口与运行时（怎么起、怎么注册）

| 文件 | 职责 | 关键逻辑 |
|---|---|---|
| [start_server.py](start_server.py) | 唯一入口 | `setup_environment`（L22）把 `src/` 插进 `sys.path`，读 [graph.json](graph.json)，把 graphs/checkpointer/store 序列化为 `LANGSERVE_GRAPHS` / `LANGGRAPH_CHECKPOINTER` / `LANGGRAPH_STORE` 环境变量；`preflight_check`（L88）就绪门控（MCP 工具为空不启动、Langfuse 失败仅告警）；`uvicorn.run("langgraph_api.server:app")`（L155）——直接用官方 API server，不走自定义 WSGI；文件日志轮转 `logs/agent-server.log`（每天 0 点、留 7 份） |
| [graph.json](graph.json) / [langgraph.json](langgraph.json) | 图注册 | 2 个 graph + `checkpointer: {backend: custom, path: ...checkpointer_factory.py:checkpointer}`；`start_server.py` 读前者，`langgraph dev` 读后者 |
| [src/api/custom_app.py](src/api/custom_app.py) | API 组合根 | import 15 个路由模块，`ROUTES` 展开 ~57 条路由，挂 `LangfuseMetadataMiddleware`，构建 `Starlette` app（L57）。被 `LANGGRAPH_HTTP.app` 钩子合并进 langgraph 进程 |
| [src/agent/checkpoint/checkpointer_factory.py](src/agent/checkpoint/checkpointer_factory.py) | Checkpointer | 双模式：`CHECKPOINT_DB_URI` 为 `postgresql://` 前缀 → `AsyncPostgresSaver`（进入上下文自动 `setup()` 建表）；否则 → `AsyncSqliteSaver`，路径 `shared/checkpoint/checkpoints.sqlite`，**全局共享、不随工作区切换**（L17-32） |

> **关键约束**：graph 定义里**不能**给 `create_deep_agent` 传 `checkpointer=`（会触发 langgraph dev 的 local_dev 校验拒绝加载），必须在 API 层（graph.json/环境变量）配置。

### 3.2 主 Agent（[src/agent/main_agent.py](src/agent/main_agent.py)）

**组装**（L232-247）：`create_deep_agent(model, tools=[*mcp_tools, build_report_tool], subagents=[nl2sql_async], middleware=[...], backend=composite_backend, permissions=FILE_PERMISSIONS, system_prompt=SYSTEM_PROMPT, state_schema=MainAgentState)`。

- **模型**：模块级 `deepseek_model`（无前端配置时为 None → 占位 `ChatOpenAI`，真实模型由 `ThinkingToggleMiddleware` 每次按 `configurable.llm_route/llm_model/enable_thinking` 重建）。
- **system prompt**：`_build_system_prompt`（L47）优先从 Langfuse `main_system_prompt`(production) 拉取（`get_prompt_text`，带 `required_markers` 内容校验 + `min_chars` 兜底），失败回退本地 [prompt/MAIN_AGENT_PROMPT.md](src/agent/prompt/MAIN_AGENT_PROMPT.md)，再替换 `{{CHART_SPEC}}` 等占位符。
- **意图路由**：不是硬编码工具，是 **prompt 意图表 + 关键词中间件**：
  - [prompt/MAIN_AGENT_PROMPT.md](src/agent/prompt/MAIN_AGENT_PROMPT.md) 定义意图（一般对话/数据查询/文档/图表/报告）与「委派 nl2sql 模板」（`【任务目标】【数据库名称】【run_sql LIMIT 规范】…`）及「子任务成功后固定协议」（check_async_task → generate_echarts → build_report）。
  - `dynamic_prompt`（L85）把 `configurable.db_name` **前置注入** system prompt 顶部，覆盖历史陈旧库名（否则切库后委派仍写旧库）。
  - `QueryKeywordsMiddleware` 把前端关键词注入 `**触发关键词**【数据查询】:` 标记行（[middlewares/query_keywords.py](src/agent/middlewares/query_keywords.py)），前后端零漂移。
- **State Schema**（`MainAgentState`，L190）：在 `DeepAgentState` 上扩展——
  - 并发多查询字段：`query_headers` / `subagent_steps_map` / `active_queries`，全部 `Annotated[..., _merge_dict_by_key]`（按 task_id 键控合并，防多线程互相覆盖）；
  - 旧单值字段（`query_header`/`subagent_steps`/`query_active`）仅单任务时镜像写，兼容旧前端；
  - `token_stats` 用 `_accumulate_token_stats` reducer 累积。
- **后端（VFS）**：`CompositeBackend`（L135）最长前缀匹配路由：
  ```
  /shared/memory/  → shared_memory_backend    （共享 memory，AGENTS/ORCHESTRATOR）
  /shared/skills/  → shared_skills_backend    （共享 skills）
  /workspace/      → workspace_data_backend   （当前工作区：report/tmp/…，DynamicFilesystemBackend 切换即时生效）
  /                → vfs_root_backend         （AGENT_DATA_ROOT，代码根退出 VFS）
  ```
  `artifacts_root="/workspace/"` 让自动压缩 offload（conversation_history/large_tool_results）落当前工作区。
- **文件权限**：`FILE_PERMISSIONS`（见 §3.8）——只读根、仅工作区可写。

### 3.3 NL2SQL 子 Agent（[src/agent/graphs/nl2sql_agent.py](src/agent/graphs/nl2sql_agent.py)）

- **配置来源**：[subagents/configs/nl2sql.yaml](src/agent/subagents/configs/nl2sql.yaml)——只配 `name/description/tools(子串匹配)/skills/system_prompt_file`；**模型与中间件在代码硬编码**（`loader.py` 是通用基建但 nl2sql 实际内联加载，见 [subagents/loader.py](src/agent/subagents/loader.py)）。
- **system prompt**：优先 Langfuse `nl2sql_system_prompt`(production)，回退本地 [prompt/NL2SQL_SYSTEM_PROMPT.md](src/agent/prompt/NL2SQL_SYSTEM_PROMPT.md)（`get_prompt_text`，L51-55）。
- **工具解析**（L57-64）：YAML `tools` 名字段对 `sub_tools`（wrenai 语义层 + dbmcp 直连）做子串匹配、去重。
- **dynamic_prompt 双通道路由**（L99-171，核心）——用 `SemanticDbDetector`（[utils/semantic_db.py](src/agent/utils/semantic_db.py)）实时判断当前 `db_name` 是否建模：
  - **已建模**（如 imdb）→ 注入语义层工具链指引（`wrenai_<库>_get_mdl/describe_schema/recall_queries/run_sql`），并扫 `cubes/*/metadata.yml` 注入 **Cube 清单** → 支持策略 C 快速通道；
  - **未建模**（如 Chinook_AutoIncrement）→ 注入直连指引（`dbmcp_get_db_info` / `dbmcp_run_sql`），并警示「误用语义层 run_sql 会报 not found，不要进纠错循环」。
- **中间件栈**（L177-197，顺序即注入顺序）：`QuotaError(最外)` → `TraceRecorder` → `skills` → `ProgressTracker` → `ThinkingToggle` → `ToolFilter`（按 db_name 过滤 wrenai 工具）→ `LangfuseSpan` → `dynamic_prompt` → `sql_approval(append)` → `WriteTodosProtocol(append, 最内)`。
- **图本身不是显式 StateGraph**：是「prompt 驱动的多 skill 顺序调用」（见 §3.4）。

### 3.4 Skill 系统与 SQL-of-Thought 流水线

**Skill 结构**：`SKILL.md`（frontmatter: name/description/version，description 即触发条件）+ `references/`（细则）+ `scripts/`（辅助脚本）。分两级：`skills/main/`（主 Agent 侧 5 个）+ `skills/nl2sql/`（8 个）。由 `SkillsMiddleware` 自动扫描并注入 `load_skill` 工具，LLM 按 frontmatter 触发加载。

| 分组 | 技能 | 作用 |
|---|---|---|
| main | `main-agent` | 意图分类/委派路由/子 Agent 清单/安全约束/结果信任规则 |
| main | `chart-saver` / `report-export` | 图表落盘（`scripts/save_chart.py`）、Markdown 报告模板 |
| main | `langfuse` / `langsmith-trace` / `alibabacloud-find-skills` | 观测与运维技能 |
| nl2sql | `sql-of-thought` | **编排器**：三策略决策树 + 流水线流程（见下） |
| nl2sql | `nl2sql-clarification` | Step 0 澄清门（问题不清则 `[需要澄清]` 停止） |
| nl2sql | `nl2sql-knowledge-loader` | 并行取业务知识（get_instructions + recall_queries + get_all_knowledge） |
| nl2sql | `nl2sql-schema-linking` | 并行取 Schema（describe_schema + get_context + get_mdl），裁剪最小集 |
| nl2sql | `nl2sql-subproblem` | 拆子句级子问题（JSON） |
| nl2sql | `nl2sql-query-plan` | CoT 步骤式查询计划（**禁 SQL**） |
| nl2sql | `nl2sql-sql-generation` | 唯一生成 SQL 的 skill（dry_run 验证） |
| nl2sql | `nl2sql-performance-optimization` | dry_run 通过后、执行前做性能优化 |
| nl2sql | `nl2sql-correction` | 错误分类法纠错（9 大类 31 小类，[error-taxonomy.md](src/agent/skills/nl2sql/nl2sql-correction/references/error-taxonomy.md)，最多 3 次） |

**SQL-of-Thought 策略决策**（[sql-of-thought/SKILL.md](src/agent/skills/nl2sql/sql-of-thought/SKILL.md)）：

```
用户问题 → Step0 澄清门(clear=false 则停止)
  → 先 list_cubes()：有匹配 → 策略C（Cube 通道，最省 token，跳过全部 NL2SQL 步骤）
  → 单表/简单筛选/COUNT → 策略B（快速通道：knowledge → sql-generation → run_sql）
  → 多表 JOIN/聚合/窗口 → 策略A（标准流水线）：
      knowledge-loader → schema-linking → subproblem → query-plan → sql-generation
      → performance-optimization → run_sql →（失败）correction(≤3 次)
```

**数据传递**：各 skill 优先从**对话上下文**取前序输出，`read_file` 仅作 fallback（避免文件传递延迟）。

### 3.5 中间件系统（横切行为全在这）

中间件均为 `langchain.agents.middleware.AgentMiddleware` 子类，挂 `create_deep_agent(..., middleware=[...])`。**顺序语义：列表第 0 项最外层，末项最内层（紧贴模型）**。

| 中间件 | 目的（一句话） | 挂载 | 文件 |
|---|---|---|---|
| `QuotaError` | LLM 额度耗尽(402/403) → 友好中文提示，不抛异常 | 主+子，最外层 | [middlewares/quota_error.py](src/agent/middlewares/quota_error.py) |
| `ExecuteGuard` | execute(shell) 护栏：拦破坏性命令/工作区外路径（护栏非安全边界） | 主 | [middlewares/execute_guard.py](src/agent/middlewares/execute_guard.py) |
| `SkillsMiddleware` | 扫描 SKILL.md 注入 `load_skill` 工具 | 主(sources=main/)+子(nl2sql/) | deepagents 内置 |
| `QueryKeywords` | 把前端关键词注入 system prompt 的【数据查询】标记行 | 主 | [middlewares/query_keywords.py](src/agent/middlewares/query_keywords.py) |
| `ThinkingToggle` | 按 configurable 重建模型（思考开关 + llm_route/llm_model 切换，进程级缓存） | 主+子 | [middlewares/thinking_toggle.py](src/agent/middlewares/thinking_toggle.py) |
| `MessageSlimmer` | 工具结果进 checkpoint 前瘦身：>`LARGE_RESULT_TRUNCATE_CHARS`(默认8000/16000)落盘截断 + 完全重复去重 | 主 | [middlewares/message_slimmer.py](src/agent/middlewares/message_slimmer.py) |
| `CurrentDbContext` | 把 db_name 注入最新用户消息（防切库后按旧库委派） | 主 | [middlewares/current_db_context.py](src/agent/middlewares/current_db_context.py) |
| `dynamic_prompt` | @dynamic_prompt：主 agent 注入库名；子 agent 注入双通道路由 | 主+子 | [main_agent.py:85](src/agent/main_agent.py#L85) / [nl2sql_agent.py:99](src/agent/graphs/nl2sql_agent.py#L99) |
| `TokenMeter` | 采集每次 LLM 调用耗时 + usage_metadata → state.token_stats | 主 | [middlewares/token_meter.py](src/agent/middlewares/token_meter.py) |
| `TraceRecorder` | 本地事件日志（LLM/tool/subagent 事件写 traces.sqlite） | 主+子 | [middlewares/trace_recorder.py](src/agent/middlewares/trace_recorder.py) |
| `LangfuseSpan` | 关键工具边界包 skill 级 span + 写确定性分 + 采样调度 LLM-judge | 主+子 | [middlewares/langfuse_span.py](src/agent/middlewares/langfuse_span.py) |
| `VfsPathResolver` | 模型输出后处理：`/workspace/…` 改写为真实磁盘路径（最内层） | 主 | [middlewares/vfs_path_resolver.py](src/agent/middlewares/vfs_path_resolver.py) |
| `ToolFilter` | 按 db_name 过滤 wrenai 工具（只暴露当前库） | 子 | [middlewares/tool_filter.py](src/agent/middlewares/tool_filter.py) |
| `SqlReadOnly` | run_sql 只读硬拦截：写/DDL 直接拒绝（不执行、不弹审批） | 子 | [middlewares/sql_approval.py](src/agent/middlewares/sql_approval.py) |
| `WriteTodosProtocol` | 追加 write_todos 分级协议（压过 deepagents 默认「简单任务可跳过」） | 子，最内 | [middlewares/write_todos.py](src/agent/middlewares/write_todos.py) |

> 另有 **不是中间件** 的全局 monkey-patch：`deepagents_async_config_patch`（[main_agent.py:10](src/agent/main_agent.py#L10) 导入即生效）在 `runs.create` 层把主 run 的 `configurable` 透传给子 run（过滤 `__pregel_*`/`langgraph_*` 内部键，否则 orjson 序列化失败），并登记 task→trace 映射。

**主 Agent 链（顺序即包裹关系）**：
`[QuotaError(最外), execute_guard, skills, query_keywords, thinking_toggle, message_slimmer, db_context, dynamic_prompt, TokenMeter, trace_recorder, LangfuseSpan, vfs_path_resolver(最内)]`

**子 Agent 链**：
`[QuotaError(insert0), TraceRecorder, skills, ProgressTracker, ThinkingToggle, ToolFilter, LangfuseSpan, dynamic_prompt, sql_approval, WriteTodosProtocol(最内)]`

### 3.6 工具层

**[tools/mcp_tool.py](src/agent/tools/mcp_tool.py)** — 多 MCP server 管理，主/子工具独立加载：

| 通道 | 工具前缀 | 提供者 | 用途 |
|---|---|---|---|
| 图表 | 无前缀（如 `generate_echarts`） | `mcp-server-echarts`（本地全局 bin） | 主 Agent 图表渲染 |
| 语义层 | `wrenai_<库名>_<工具>` | 每个已建模库一个 wren server，`MultiServerMCPClient(tool_name_prefix=True)` | 子 Agent：get_mdl/describe_schema/run_sql/query_cube 等 |
| 直连 | `dbmcp_run_sql` / `dbmcp_get_db_info` | `db_mcp_server` stdio 子进程 | 子 Agent：未建模库直接查 |

- **工具命名规则**：`wrenai_server_name(db)` = `"wrenai_" + 库名`（[utils/semantic_db.py:34](src/agent/utils/semantic_db.py#L34)），拼出 `wrenai_imdb_run_sql` 等；`dbmcp_*` 来自 db_mcp_server 的 `run_sql`/`get_db_info` 加前缀。
- **Wren fast-path**（[mcp_tool.py:91](src/agent/tools/mcp_tool.py#L91)）：`WREN_MEMORY_BACKEND=grep` 时 get_context/recall_queries 在主进程直调 wren Python API，绕过 MCP 子进程 + 420MB 嵌入模型加载（避免超时）。
- **wrap_tool 只给图表工具注入 outputType**（[utils/path_resolver.py](src/agent/utils/path_resolver.py)）：`generate_echarts` 需要 `outputType="option"` + 参数归一化 + 补缺轴；而 dbmcp/wrenai 工具 `args_schema extra="forbid"`，塞 outputType 会报错。`wrap_tool` 还做：`dbmcp_run_sql/get_db_info` 强制用 configurable.db_name 覆盖 LLM 旧值、**分级超时**（wrenai run_sql 300s / read-write-file 60s / execute 120s，超时返回友好消息让 LLM 决策）、图表结果 sanitize（ECharts option→交互式 HTML iframe / SVG→iframe / PNG→img）。

**[tools/report_builder.py](src/agent/tools/report_builder.py)** — `build_report` 工具（程序化装配报告）：从主 Agent 自身 state 里提取 `check_async_task` 最近一次 success 结果（按内容签名 `{status,thread_id,result}` 识别，因该 ToolMessage 不带 name）与 `generate_echarts` 最近的 iframe，拼成 Markdown 落盘活跃工作区 `report/`，返回 VFS 路径。省掉模型手工搬运 token。

### 3.7 数据与工作区

**[workspace_manager/manager.py](src/agent/workspace_manager/manager.py)** — `WorkspaceManager` 单例（`get_workspace_manager()`）：

- **AGENT_DATA_ROOT 外置**（L61-73）：配置后 `shared` → `<AGENT_DATA_ROOT>/shared`、默认工作区 → `<AGENT_DATA_ROOT>/workspace`，**代码根 `src/agent/` 彻底退出 VFS**；未配置回退仓库内 `src/agent/{shared,workspace}`；首次运行自动从仓库种子原子拷贝。
- **workspaces.json 注册表**：`{version, active, workspaces:{name:{path,...}}}`，原子读写。
- **隔离矩阵**（L267-350）：
  - 按工作区隔离：`db_config.json / semantic/ / report/ / tmp/ / nl2sql_process_data/ / large_tool_results/`
  - 全局共享：`memory/ / skills/ / model_config.json / checkpoint/ / trace/ / feedback/`
- **切换即时生效**：[backends/dynamic_workspace.py](src/agent/backends/dynamic_workspace.py) `DynamicFilesystemBackend` 每次文件操作前从 callable 重解析 root_dir，切工作区免重启。

**持久化**：
- checkpoint：`checkpointer_factory.py`（§3.1），全局共享、不随工作区。
- 用户反馈：[feedback/store.py](src/agent/feedback/store.py) `FeedbackStore`（SQLite `message_feedback.db`，PRIMARY KEY (thread_id,message_id)，`version` 乐观并发 CAS）。
- 事件日志：[trace/event_store.py](src/agent/trace/event_store.py) `traces.sqlite`（全局共享，WAL）。

### 3.8 配置与安全

**[settings/env_loader.py](src/agent/settings/env_loader.py)** — `load_env()` 加载顺序：**① 真实注入 env（docker env_file/手动 export）恒优先不覆盖 → ② `.env`（dev 基线，override=False 填缺）→ ③ `.env.prod` 的 `LANGFUSE_*` 覆盖**（仅 LANGFUSE_* 且不在 pre 中）。作用：本机/宿主机手动跑 eval/反馈脚本时连生产 Langfuse 项目。⚠️ `.env.prod` 的 `AGENT_DATA_ROOT=/app/data` 是容器路径，绝不能覆盖 `.env` 的本机路径。

**[settings/setting.py](src/agent/settings/setting.py)** — pydantic-settings 单例 `settings = create_settings()`；多 MySQL DB 靠 `DB_N_*` 环境变量（`get_databases()`）。

**配置权威性（重要设计）**：**前端是唯一配置来源**——模型（`model_config.json`，AES-256-GCM 加密，前端 CRUD）、当前库/关键词/思考开关（`configurable` 由前端随请求传）。`.env` 的 `LLM_*` 已边缘化，仅作开发兜底。多个中间件都踩过 `request.runtime.config` 恒空的坑，**统一走 `langgraph.config.get_config()`** 读 configurable。

**安全三层**：

| 层 | 机制 | 文件 |
|---|---|---|
| 文件 | `FILE_PERMISSIONS` 声明式规则：先 allow `/shared/**`、`/workspace/**`，再 deny `/**` 兜底；写仅 `/workspace/**` | [settings/file_permissions.py](src/agent/settings/file_permissions.py) |
| 执行 | `ExecuteGuardMiddleware` 拦 execute 破坏性命令/工作区外路径（**护栏非安全边界**，彻底隔离需换 OpenSandbox，[backends/](src/agent/backends/) 已备） | [middlewares/execute_guard.py](src/agent/middlewares/execute_guard.py) |
| SQL | `SqlReadOnlyMiddleware`：run_sql 类工具执行前 `classify_sql`，写/DDL（`_WRITE_LEADING` 集合）直接拒绝返回 error ToolMessage，不执行不审批；SELECT 无 WHERE/LIMIT/聚合 → `full_dump`（放行但 sql_valid 打 0.4） | [middlewares/sql_approval.py](src/agent/middlewares/sql_approval.py) |

### 3.9 可观测性（Langfuse + 本地事件日志）

**一次查询 = 一条 `chat-turn` trace**。三层实现：

1. **入口注入**：[api/langfuse_metadata.py](src/api/langfuse_metadata.py)（ASGI 中间件）拦截 POST run 端点，把 `langfuse_session_id=thread_id`、`langfuse_trace_name="chat-turn"`、`tags=["nl2sql"]`、`db_name/workspace/skills/prompt_label/release` 写进请求体 `config.metadata`。`LANGFUSE_ENABLE=false` 时透传不注入。
2. **trace 层级核心**：[trace/langfuse_client.py](src/agent/trace/langfuse_client.py) `_patch_handler_for_trace_nesting`（L364）monkey-patch `on_chain_start`：
   - 子 Agent 嵌套（M-T3）：子 agent root chain 启动时若 metadata 带 `langfuse_parent_trace_id`，构造 OTel `NonRecordingSpan`（**必须 `trace_flags=SAMPLED(0x01)`**，否则整棵子树未采样从不导出）→ 子 agent 整棵归入主 trace；
   - auto-continue 续跑（M-T3d）：从 `_THREAD_TRACE_MAP` 读该 thread 最近一次新查询的 trace，续跑不新开 chat-turn trace；
   - 任务级归属（M-T5）：`_TASK_TRACE_MAP`（task_id→发起它的查询，cap 2000 FIFO）供**连问场景**把续跑路由回正确的 trace。
   - 三张进程级映射：`_ROOT_OBS_MAP`（root observation id，供 skill span 挂父）、`_THREAD_TRACE_MAP`（thread→最近新查询）、`_TASK_TRACE_MAP`（task→查询）。
3. **skill span 结构化层**：[middlewares/langfuse_span.py](src/agent/middlewares/langfuse_span.py) `LangfuseSpanMiddleware`：按工具名后缀分类（schema-linking / sql-execution / sql-generation / recall-queries…），`start_observation` 建 `skill:<skill>:<tool>` span 嵌套到 agent root observation 下；span 结束写确定性分（`sql_valid_score`/`sql_exec_success`/`schema_match_score`）+ 采样调度 LLM-judge。

**v4 读封装**：[trace/langfuse_v4_reads.py](src/agent/trace/langfuse_v4_reads.py)（自托管 Langfuse v4 events_only 后 legacy 读接口 404）：`find_session_main_trace_id` / `find_message_trace_id` / `session_scores_map` / `list_scores` / `list_user_traces` 等；`USER_FEEDBACK_REVOKED=-1.0` 为撤销哨兵分。

**总开关**：`LANGFUSE_ENABLE`（默认 true；false 停一切运行时埋点/打分/prompt 拉取，但 `get_client()` 仍可用，管理工具照跑）。三个关键函数签名：

```python
create_score(name, value, trace_id="", observation_id="", comment="", ...) -> bool   # langfuse_client.py:549
get_prompt_text(name, label=None, fallback="", required_markers=None, min_chars=0) -> str   # :604
resolve_prompt_label() -> str   # 显式 LANGFUSE_PROMPT_LABEL > canary 掷骰 > production，:99
```

**本地事件日志**（与 Langfuse 并行，不依赖外部）：[trace/event_log.py](src/agent/trace/event_log.py)（数据模型）+ [trace/event_store.py](src/agent/trace/event_store.py)（traces.sqlite 全局共享）+ [trace/session_lineage.py](src/agent/trace/session_lineage.py)（谱系查询引擎）。供 `api/trace_routes.py` 做实时事件/谱系/统计。

### 3.10 评估与迭代闭环（Langfuse 全流程）

五个模块 + prompt 管理，构成「采集→复审→修复→回归→灰度→放量」闭环（操作手册见 [NL2SQL-Langfuse全流程实现手册.md](docs/langfuse平台/NL2SQL-Langfuse全流程实现手册.md)）：

| 模块 | 职责 | 关键逻辑 |
|---|---|---|
| [eval/evaluators.py](src/agent/eval/evaluators.py) | 在线五维评分 | 确定性维同步零成本（`sql_valid_score` 复用 `classify_sql`、`schema_match_score`、`sql_exec_success`+`looks_like_exec_error`）；LLM-judge 维（`sql_biz_correct`/`report_table`/`analysis_report`）按 `NL2SQL_EVAL_JUDGE_SAMPLE`（默认 0.3）daemon 线程采样，`schedule_judge`（L249） |
| [eval/collect_badcase.py](src/agent/eval/collect_badcase.py) | 每日采集 | 4 类命中任一入 `Dataset:badcase`：系统异常 / 五维<0.6 / user-feedback=0 / sql_exec_success=0；`badcase_collected.json` stamp 去重；`register_batch` 注册 pending；进程外独立跑 `python -m agent.eval.collect_badcase --days N` |
| [eval/badcase_status.py](src/agent/eval/badcase_status.py) | 状态机 | `{workspace}/eval/badcase_status.json` 以 source_trace_id 主键；`pending→reviewed→fixed/invalid`；回归集(open)=pending+reviewed；CLI `list/summary/mark/review` |
| [eval/feedback_gate.py](src/agent/eval/feedback_gate.py) | 在线门禁 | 拉 user-feedback → 按 trace 去重取最新、剔 value<0 撤销哨兵 → 按 trace metadata `prompt_label` 分组 → 比较 candidate/reference 好评率；exit 0=通过 / 1=回归 / 2=数据不足(--fail-insufficient) |
| [eval/run_experiment.py](src/agent/eval/run_experiment.py) | 离线 A/B | 每个 label 一个 **worker 子进程**（import graph 前注入 `LANGFUSE_PROMPT_LABEL`，顶层 import nl2sql_agent，逐条 ainvoke）；聚合 `CORE_DIMS=(sql_biz_correct_score, sql_valid_score, sql_exec_success)`，candidate < reference − threshold → exit 1；`--from-badcase` 从 Dataset 装载并按状态过滤 |
| [prompt/sync_prompts.py](src/agent/prompt/sync_prompts.py) | prompt 版本管理 | 同步 `main_system_prompt`/`nl2sql_system_prompt`（本地文件→Langfuse 单向初始化）；`--skills` 把每个 SKILL.md 同步为 `skill/{group}/{dir}`；标签 `latest`(恒最新)/`production`(对外)/`staging|prod-a|prod-b`(灰度)；`--force` 内容未变也 mint 新版本 |

**三道闸门**（发版安全网）：① SQL 只读硬拦截（运行时，§3.8）；② 离线回归门禁（run_experiment，发版前）；③ 在线真实反馈门禁（feedback_gate，放量后）。

### 3.11 API 层（[src/api/](src/api/)）

组合根 [custom_app.py](src/api/custom_app.py) 注册 ~57 条自定义路由（与 langgraph 原生路由同进程同端口）：

| 模块 | 路径 | 用途 |
|---|---|---|
| workspace | `/api/workspaces*` | 工作区列表/注册/切换/激活 |
| db_config | `/api/db-configs*`、`/healthz` | 数据库配置 CRUD + 连通测试 |
| model_config | `/api/model-configs*` | 模型 provider CRUD + 探活 |
| message_feedback | `PUT/DELETE /api/threads/{tid}/messages/{mid}/feedback`、`/api/threads/{tid}/feedback`、`/api/feedback/export` | 用户反馈写库(CAS)+后台 Langfuse 打分 / 撤销(哨兵分) / 导出 |
| auto_title | `POST /api/auto-title` | LLM 生成会话标题 |
| report_file | `GET /api/reports/{filename}?download=1` | 报告预览/下载（路径穿越防护，RFC5987 中文文件名） |
| sql_approval | `POST /api/threads/{tid}/sql-approval` | SQL 审批恢复（v1 遗留，现写/DDL 已硬拦截不再触发） |
| task_cancel | `POST /api/threads/{task_id}/cancel` | 停止异步子任务（取消 run + 回写 async_tasks 终态） |
| thread_compact | `POST /api/threads/{tid}/compact` | 手动压缩上下文（LLM 摘要 + RemoveMessage） |
| thread_export | `GET /api/threads/{tid}/export?format=md\|json` | 会话日志导出 |
| thread_fork | `POST /api/threads/{tid}/fork` | 会话分叉 |
| thread_search | `GET /api/threads/fts?q=` | 会话全文搜索（SQLite FTS5 trigram） |
| wren_semantic | `/api/wren-projects*`（18 条） | 语义库管理（列表/创建/introspect/generate-models/knowledge/push/build/validate） |
| trace_routes | `/api/traces/*`（5 条） | 事件查询/谱系树/统计/LLM 调用/子任务事件 |
| langfuse_metadata | 中间件 | POST run 注入 config.metadata（§3.9） |

### 3.12 DB MCP 服务（[src/mcp_server/db_mcp_server/](src/mcp_server/db_mcp_server/)）

- 独立 **FastMCP** 服务（[db/db_server.py](src/mcp_server/db_mcp_server/db/db_server.py)），被子 Agent 以 **stdio 子进程** 拉起（`mcp_tool.py` 注册 `dbmcp` server，`NL2SQL_DBMCP_ENABLED` 控制）。
- 暴露 2 个工具：`run_sql`（多语句执行，服务端默认 LIMIT）→ 加前缀成 `dbmcp_run_sql`；`get_db_info`（表清单）→ `dbmcp_get_db_info`。
- 引擎：`_RUNNER_REGISTRY` 实际可路由 **4 个**——mysql（PyMySQL）/ clickhouse（clickhouse_connect）/ postgres（psycopg3）/ sqlite。`engine/` 下另有 mssql/oracle/presto/snowflake/bigquery/duckdb 六个 runner（vanna 遗留，未进 registry）。
- 连接配置：[db/core/db_config_store.py](src/mcp_server/db_mcp_server/db/core/db_config_store.py)（db_config.json + AES-256-GCM 加密），[db/config.py](src/mcp_server/db_mcp_server/db/config.py) 优先读 store、回退 `.env` `DB_N_*`。

---

## 4. 关键机制详解（代码逻辑）

### 4.1 异步子 Agent 与续跑链

```
主 Agent start_async_task(description, "nl2sql")
  → deepagents 异步子 Agent：创建独立 thread + client.runs.create(graph_id=nl2sql_agent)
  → 立即返回 task_id；主 Agent 本轮结束
  → sync_launcher(monkey-patch) 启动守护线程 _async_sync_loop（sync_subagent_todos.py）
     每 0.5s：读子 run 状态 → 写回主线程 state（query_headers/active_queries/subagent_steps_map）
     → 子 Agent 完成 → 写 async_tasks[task_id]=终态
  → 前端 1s 轮询 async_tasks 见终态 → 发 auto-continue run（[系统自动通知] 消息）
  → 主 Agent 新一轮：check_async_task → 结果/图表/报告 → 最终答复
```

关键文件：
- [subagents/sync_launcher.py](src/agent/subagents/sync_launcher.py)（patch `start_async_task` 启动同步 watcher）
- [subagents/sync_subagent_todos.py](src/agent/subagents/sync_subagent_todos.py)（`_async_sync_loop` L191：0.5s 轮询、`_SYNC_WRITE_LOCK` 串行化、靠 `MainAgentState` 的 `_merge_dict_by_key` reducer 合并写回；`_notify_main_agent_continue` L1128 是后端 auto-continue 注入实现，当前由前端驱动）
- [subagents/check_progress.py](src/agent/subagents/check_progress.py)（增强 check_async_task：running 态也返回进度/步骤/耗时，success 态 `_summarize_result` 摘要）
- [subagents/track_progress.py](src/agent/subagents/track_progress.py)（ProgressTrackerMiddleware：从 write_todos ToolMessage 提取步骤写临时进度文件）

### 4.2 双通道路由与切库

- 子 Agent 每次模型调用前 `dynamic_prompt` 用 `SemanticDbDetector.is_modeled(db_name)` 判断（[semantic_db.py](src/agent/utils/semantic_db.py)）：建模 → `wrenai_<库>_*`；未建模 → `dbmcp_*`。
- **切库链路**：前端选库 → `configurable.db_name` 随 run 请求 → `deepagents_async_config_patch` 透传给子 run → 主/子 dynamic_prompt + `ToolFilterMiddleware` + `CurrentDbContext` 都读它。任何一层读不到都会导致切库仍连旧库（本项目踩过：必须 `langgraph.config.get_config()` 读取，且前置注入覆盖历史）。

### 4.3 上下文瘦身（防 checkpoint O(n²) 膨胀）

`MessageSlimmerMiddleware`（[middlewares/message_slimmer.py](src/agent/middlewares/message_slimmer.py)）在工具结果**进入 checkpoint 前**做后处理：文本 > `LARGE_RESULT_TRUNCATE_CHARS`（默认 8000，`LARGE_RESULT_TRUNCATE_CHARS=16000` 与 Langfuse span 对齐）→ 落盘 `/workspace/large_tool_results/<tool_call_id>`，消息内留 head+tail 预览；完全重复结果 → 占位（保留 tool_call_id/name，不影响工具配对）。全程 fail-open。

### 4.4 Trace 归属（为什么「一次查询=一条 trace」）

见 §3.9。核心心智：**一条 chat-turn trace = 一次用户新查询**；子 Agent 嵌套进去、续跑不新开、连问时按 task_id 路由回原 trace。判据：trace 名=chat-turn，子 Agent 是 NonRecordingSpan 嵌套（`trace_flags=0x01` 必须 SAMPLED）。

### 4.5 配置权威性 & 前端闭环

前端是唯一配置源（模型/库/关键词/思考开关/反馈），后端只做承接与安全校验；`.env` 的 LLM_* 是开发兜底。反馈闭环：前端 👍/👎 → `message_feedback` API → 本地 store + Langfuse `user-feedback` score → collect_badcase → Dataset → 回归集 → 门禁。

---

## 5. 开发指南（新人上手后要干的活）

### 5.1 加一个中间件

1. 在 [middlewares/](src/agent/middlewares/) 建 `xxx.py`，继承 `AgentMiddleware`，实现 `wrap_model_call` / `wrap_tool_call`（同步）或 `awrap_*`（异步）；
2. 挂到目标链：主 Agent 改 [main_agent.py:239](src/agent/main_agent.py#L239) 的 middleware 列表；子 Agent 改 [nl2sql_agent.py:177](src/agent/graphs/nl2sql_agent.py#L177)；
3. 注意顺序语义（第 0 项最外层）；读 configurable 统一用 `langgraph.config.get_config()`。

### 5.2 加一个自定义 API

1. 在 [src/api/](src/api/) 建模块，暴露 `routes: list[BaseRoute]`；
2. [custom_app.py:36](src/api/custom_app.py#L36) 的 `ROUTES` 里展开一行 `*api.xxx.routes`（组合根模式）；
3. 重启后端即可同端口 2026 访问。

### 5.3 加一个 Skill

1. 在 [skills/{main|nl2sql}/](src/agent/skills/) 下建目录 + `SKILL.md`（frontmatter 的 `description` 是触发条件）；
2. 需要细则放 `references/`、脚本放 `scripts/`；
3. 新增后跑一次 `sync_prompts --skills`（把 SKILL.md 同步进 Langfuse，消 Prompt-not-found 警告；本机 seed 逻辑见 [workspace_manager/manager.py](src/agent/workspace_manager/manager.py) `_seed_data_root_once`）。

### 5.4 加一个数据库

1. 前端界面填连接信息（落 `db_config.json`，密码 AES 加密）；代码无需改；
2. 想走语义层：在 Wren 语义库管理中建模（wren_semantic API / wren 项目），子 Agent 会自动切到 `wrenai_<库>_*` 通道。

### 5.5 跑评估 / 采集 / 门禁命令

生产前缀 `DC()`（容器）见 [全流程手册 §1.1](docs/langfuse平台/NL2SQL-Langfuse全流程实现手册.md)，开发机用 `.venv` python。常用：

```bash
# 采集 badcase（昨天）
DC -m agent.eval.collect_badcase --days 1
# 状态机：复审 / 汇总 / 标记
DC -m agent.eval.badcase_status review
DC -m agent.eval.badcase_status summary
DC -m agent.eval.badcase_status mark <trace_id前缀> fixed --note "..."
# 离线 A/B 回归门禁
DC -m agent.eval.run_experiment --from-badcase --labels production staging
# 在线真实反馈门禁
DC -m agent.eval.feedback_gate --days 7 --ref production --cand staging
# 同步 prompt
DC -m agent.prompt.sync_prompts --all --label staging --force
```

### 5.6 常见坑速查

| 坑 | 说明 |
|---|---|
| 容器 exec 报 `No module named 'agent'` | 容器 venv 是 `uv sync --no-install-project`，无 `_nl2sql_src.pth`；须 `PYTHONPATH=/app/src` + `/app/.venv/bin/python` + `bash -c`（别用 `bash -lc` 会丢 venv PATH） |
| `request.runtime.config` 恒空 | 读 configurable 统一用 `langgraph.config.get_config()` |
| 切库仍连旧库 | db_name 链路：前端 → configurable → 透传 patch → 各中间件；见 §4.2 |
| 子 Agent 拿不到前端配置 | `deepagents_async_config_patch` 必须**在 create_deep_agent 之前导入**（main_agent.py 顶部） |
| 手工 SpanContext 不导出 | 必须 `trace_flags=TraceFlags(0x01)`，否则整棵子树未采样（Langfuse 恒 404） |
| `trace.list` 单页限制 | ≤100，分页拉取；v4 `observations?sessionId=` 参数 422，须用 filter |
| graph 传 checkpointer 被拒 | 必须在 API 层（graph.json / 环境变量）配置，graph 定义里不要传 |
| `.env.prod` 覆盖本机路径 | `AGENT_DATA_ROOT=/app/data` 是容器路径，宿主机手动跑 eval 别让它盖掉 `.env` 的本机路径（env_loader 只叠 LANGFUSE_*） |
| 前端 build 卡 `@ts-expect-error` | DLP 加密行遗留，`next.config.ts` 已临时 `ignoreBuildErrors` |

---

## 6. 部署与运维

### 6.1 部署形态

- **生产**：Docker Compose（[docker-compose.yml](docker-compose.yml)）——`nginx:80` → `frontend:3000` + `langgraph-api:2026`；后端 `env_file: .env.prod`，`AGENT_DATA_ROOT=/app/data`（卷持久化），checkpoint 用宿主机 PostgreSQL（`CHECKPOINT_DB_URI`）。镜像见 [Dockerfile](Dockerfile)（阿里云源加速、去掉 wrenai memory extra 省 1.3GB、时区 Asia/Shanghai）。
- **开发**：`python start_server.py`（inmem 运行时 + SQLite checkpoint）。

### 6.2 每日自动调度（生产）

宿主机 cron → `scripts/daily_collect_badcase.sh` → docker exec 三步（02:13）：

```
① collect_badcase --days 1   → 采集 Dataset:badcase + 注册 pending
② feedback_gate --days 7     → 在线门禁（production vs canary）
③ badcase_status summary     → 待复审提醒
```

注册/启停见 [docs/weint环境/NL2SQL-部署与更新手册.md](docs/weint环境/NL2SQL-部署与更新手册.md) §2.6。

### 6.3 发版与回滚（prompt 版本）

- 线上改 prompt 在 **Langfuse UI** 编辑 → 打 `staging` → 离线门禁 `run_experiment` → canary（`LANGFUSE_CANARY_LABEL/RATIO`）→ 在线门禁 `feedback_gate` → 打 `production` 标签全量。
- 回滚手段（由局部到全局）：① 退 canary 环境变量 → ② `LANGFUSE_PROMPT_LABEL=production` → ③ UI 把 production 标签移回旧版本 → ④ `LANGFUSE_PROMPT_ENABLED=0` 强制本地 → ⑤ `LANGFUSE_ENABLE=false` 总关。

---

## 7. 相关文档导航

| 文档 | 内容 |
|---|---|
| [docs/langfuse平台/NL2SQL-Langfuse全流程实现手册.md](docs/langfuse平台/NL2SQL-Langfuse全流程实现手册.md) | Langfuse 监控/评估/数据集/迭代/A/B/发版全流程操作手册（含实例走读） |
| [docs/langfuse平台/Langfuse标准Trace层级方案.md](docs/langfuse平台/Langfuse标准Trace层级方案.md) | M-T1~T6 trace 层级 / 归属规范（为什么一次查询一条 trace） |
| [docs/langfuse平台/项目脚本命令手册.md](docs/langfuse平台/项目脚本命令手册.md) | 全部 eval/prompt 命令参数表 |
| [docs/langfuse平台/BadCase状态标记使用指南.md](docs/langfuse平台/BadCase状态标记使用指南.md) | badcase_status 使用细节 |
| [docs/weint环境/NL2SQL-部署与更新手册.md](docs/weint环境/NL2SQL-部署与更新手册.md) | 生产部署 / 容器 / cron / 发版回滚 |
| [README.md](README.md) | 快速开始 + 工具表（部分描述已过时，以本文为准） |
| `docs/agent优化记录/*.md` | 各子系统优化方案历史（并发多查询/上下文管理/消息瘦身/路径解析等） |
