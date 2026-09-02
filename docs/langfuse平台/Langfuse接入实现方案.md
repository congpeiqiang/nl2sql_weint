# Langfuse 接入实现方案（监控 · 评估 · 版本管理 · 灰度 · 发版）

> 目标平台：**Langfuse Cloud**（`https://cloud.langfuse.com`，EU 区）
> SDK：`langfuse>=4.14.4`（已装于 `pyproject.toml`）；`.env` 已有 `LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL`
> 文档性质：**实现方案（规划稿），暂不实施**
> 总开关：`.env` `LANGFUSE_ENABLE`（默认 `true`；设 `false` 一键停用运行时埋点/打分/prompt 拉取，`get_client()` 本体仍可用供管理工具读历史）。向下还有 `LANGFUSE_PROMPT_ENABLED`（M4，仅控 prompt 拉取）
> 关联文档：
> - [Langfuse连接说明.md](Langfuse连接说明.md)（自托管 v2 说明，本方案不采用，仅背景）
> - [1主1子多Skill NL2SQL+报表报告+全链路可观测+BadCase迭代闭环生产架构.md](1主1子多Skill%20NL2SQL%2B报表报告%2B全链路可观测%2BBadCase迭代闭环生产架构.md)（已设计的总体架构愿景）

---

## 0. 结论摘要（TL;DR）

| 能力 | Langfuse 对应能力 | 落地形态 | 工作量量级 |
|------|------------------|----------|-----------|
| Agent 监控（主/子 agent、模型、工具） | Tracing + Agent Graph | 两个 graph 的 `.with_config` 注入 `CallbackHandler` + trace 合并 | 小（核心改动 2 处 + 1 个公共模块） |
| 评估 | Scores / LLM-as-a-Judge / Code Evaluators / Datasets / Experiments | 五维评分 + 规则评估 + 离线实验 | 中 |
| 版本管理 | Prompt Management（版本 + 标签 + 回滚） | 主/子系统提示词迁入 Langfuse，label 管理 | 中 |
| 灰度测试 | Prompt A/B（`prod-a`/`prod-b`）+ Environments | label 路由 + user 级 canary | 中 |
| 发版 | Releases & Versioning + Experiments 在 CI/CD 门禁 | `release` 标记 + 实验阈值门禁 + 每日 BadCase 采集 | 中 |

**一句话架构**：用 Langfuse 官方 LangChain/DeepAgents 集成（`langfuse.langchain.CallbackHandler`）自动埋点两个 deepagents graph；主-子 trace 通过 `configurable.langfuse_trace_id` 透传合并成一条；五维评分 + 用户反馈作为 Scores；主/子系统提示词与图表模板迁入 Prompt Management；Dataset + Experiments 支撑离线回归与灰度门禁；`release`/`version`/`environment` 三个属性支撑发版与多环境隔离。

> ⚠ **现状警示（2026-08-24 实测）**：Langfuse 云项目已进入 **v4 `events_only` 模式**——**写入/埋点（trace、score ingestion）正常**，但 v3 读接口批量不可用：
> `trace.list`/`trace.get`/`scores.get_many` → 404「not available in events_only」；`dataset_items.list` → 404「Dataset not found」；`prompts.list` → 空、`get_prompt(label=production)` → 404「Prompt not found」。
> **对运行的影响 = 恰好落在本文设计的兜底上**：装配处拉不到 prompt → 自动回退本地文件（`metadata.prompt.source=local`），服务不崩、功能照常。**对工具链的影响**：M1–M7 所有基于 v3 读接口的验证/采集脚本（collect_badcase / feedback_gate / run_experiment --from-badcase / sync 校验 / d:/tmp 验收脚本）当前跑不通，需 v4 读 API 迁移后才能恢复。这是**独立于本方案的平台侧变更**，是否迁移、何时迁移需在 Langfuse Cloud 项目确认 v4 迁移状态后另行排期。

---

## 1. 现状盘点（接入点）

| 现状 | 说明 |
|------|------|
| `langfuse>=4.14.4` | 已安装（`pyproject.toml`），包含 `langfuse.langchain.CallbackHandler`、`get_client()` |
| `.env` | 已有 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL=https://cloud.langfuse.com`（确认云端） |
| 环境加载 | `start_server.py` 用 `load_dotenv(.env)`；`langgraph.json` 配 `"env": ".env"`，两种启动方式都能读到 key |
| 两个 graph | `src/agent/main_agent.py:agent`（chat_agent 主调度）与 `src/agent/graphs/nl2sql_agent.py:agent`（nl2sql 子查询），**都已 `.with_config({"recursion_limit": 500})`** |
| 模型层 | 全部是 LangChain Chat 模型（`create_model()`：ChatDeepSeek/ChatGLM/ChatKimi/ChatQwen/ChatOpenAI）→ CallbackHandler 自动捕获模型名、token、耗时、成本 |
| 工具层 | MCP 工具（`dbmcp_run_sql`、`wrenai_*`、`start_async_task` 等）→ CallbackHandler 自动捕获工具调用 |
| 子 agent 机制 | `AsyncSubAgent`（`start_async_task` 独立 run）；`deepagents_async_config_patch.py` 已把主 run 的 `configurable` 透传给子 run（并已注入 `trace_parent_thread_id`）→ **`langfuse_trace_id` 可零改造透传** |
| 本地自含追踪 | `TraceRecorderMiddleware` 已写本地 `traces.sqlite`（事件级，非 trace 树）→ 与 Langfuse 并存，Langfuse 负责可观测/评估，本地事件库保留兜底 |
| 大数据处理 | `MessageSlimmerMiddleware` + VFS 已把 >8000 字符结果落盘为 `vfs://` 路径 → **Span 只存路径字符串，trace 不膨胀**，与用户架构文档的「VFS 大结果隔离」同构 |

### 需要新增/修改的文件（规划）

| 文件 | 动作 |
|------|------|
| `src/agent/trace/langfuse_client.py` | **新增**：Langfuse 客户端/CallbackHandler 单例 + 初始化 + 公共工具函数 |
| `src/agent/main_agent.py` | 修改：`.with_config` 并入 `callbacks`；加 `LangfuseContextMiddleware` |
| `src/agent/graphs/nl2sql_agent.py` | 修改：`.with_config` 并入 `callbacks`；读 `langfuse_trace_id` 合并 trace |
| `src/agent/middlewares/langfuse_context.py` | **新增**：请求级上下文中间件（trace 命名、session/user/tags/version、trace_id 生成） |
| `start_server.py` / `custom_app.py` | 修改（可选）：run 入口统一注入 `langfuse_trace_id` 到 configurable |
| `.env` | 追加 `LANGFUSE_TRACING_ENVIRONMENT`（可选，默认 `production`） |
| 前端 `harness-deep-agents-ui` | 追加：用户反馈按钮（满意/不满意 → 写 score）；trace 链接跳转（可选） |

---

## 2. 阶段一：全链路监控（Tracing）

### 2.1 公共初始化模块（新增 `src/agent/trace/langfuse_client.py`）

Langfuse 官方 DeepAgents/LangGraph 集成的核心模式（来源：[LangChain DeepAgents 集成](https://langfuse.com/integrations/frameworks/langchain-deepagents)、[LangGraph 集成](https://langfuse.com/integrations/frameworks/langgraph)）：

```python
# 方案示例（未实施）
import os
from langfuse import get_client
from langfuse.langchain import CallbackHandler

def get_langfuse_handler() -> CallbackHandler:
    """Langfuse 客户端单例 + LangChain CallbackHandler。
    必须在使用前确保环境变量已加载（start_server.py 已 load_dotenv）。
    """
    langfuse = get_client()          # 读 LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL
    return CallbackHandler()          # 自动捕获 model/tool/chain/agent 观察

def assert_connected() -> None:
    get_client().auth_check()         # 启动预检（可并入 start_server.py 的 preflight_check）
```

要点：
- **导入顺序**：Langfuse 必须在环境变量加载之后导入（skill 最佳实践：`load_dotenv` 之后），`start_server.py` 的 `setup_environment()` 已满足。
- `langfuse.langchain.CallbackHandler`（新 API）而非旧 `langfuse.callback.CallbackHandler`（v4 已迁移）。
- 客户端是进程级单例（`get_client()`），主/子 graph 共享同一实例与后台批量上报队列。

### 2.2 主 agent 接入（`main_agent.py`）

两个 graph 都已经是 `create_deep_agent(...).with_config(...)`，LangGraph Server 场景官方推荐做法就是 **把 callbacks 并入 with_config**（无需每次调用手传）：

```python
# 方案示例（未实施）—— main_agent.py 第 204-214 行
from agent.trace.langfuse_client import get_langfuse_handler
...
agent = create_deep_agent(
    model=_agent_model,
    tools=mcp_tools,
    subagents=[nl2sql_async],
    ...
    system_prompt=SYSTEM_PROMPT,
    state_schema=MainAgentState,
).with_config({
    "recursion_limit": 500,
    "callbacks": [get_langfuse_handler()],   # ← 新增
})
```

`nl2sql_agent.py` 第 162-172 行同样处理。

### 2.3 自动捕获范围（无需手写埋点）

CallbackHandler 基于 LangChain callbacks，自动生成完整 trace 树：

- **Generation（模型调用）**：DeepSeek/GLM/Kimi/Qwen 每次 LLM 调用 → 模型名、input/output、token 用量（自动成本计算）、耗时。
- **Tool（工具调用）**：`dbmcp_run_sql` / `wrenai_*` / 文件工具 / `start_async_task` → 工具参数与结果。
- **Agent / Chain（agent 内部规划）**：deepagents 的调度、子 agent 交互。
- **Agent Graph 视图**：Langfuse v4 的 Agent Graph 会自动把上述观察聚合成 DAG（[Agent Graphs](https://langfuse.com/docs/observability/features/agent-graphs)）。

### 2.4 主-子 trace 合并（1 次用户查询 = 1 条 trace）

**问题**：主 agent 与 nl2sql 子 agent 是两个独立 graph run（`start_async_task` 建独立 run），默认产生两条 trace。

**方案（利用现有透传机制）**：

1. **trace_id 生成与注入**：在 run 入口（`custom_app.py`/`start_server.py` 或前端 configurable）为每次用户请求生成 `langfuse_trace_id = Langfuse.create_trace_id()`，放入 `configurable.langfuse_trace_id`。
2. **自动透传**：现有 `deepagents_async_config_patch.py` 会把 `configurable`（过滤后）透传给子 agent run → `langfuse_trace_id` 自动到达子 graph，**零改动**。
3. **子 graph 加入父 trace**：nl2sql graph 的 run 边界读到 `configurable.langfuse_trace_id` 后，用 Langfuse 官方「嵌套 agent 合并」模式（LangGraph cookbook Example 5）包裹执行：

```python
# 方案示例（未实施）—— 子 agent run 边界
from langfuse import get_client
client = get_client()
with client.start_as_current_observation(
    as_type="span", name="nl2sql-subagent",
    trace_context={"trace_id": cfg.configurable["langfuse_trace_id"]},  # 合并到主 trace
):
    result = await graph.ainvoke(input, config)   # CallbackHandler 观察挂到该 trace 下
```

4. **兜底方案（保证可用）**：若 3 的 contextvar 跨 async run 传递在本部署（langgraph-api 异步任务池）下不可靠，退化为 **session 分组**：主/子 trace 都设 `session_id = thread_id`，两条 trace 在 Sessions 视图自动成组，用 `trace_parent_thread_id`（已透传）做关联。**先落兜底，再优化为 trace 合并**，避免阻塞主线。

> 注意：异步子 agent 的 trace 在父 run 结束前/后才上报，Langfuse 端到端一致（最终一致），Agent Graph 视图会在子 trace 到达后补全。

### 2.5 Trace 质量（命名 / 属性 / 可筛选）

按 [Best Practices（好的 trace 长什么样）](https://langfuse.com/docs/observability/best-practices) 基线，请求级注入（`propagate_attributes` / `start_as_current_observation`）：

| 属性 | 值 | 作用 |
|------|-----|------|
| `trace_name` | `nl2sql-user-query` | trace 可读、可筛选 |
| `session_id` | `thread_id`（LangGraph 线程 id） | 多轮对话分组（Sessions 视图） |
| `user_id` | 登录用户名（前端可传） | 用户维度成本/质量归因 |
| `tags` | `["production", "db:imdb", "nl2sql"]` + 任务类型（查询/报表/报告） | 功能维度过滤、自定义 Dashboard |
| `metadata` | `{db_name, model, chart_engine, query_keywords, skills: [{name, version}], ...}` | 排查上下文；`skills` 记录本次注入的 skill 清单 + 版本 |
| `environment` | `LANGFUSE_TRACING_ENVIRONMENT` | dev/staging/prod 隔离 |
| `release` | `LANGFUSE_RELEASE`（git commit / semver） | 发版维度对比 |
| `version` | 主/子 prompt 版本号 | 组件维度对比 |

### 2.6 大数据量处理（VFS 隔离，复用现有能力）

项目已有 `MessageSlimmerMiddleware`（>8000 字符结果截断 + 落盘 `vfs://` 路径）与 VFS 后端。Langfuse 侧**不需要额外改动**：Span 里存的已经是摘要 + 路径，天然避免 trace 体积爆炸、上报卡顿、费用虚高。与用户架构文档「链路只传路径」一致。

> 若需要把大结果也送到 Langfuse 分析：可在 span metadata 里附带 `vfs_path`，或按需异步转存 S3/MinIO 后在 Langfuse 通过 custom dashboard / blob export 关联（后续可选）。

### 2.7 敏感数据与成本控制

- **数据脱敏**：MCP 工具参数可能含库连接信息、SQL、业务数据。Langfuse 支持 masking（SDK `advanced-features`）；云平台 UI 可配置脱敏规则。SQL/业务数据在内部项目可接受，但**需明确：不含任何密钥/密码**（模型 key 走 env 不会进 trace）。
- **成本（Billable Units）**：云按观察数计费。若流量大，用 SDK `sampling`（按比例采样）控制成本；报表/报告生成类高价值请求可采样率更高。
- **不要记录**：`model_config` 里的 api_key 等，CallbackHandler 只记录模型名不记录 key，天然安全；人工 review 时注意 user 消息与 tool 参数里可能出现的敏感内容。

### 2.8 验证（上线后必做）

1. `assert_connected()`（`auth_check`）并入 `start_server.py` 预检。
2. 起服务，发一次「统计订单销售额」类真实查询。
3. Langfuse UI → Traces：确认 1 条 trace（或 2 条成组），含主 agent 的 generation/tool + 子 agent 的 SQL 生成/执行 generation/tool，模型名与 token 齐全。
4. 对照 Best Practices 页自查后闭环。

### 2.9 子 agent 各 Skill 中间结果跟踪

**能跟踪**。先说清现状（本次盘点结论）：

- **真正落盘的机制** = LLM 按 SKILL.md 指令用 `write_file`/`read_file` 工具读写 `/workspace/nl2sql_process_data/{thread_id}/{skill}/...`（如 `sql-of-thought` 读 `clarification/verdict.json`、`nl2sql-sql-generation` 读写 `sql.sql`、`nl2sql-knowledge-loader` 写 `knowledge.json`、错误写 `error.json`）。这是 agent **运行时跨 skill 数据总线**（中途要读回），Langfuse 是只读观测、**替代不了**，必须保留。
- **`SkillDataMiddleware` 这个 Python 类目前是死代码**：实例化于 `src/agent/graphs/nl2sql_agent.py`，但 `save_output/get_output/get_session_data` 等方法全项目**零调用方**；其 `wrap_tool_call`/`__call__` 是 `__init__` 里的**闭包**，从未挂到实例、框架看不到 → 运行时什么也不做。结论（已定）：**删除**该类（从中间件列表移除），Langfuse 接入时**新建轻量 `LangfuseSpanMiddleware`** 承载 ② 层；文件协议本身由 SKILL.md 继续驱动，无需代码层封装。
- **「VFS」是 deepagents `FilesystemBackend` 的虚拟路径空间，底层是真实磁盘目录**（非架构文档所述「纯内存虚拟文件」）。`/workspace/*` 由 `DynamicFilesystemBackend` 解析到活跃工作区磁盘，解析链：`workspaces.json` 的 `active` → 环境变量 `WORKSPACE_PATH` → 默认 `src/agent/workspace/`（见 `src/agent/workspace_manager/manager.py`）。**磁盘落盘 ⇒ 重启不丢、同机定时任务可直接读**（BadCase 归档不再需要「内存导出」）。

| 层 | 机制 | 说明 |
|----|------|------|
| ① 自动层（零成本） | `CallbackHandler` 自动捕获 | 每一步 LLM 调用（generation：推理/工具调用/每轮消息）+ 每个工具调用（tool：SQL/schema 检索/执行结果）全在 trace。中间过程 100% 可回溯，但按产物散落、未按 skill 分组 |
| ② 结构化层（按 skill 加 span） | `start_as_current_observation(as_type="span", name="skill_xxx")` + `span.update(input/output)` | 每个 skill 的关键中间产物显式记录：小数据（<8000 字符）直接存 span input/output、UI 直接看；大结果存 `vfs://` 路径（对齐「全链路埋点树」） |
| ③ 存量数据层（复用现有文件总线） | SKILL.md 驱动已落盘的 `{active_workspace}/nl2sql_process_data/{thread_id}/{skill}/` | 把文件路径挂到对应 span `metadata`（`vfs_path` + `workspace`）→ **Langfuse 看结构/摘要 + 磁盘文件看全量**，即架构文档 4.2 的关联方案 |

**实现要点**：
- skill 执行序列由 LLM 按 SKILL.md 自行决定，代码**无法确定性判断「当前在跑哪个 skill」** → 按**确定性的产物/工具边界**分段（`get_db_info`→schema 阶段、`run_sql`→生成/执行阶段、`recall_queries` 等），skill 名作 span `metadata` 标注（启发式，用于筛选而非切分）。
- 落点：在子 agent 中间件（**新建 `LangfuseSpanMiddleware`**，原 `SkillDataMiddleware` 删除）里，于关键工具调用处包 skill 级 span；span 结束时把 `{thread_id}/{skill}/` 下的小中间数据写入 span output / `vfs_path` 写入 metadata。
- 大工具结果（>16000 字符）由 `MessageSlimmerMiddleware` 落盘到 `{active_workspace}/large_tool_results/{tool_call_id}`，trace 里以 `vfs://large_tool_results/{tool_call_id}` 占位；span 结束按需把完整路径补进 metadata。
- 数据落点速查（`{active_workspace}` 见上）：skill 中间 `nl2sql_process_data/{thread_id}/{skill}/`、大工具结果 `large_tool_results/{tool_call_id}`、报告/图表 `report/`、临时 `tmp/`、语义库=工作区根、库配置 `db_config.json`。

> ✅ **② 结构化层实施验证记录（2026-08-23，M2 落地）**：
> - `client.start_observation(trace_context={...})` **只认 `trace_id`/`parent_span_id`**（langfuse 4.14.4 `client.py:713`），`session_id` 静默忽略 → 直接靠 trace_context 无法把 span 挂到 session。
> - **可行方案（已隔离验证）**：给 root span 的 OTel 属性设 `session.id` + `langfuse.trace.tags`（`span._otel_span.set_attribute(...)`），exporter 据此派生该 trace 的 session_id/tags；对已继承 session 的子 span 设置同名属性无害。
> - **span metadata 落点**：span 成为独立 root trace 时，`start_observation(metadata=...)` 传入的 dict 最终落在 **trace 级** metadata（span 观测级为空）；查询时按 trace 级读。
> - **ToolCall 是 TypedDict（dict）**（`langchain_core/messages/tool.py:206`）：取工具参数必须 `tc.get("args")`，`getattr` 恒 None → vfs_path 永远取不到（2026-08-23 修复 `_tool_args` 的 dict 兜底）。
> - **thread_id 取会话级来源**（优先级：`config.metadata.langfuse_session_id` → `configurable.trace_parent_thread_id` → `configurable.thread_id`）；`request.runtime.execution_info.thread_id` 在子 agent 上下文是**子线程** id，会拆 session。

### 2.10 skill 中间结果查看与排查手册

**三层通道（宏观→微观）**：

| 通道 | 看什么 | 何时用 |
|---|---|---|
| ① Langfuse UI | skill 级 span 的 input/output（小数据直接可见）+ metadata（`skill`、`vfs_path`、`workspace`） | 单次问题，点开即看结构 |
| ② Langfuse API/CLI | 按 `session_id=thread_id`/评分/报错批量筛 trace | 一批问题、统计性排查 |
| ③ 磁盘原始文件 | `{active_workspace}/nl2sql_process_data/{thread_id}/{skill}/...` 全量内容 | 精确定位根因、看大文件 |

① 是摘要层、③ 是最终真相（全量永远在磁盘，重启不丢、同机可读）。`nl2sql-correction` **不写文件**（纯对话上下文），排查看 trace 的 generation span。

**skill → 产物文件对照表**（`{active_workspace}/nl2sql_process_data/{thread_id}/` 下，源自 SKILL.md 扫描）：

| Skill | 写出的产物（排查点） | 依赖上游 |
|---|---|---|
| nl2sql-clarification | `clarification/verdict.json`、`context.json` | — |
| nl2sql-knowledge-loader | `knowledge-loader/knowledge.json` | — |
| nl2sql-schema-linking | `nl2sql-schema-linking/schema.json` | knowledge.json |
| nl2sql-subproblem | `nl2sql-subproblem/subproblem.json` | knowledge/schema |
| nl2sql-query-plan | `nl2sql-query-plan/query_plan.txt` | knowledge/schema/subproblem |
| nl2sql-sql-generation | `nl2sql-sql-generation/sql.sql` | knowledge/schema/subproblem/query_plan |
| nl2sql-performance-optimization | `nl2sql-performance-optimization/optimization.json` | sql.sql、schema.json |
| sql-of-thought（编排） | 不写自己的文件，串起上述产物 | verdict→sql→优化→执行 |
| 各 skill 失败时 | 约定统一写 `error.json`（多数 SKILL.md 已实现） | — |

**排查流程（症状→根因）**：
1. 拿 thread_id（前端会话 / 日志 / trace 链接）。
2. Langfuse 按 `session_id={thread_id}` 或 `trace_name=query:{thread_id}` 打开该 trace。
3. 看整体结构定位失败段：哪个 skill 的 span 报错/超时/重试、哪个工具（`dry_run`/`run_sql`/`get_db_info`）失败。
4. 按对照表去磁盘看完整产物：SQL 错看 `sql.sql` 原文 vs 执行报错；语义错看 `knowledge.json` 是否误导选表；澄清错看 `verdict.json`；优化劣化看 `optimization.json` 前后对比。
5. `error.json` 是失败现场，直接读比翻 LLM 输出快。

**配套约定（随 M2/M3 落地）**：
- span metadata 固定带 `skill` + `product_file`（该 skill 产物文件清单）——排查不记对照表，UI 里点开即拿路径。
- span 报错标级：`span.set_level(Level.ERROR)` + output 带 `error` 字段，错误 span 可筛。
- 新增 `scripts/inspect_thread.py --thread <tid>`：一条命令打印该线程产物文件树 + 关键文件内容，替代人肉 `find`；顺带集成 M3 BadCase 归档。

**数据生命周期（现状与推荐）**：现状 `nl2sql_process_data` / `large_tool_results` **无任何清理/TTL**（全项目仅死代码 `SkillDataMiddleware.clear_session`，已决定删除），按 thread_id 无限增长。推荐：
- **R0 保留期 TTL（推荐，随 M1 落地）**：`start_server.py` 启动钩子 + 每日任务扫描，删除超过 N 天的 `{thread_id}` 目录（N 可配，建议 7~30）。依据：Langfuse 只存路径+摘要，删全量文件不影响 trace 回溯；排查窗口期在保留期内够用。清理函数放 `WorkspaceManager`（如 `prune_workspace_data()`），**遍历 `workspaces.json` 全部工作区**，不只 active。
- **R1 会话删除联动**：前端删会话时同步删 `{thread_id}` 目录（当前 delete 仅清搜索索引 / fork 副本）。
- **R2 评分分级（M3 后）**：低分/差评（BadCase）thread 提升保留期或归档 `badcase/`，普通 thread 短保留。
- **R3 大结果冷化 + 配额护栏**：`large_tool_results` 设配额，超阈值先删最老，可选 gzip。
- 后台任务注意：长跑清理任务需进程外分离启动（用户终端 / PyCharm / Start-Process），避免随会话被杀。

---

## 3. 阶段二：评估（Evaluation）

> Langfuse 评估核心概念（离线/在线/批量）：[Evaluation Core Concepts](https://langfuse.com/docs/evaluation/core-concepts)

### 3.1 评分模型（五维评分 → Scores）

把用户架构文档的「五维自动评分」落到 Langfuse **Score**（`Numeric`，0-1 或 0-100）：

| 评分 | 含义 | 评估方式 | 挂载对象 |
|------|------|---------|---------|
| `schema_match_score` | Schema 选择正确性（选对表/字段） | Code evaluator（规则）+ LLM-as-a-Judge 采样 | 子 agent 的 schema span |
| `sql_valid_score` | SQL 合法性/安全性（拦截 DDL/全表/高危） | Code evaluator（确定性，复用现有 sql_approval 逻辑） | 子 agent 的 SQL 生成 span |
| `sql_biz_correct_score` | 业务语义正确性（结果是否符合提问意图） | LLM-as-a-Judge（核心） | 子 agent 执行 span |
| `report_table_score` | 报表/表格正确性 | LLM-as-a-Judge | 主 agent 报表 span |
| `analysis_report_score` | 分析报告质量 / 幻觉检测 | LLM-as-a-Judge | 主 agent 报告 span |

写入方式（在线，随运行打标）：

```python
# 方案示例（未实施）
from langfuse import get_client
client = get_client()
client.create_score(
    trace_id=trace_id,                 # 可精确到 span/observation_id
    name="sql_biz_correct_score",
    value=0.9,
    data_type="NUMERIC",
    comment="业务口径正确",
)
```

### 3.2 在线评估（Evaluators + Rules）

- **Code evaluators（确定性、零成本）**：SQL 语法校验、执行是否成功、是否全表扫描、返回行数合理性 → 命中即打低分。
- **LLM-as-a-Judge（语义质量，采样执行控成本）**：在 Langfuse UI 配置 evaluator（含 rubric），并配 **rule**（匹配线上 observation：按 db/tag/采样率），新数据自动评分。
- 全量规则校验 + 部分 LLM-Judge 采样 = 用户文档「全量规则校验+部分 LLM-Judge 采样校验」的直接落地。
- 评分结果进 [Score Analytics](https://langfuse.com/docs/evaluation/scores/score-analytics) 看趋势。

### 3.3 用户反馈（Human-in-the-loop）

- 前端加「满意 / 不满意 / 结果错误」按钮（对齐用户文档 BadCase 第 3 层）。
- 点击后调后端 `create_score(trace_id, name="user-feedback", value=0/1, data_type="NUMERIC", comment=...)`。
- 前端如何拿到 trace_id：主 agent run 响应里带上 `langfuse_trace_id`（后端生成后回传），或后端存 `thread_id → trace_id` 映射表。

### 3.4 离线评估（Datasets + Experiments）

对应「新版本批量跑 Dataset 回归实验」：

- **Dataset**：一组 `(input, expected_output)`（BadCase 库 + 标准问题集），用 `langfuse.create_dataset` / 添加 item 维护。
- **Experiment**（SDK，[Experiments via SDK](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk)）：

```python
# 方案示例（未实施）
from langfuse import Evaluation, RunnerContext

def task(item, **kwargs):
    """跑一条 Dataset item：调用 nl2sql 子 agent（或简化路径：直接 NL2SQL → 执行）"""
    return run_agent(item.input)          # 复用项目 agent 调用

def sql_biz_correct_evaluator(*, input, output, expected_output, **kwargs):
    return judge_semantic_correctness(input, output, expected_output)  # 规则或 LLM-Judge

with Evaluation(
    name="nl2sql-v1.2.0",
    dataset_id=...,          # 或 dataset_name
    task=task,
    evaluators=[sql_biz_correct_evaluator, sql_valid_evaluator],
) as experiment:
    pass
```

- **Experiments via UI** 可直接对「某 prompt 版本 × 某 dataset」跑并排对比（适合提示词快速迭代，无需写代码）。

### 3.5 BadCase 每日采集（定时任务）

对齐用户文档「每日自动 BadCase 采集机制」，实现为独立脚本 + 定时器：

```
每日 02:00 (cron/CronJob)
  → 调 Langfuse API 拉昨日 traces（public API / langfuse-cli / SDK）
  → 按条件筛选 BadCase：
      1) 系统异常（error status、工具报错）
      2) 五维分数低于阈值（<0.6~0.7）
      3) user-feedback = 0
      4) SQL 执行失败 / 超时 / 重试
  → 去重 → 写入 Dataset「production-badcase」
  → 按根因分类打 tag（schema 选错 / SQL 非法 / 口径错 / 报告幻觉）
```

> 依赖：需把复盘所需的大中间结果提前持久化（VFS 是进程内存，定时任务是独立进程读不到），用户文档已有此约束。

---

## 4. 阶段三：版本管理（Prompt Management）

> Langfuse Prompt Management：版本 + 标签（`production`/`staging`/`latest`）+ 一键回滚。[Prompt Version Control](https://langfuse.com/docs/prompt-management/features/prompt-version-control)

### 4.1 迁移范围

| 现有内容 | 来源 | Langfuse Prompt |
|---------|------|-----------------|
| 主 agent 系统提示词 | `src/agent/prompt/MAIN_AGENT_PROMPT.md`（含 `{{CHART_SPEC}}` 等占位符） | `main-agent-prompt`（chat 型，变量占位） |
| 子 agent 系统提示词 | `subagents/configs/nl2sql.yaml` 的 `system_prompt_file` | `nl2sql-agent-prompt` |
| 图表引擎规范 | `prompt/chart_specs/{engine}.md` | `chart-spec-semiotic` / `chart-spec-echarts` |
| 模型参数（temp/max_tokens） | `model_config.json`（前端 CRUD） | prompt `config` 字段（JSON）或保持现状 |
| **Agent Skill（SKILL.md 指令）** | `shared/skills/{main,nl2sql}/*/SKILL.md`（多文件资产） | 见 4.4「Skill 管理」：git 为主 + trace 版本标记（可选迁 Prompt 做指令灰度） |

**关键设计**：Langfuse prompt 保存**基础模板**；`dynamic_prompt` 中间件继续在运行时注入 db_name/通道路由等动态段（两者不冲突：静态模板 + 动态注入）。用 `.get_langchain_prompt()` 取字符串。

### 4.2 版本与标签（部署/回滚）

```python
# 方案示例（未实施）
client = get_client()
client.create_prompt(
    name="nl2sql-agent-prompt",
    prompt=prompt_text,            # 新版本
    labels=["production"],         # 部署 = 打 production 标签
)
# 读取
prompt = client.get_prompt("nl2sql-agent-prompt", label="production")
system_prompt = prompt.get_langchain_prompt()
```

- **发布**：新版本 UI/SDK 创建 → 打 `production` 标签生效。
- **回滚**：UI 把 `production` 标签改指上一版本（秒级回滚，无需发版）。
- **对比**：version diff 视图 + prompt config（模型参数）一并版本化。
- 可选增强：GitHub 集成（prompt 变更 webhook → CI）+ protected labels（防误改 production）。

### 4.2.1 Prompt 更新方式（现状 · 三种，2026-08-24 实况）

> M4/M6 落地后，prompt 的「改谁、怎么传、何时生效」共三条路，都基于同一条铁律：
> **后端启动时从 Langfuse 拉一次 `production`（失败回退本地），改完必须重启才生效。**

| 方式 | 源头 | 操作 | 生效 | 适用 |
|------|------|------|------|------|
| **① UI 直接更新**（推荐） | Langfuse UI | Prompts 页编辑 → 存新版本 → 设 `production` 标签 | 重启后 | 日常迭代、放量前人工评审 |
| **② 本地更新 + 上传** | 本地 md 文件 | 改 `src/agent/prompt/*.md` → `sync_prompts --all`（推新版本 + 自动打 `production`+`latest`） | 重启后 | 本地是唯一基线、走 git 评审 |
| **③ SDK/API 运维** | Langfuse 数据层 | `create_prompt`（建版/打标）/ `update_prompt_labels`（秒级切标签回滚）/ `sync_prompts --label staging`（A/B 灰度） | 重启后 | 回滚、灰度、脚本化发布 |

**三条路共通**：上传 ≠ 生效。①②③ 都只改 Langfuse 云端，运行中的服务要**重启**（启动时拉一次）才切到新版本；不重启则继续旧版（云端已新、进程仍旧）。验证：`logs\agent-server.log` 出现 `[langfuse] prompt main_system_prompt(label=production) v<N> 生效`。

**① UI 直接更新（改 Langfuse，推荐）**
1. Prompts → `main_system_prompt` → 编辑正文 → Save（生成新版本 vN+1）
2. 把新版本设 `production`（发布开关；`latest` 恒指最新）
3. 重启后端 → 日志 `vN+1 生效`
4. 回滚：旧版本设回 `production` + 重启（秒级，无需改代码）
> 注意：改 UI 不动本地 md，两边基线漂移；之后 `sync_prompts --all` 以本地为准，可能冲掉 UI 改动。

**② 本地更新 + 上传（本地为源头）**
1. 改 `src/agent/prompt/MAIN_AGENT_PROMPT.md` / `NL2SQL_SYSTEM_PROMPT.md`
2. `PYTHONPATH=src .venv/Scripts/python.exe -m agent.prompt.sync_prompts --all`
   - 对比云端 `production` 正文：内容一致 → 跳过（`--force` 才强制新版本）；有差异 → 推新版本并自动打 `production`+`latest`
   - 云端无该 prompt（首传）→ 直接建 v1 + `production`
3. 重启后端 → 日志 `vN+1 生效`
> 只改本地不跑 ② 就重启 → 拉到旧版或回退本地，云端不更新，基线漂移。
> 变体：`--name main_system_prompt`（单个）、`--skills`（顺带同步 15 个 SKILL.md）、`--label staging`（A/B 灰度，不碰 production）。

**③ SDK/API 运维（脚本化 / 回滚）**
- 建版本 + 打标：`create_prompt(name, prompt=..., labels=[...])`（sync_prompts 内部即此调用）
- 秒级回滚：`update_prompt_labels(name, version=旧版号, new_labels=['production'])`——labels 跨版本唯一，自动从新版移除 `production`，无需发版
- 灰度：`sync_prompts --label staging` 只打 `staging`+`latest`，配合 `LANGFUSE_CANARY_LABEL=staging` + `LANGFUSE_CANARY_RATIO` 按比例分流（M5 进程级机制）

### 4.3 版本语义（`release` vs `version`）

| 维度 | 值 | 用途 |
|------|-----|------|
| `release`（应用级） | `LANGFUSE_RELEASE` = git commit / semver | 对比发版前后成本/延迟/质量 |
| `version`（组件级） | prompt 版本号 / 模型配置版本号 | 定位「哪次 prompt/模型改动」导致指标变化 |

### 4.4 Skill（技能）资产的管理

**现状**：skill 是 Anthropic Agent Skills 格式的**多文件资产**——`shared/skills/{main,nl2sql}/*/` 目录含 `SKILL.md`（YAML frontmatter：name/description + markdown 指令）、`references/`（参考文档）、`scripts/`（可执行代码）。`SkillsMiddleware`（deepagents）解析 frontmatter 后把文档注入 system prompt。**当前无版本号/标签/灰度/回滚，仅 git 管理。**

**为什么不能像 prompt 一样整体迁入 Langfuse**：`scripts/` 是代码、`references/` 是多文件，Langfuse Prompt Management 面向单一文本 prompt。因此差异化处理：

| 维度 | 方案 |
|------|------|
| **监控** | 在 run 开始时把「本次注入的 skill 清单 + 各 skill 版本」写入 trace `metadata.skills`（见 2.5）。版本标识 = git commit + 每个 SKILL.md 文件 hash（或显式 `version:` 字段，可约定加入 frontmatter）。skill 实际被调用（触发 MCP 工具）在 tool 观察里已自动可见，两处结合可定位「这个 BadCase 是哪个 skill 版本产生的」。采集点：`SkillsMiddleware` 注入处（`skills_src.py` 的 `_list_skills`/注入逻辑，解析 frontmatter 后已有 name/path）或 `LangfuseContextMiddleware` |
| **版本管理** | **主方案（务实，推荐）**：skill 继续 git 管理，每次 run 用 `metadata.skills` + `release` 锁定 skill 版本，问题回溯按 git commit 定位。**增强方案**：仅把 `SKILL.md` 正文（纯指令段）迁入 Langfuse Prompt Management（`<skill-name>-skill`），scripts/references 留 git；`.get_langchain_prompt()` 读取注入，`production` 标签 + 秒级回滚 |
| **评估** | 对每个 skill 的产出打 skill 级 score（如 `sql-of-thought` 的 SQL 质量分、`chart-saver` 保存成功率），挂到对应 span；按 skill 版本聚合对比质量趋势（3.1 五维评分中 schema/sql 两维本质上在评 skill 的产出） |
| **灰度** | skill 指令 A/B：同一 skill 的 SKILL.md 两个版本打 `prod-a`/`prod-b` 标签（增强方案），按 user/比例路由；Langfuse 按版本聚合质量分/延迟/成本 |
| **发版** | skill 变更 = git tag + `LANGFUSE_RELEASE`；若用增强方案，SKILL.md 打 `production` 标签即发布、可一键回滚 |

**落地步骤（M4 内）**：
1. 约定：每个 SKILL.md frontmatter 增加 `version` 字段（或记录 git commit + hash）。
2. `LangfuseContextMiddleware` / skill 采集点读取 `shared/skills/` 目录，把 `{name, version, path}` 列表写入 trace metadata。
3. （可选增强）把主链路高频迭代的 SKILL.md 正文迁入 Langfuse prompt，保留 git 文件为 fallback，灰度验证通过后再切换。

> 关联：与现有 `ToolFilterMiddleware`（按请求裁剪工具）配合，metadata.skills 应记录**实际生效**的 skill 集而非全量目录。

**状态（M6，2026-08-23 已实施）**：上述「增强方案」已按 M6 落地——SKILL.md 正文同步为 Langfuse prompt `skill/{group}/{skill_dir}`（15 个全部 v1+production），trace `metadata.skills` 带云端解析版本 + `source` 标记，灰度/回滚复用 M5 的 label 机制。细节见 §8「M6 Skill 资产纳入 Langfuse 版本管理」。

---

## 5. 阶段四：灰度测试

### 5.1 Prompt A/B（canary）

Langfuse 官方 A/B 模式（[A/B Testing](https://langfuse.com/docs/prompt-management/features/a-b-testing)）：给两个 prompt 版本分别打 `prod-a`/`prod-b` 标签，应用按比例随机选一个：

```python
# 方案示例（未实施）
import random
prompt_a = client.get_prompt("nl2sql-agent-prompt", label="prod-a")
prompt_b = client.get_prompt("nl2sql-agent-prompt", label="prod-b")
selected = random.choices([prompt_a, prompt_b], weights=[0.9, 0.1])[0]  # 10% canary
```

Langfuse 自动按 prompt 版本聚合延迟/成本/质量分 → UI 对比两版本表现。每个 trace 的 generation 已自动关联所用 prompt 版本，无需额外埋点。

### 5.2 用户级 canary（user_id 路由）

- 按 `user_id`（或租户/tag）把指定用户路由到 `prod-b`：`if is_canary_user(user_id): label = "prod-b"`。
- 对比用 Score Analytics / Custom Dashboard 按 `user_id`、prompt 版本过滤。
- 对齐用户文档「版本灰度 → 全量发布」：小流量 → 指标达标 → 提升权重 → 全量。

### 5.3 环境隔离（Environments）

- `LANGFUSE_TRACING_ENVIRONMENT` 区分 `development` / `staging` / `production`（[Environments](https://langfuse.com/docs/observability/features/environments)）。
- 同一 project 内数据按环境过滤；dataset/prompt 可跨环境复用（staging 实验 → 达标 → production 打标）。
- 命名约束：小写字母/数字/`-`/`_`，≤40 字符，不能以 `langfuse` 开头。

### 5.4 模型级灰度（可选）

项目 `model_config_store` 已支持前端 CRUD 多模型。可结合 Langfuse：trace metadata 记录 `model`（CallbackHandler 自动带），用 Analytics 按模型对比质量/成本/延迟，作为模型灰度切换的量化依据（不必改 Langfuse，纯分析）。

---

## 6. 阶段五：发版与迭代闭环

### 6.1 release 标记

- 部署时注入 `LANGFUSE_RELEASE=$(git rev-parse --short HEAD)`（或 semver）。
- 发版对比：Traces 按 release 过滤，Analytics 对比成本/延迟/质量（[Releases & Versioning](https://langfuse.com/docs/observability/features/releases-and-versioning)）。

### 6.2 CI/CD 实验门禁（灰度/发版闸门）

[Experiments in CI/CD](https://langfuse.com/docs/evaluation/experiments/experiments-ci-cd)：在 CI 里跑实验，分数低于阈值用 `RegressionError` 阻断发版：

```python
# 方案示例（未实施）—— CI 脚本
from langfuse import Evaluation, RegressionError

THRESHOLD = 0.8
with Evaluation(name="nl2sql-regression", dataset_name="production-badcase",
                task=task, evaluators=[avg_sql_biz_correct]) as experiment:
    if experiment.average_scores["sql_biz_correct_score"] < THRESHOLD:
        raise RegressionError(f"sql_biz_correct {..} 低于阈值 {THRESHOLD}，阻断发版")
```

**完整发版流水线（SOP）**：

```
代码/提示词变更
  → 单元测试 + 本地验证
  → 跑 Dataset 回归实验（离线评估）           [质量闸门 1]
  → 通过 → 打 production 标签 / 提升 A/B 权重   [灰度]
  → 线上灰度观察（Scores/Tags/Release）         [质量闸门 2]
  → 通过 → 全量 + 打 LANGFUSE_RELEASE           [发布]
  → 每日 BadCase 采集 → 新样本入 Dataset         [闭环]
```

### 6.3 迭代闭环（对齐用户文档第 9 节）

用户文档的 SOP 与 Langfuse 能力一一映射：

| 用户文档步骤 | Langfuse 落地 |
|------------|--------------|
| 每日自动采集生产 BadCase | 3.5 定时任务 → Dataset |
| 人工补充标准答案、标记根因 | Dataset item 人工编辑 + 打 tag |
| 针对性迭代 Prompt/Schema/模板 | 4. 版本管理（新版本 + diff） |
| 新版本批量跑 Dataset 回归 | 3.4 Experiments |
| 指标提升 → 灰度 → 全量发布 | 5. 灰度 + 6. CI/CD 门禁 |
| 全程可观测、可对比、可复盘 | Trace + Scores + Release 全量留痕 |

---

## 7. 风险与注意事项

| 风险 | 说明 | 对策 |
|------|------|------|
| 异步子 agent trace 合并可靠性 | `start_async_task` 独立 run + 异步 contextvar 可能不跨 run 传递 | 先落地 `session_id=thread_id` 兜底；trace 合并作为增强单独验证（spike） |
| 云计费（Billable Units） | 全量埋点按观察计费 | sampling 控制；大结果已 VFS 化不膨胀 |
| 数据脱敏 | 工具参数/用户输入可能含敏感业务数据 | 明确「不入密钥」；UI 配置 masking；必要时过滤 tool 参数再上报 |
| 性能 | 埋点上传统一由 SDK 后台批量上报 | 不阻塞主链路；`flush()` 只在脚本/退出时调用 |
| 与自托管 v2 的关系 | 自托管 v2 不支持 Experiments/Releases/Agent Graph | 本方案统一用云 v4；连接说明文档保持背景参考 |
| `LANGFUSE_PROJECT` | `.env` 中的该变量非官方标准变量 | 忽略；标准变量为 PUBLIC/SECRET/BASE_URL |
| prompt 迁移回归 | 系统提示词从文件迁 Langfuse 后 dynamic_prompt 注入逻辑需保持 | 分步：先加 Langfuse 读取（回退到文件），验证无回归后再切换 |

---

## 8. 里程碑（分阶段落地建议）

| 里程碑 | 内容 | 验收 |
|--------|------|------|
| **M1 监控上线** | 公共模块 + 两 graph 接 `callbacks` + session 分组 + 预检 `auth_check` + 清理 SkillDataMiddleware | 真实查询出 1~2 条完整 trace，模型/token/工具齐全 |
| **M2 监控增强** | 请求级 trace 命名/session/user/tags/metadata（含 `metadata.skills` 注入清单+版本、`metadata.workspace`）；trace_id 合并子 trace；Agent Graph 视图正常 | 1 次查询 = 1 条 trace，子 agent 是 agent 节点，skill 版本可回溯 |
| **M3 评估上线** | 五维评分写入 + 在线 rules + 用户反馈按钮 + BadCase 采集脚本 | 线上 trace 带分；Dataset 有 BadCase |
| **M4 版本管理** | 主/子 prompt 迁 Langfuse + 标签 + 回滚演练；skill frontmatter `version` 约定 + trace metadata 记录（可选：高频 SKILL.md 迁 Langfuse） | 改 prompt 打 production 立即生效，可回滚；skill 版本可追溯 |
| **M5 灰度 + 发版闭环** | A/B 灰度 + CI/CD 实验门禁 + release 标记 + 每日采集 | 灰度→全量 SOP 跑通一轮 |
| **M6 Skill 资产版本管理** | 15 个 SKILL.md 同步为 `skill/*` prompt；trace `metadata.skills` 带云端版本 + `source` | Prompts 页 15 个 skill；主/子 trace skills 全 `source=langfuse` |
| **M7 反馈闭环** | BadCase 带 db_name + `run_experiment --from-badcase` 回灌 + `feedback_gate` 真实反馈门禁 + 每日 cron 报表 | 差评率骤降触发门禁 exit 1；badcase 集可离线回归 |

> 依赖链：M1 → M2 → M3 → M4 → M5 → M6 → M7 串行，各自独立可交付、可回滚。每个里程碑按「文件 → 步骤 → 验收 → 回滚」拆解如下。标 ⚑ 的为需 spike/联调确认项，非纯照搬。

### M1 监控上线

**前置**：`.env` 已配 `LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL`；`pip install langfuse langfuse-langchain`（版本按 skill 安装说明核对）。

**涉及文件**
- 新建 `src/agent/trace/langfuse_client.py`（公共模块；目录不存在则新建）
- 修改 `src/agent/main_agent.py`：主 graph `.with_config`
- 修改 `src/agent/graphs/nl2sql_agent.py`：子 graph `.with_config` + 移除 `SkillDataMiddleware` 实例化
- 删除 `src/agent/middlewares/skill_data.py`
- 修改 `start_server.py`：预检 `auth_check`

**实施步骤**
1. `langfuse_client.py` 提供 `get_langfuse_handler()`（返回 `CallbackHandler()`）、`auth_check()`（`get_client().auth_check()`）、`get_client()` 透传。参考附录 API 速查。
2. 主 graph：`.with_config({"recursion_limit": 500, "callbacks": [get_langfuse_handler()]})`。
3. 子 graph：同样并入 `callbacks`。
4. 删除 SkillDataMiddleware：从 `nl2sql_agent.py` 中间件列表移除实例，`grep -rn SkillDataMiddleware src` 确认无残留 import 后删文件。⚠ 文件协议本身由 SKILL.md 继续驱动，与代码无关，删除不影响运行时。
5. `start_server.py` 启动预检：`auth_check()` 失败则日志告警（不阻断，便于首次观察）。

**验收**
- 起服务，发一次「统计订单销售额」真实查询；Langfuse UI → Traces 出 1~2 条 trace，主/子 agent 的 generation（模型名、token）与 tool（SQL/schema 检索/执行）齐全。

> ✅ **M1 已实施并验证（2026-08-23）**：真实查询（imdb 电影数量）已出完整 trace 链——
> 主 trace `chat_agent`（12 观测）+ 子 trace `nl2sql_agent`（latency 69.9s、output 完整）
> + 工具 trace `wrenai_imdb_get_instructions/dry_run/dry_plan/run_sql` + 模型 trace `ChatDeepSeek`（`https://api.deepseek.com/v1`），全部 level=DEFAULT（成功）。
> 子 agent 模型路由修复：nl2sql_agent 补 `ThinkingToggleMiddleware`（否则前端切模型只对主 agent 生效，子 agent 仍打旧 provider 403 quota）。
> 查询 Langfuse API 用 `client.api.trace.*` / `client.api.observations.get_many`（时间参数传 datetime 对象）。

**回滚**：移除 `callbacks` 键、恢复 middleware 列表，删除 `langfuse_client.py` 引用即可；纯埋点无业务副作用。

### M2 监控增强

**前置**：M1 完成且 trace 正常。

**涉及文件**
- 修改 `src/agent/trace/langfuse_client.py`（+ 请求级属性封装）
- 修改请求入口（`custom_app.py` 或 run 处理层）：每请求 `propagate_attributes(...)`
- ⚑ 修改 `src/agent/middlewares/deepagents_async_config_patch.py`：configurable 透传增加 `langfuse_trace_id`
- 新建 `LangfuseSpanMiddleware`（替换已删的 `SkillDataMiddleware`，承载 ② 结构化层）
- 修改 `src/agent/graphs/nl2sql_agent.py`：中间件列表挂 `LangfuseSpanMiddleware`

**实施步骤**
1. 请求级属性：`propagate_attributes(trace_name=f"query:{thread_id}", session_id=thread_id, user_id=..., tags=["nl2sql"], metadata={"workspace": {"name": wm.active_name, "path": str(wm.active_workspace)}, "skills": skill_manifest})`。thread_id 从请求 config 取，workspace 经 `get_workspace_manager()`，skills 清单读 `src/agent/shared/skills/{main,nl2sql}/*/SKILL.md` 的 frontmatter（name+version）。
2. skill 清单注入时机：可在 SkillsMiddleware 注入 system prompt 处顺带构建 `skill_manifest`（名称+version），或启动时扫描一次缓存。记录「本次实际注入的 skill 及版本」到 metadata，便于回溯。
3. ⚑ 子 trace 合并：复用在 `deepagents_async_config_patch.py` 已注入 `trace_parent_thread_id` 的机制，加 `langfuse_trace_id` 转发；子 agent handler 用 `client.start_as_current_observation(as_type="span", name="nl2sql_agent", trace_context={"trace_id": X})`。**先落 `session_id=thread_id` 兜底**（父、子同 session），trace_id 合并做 spike 验证异步 contextvar 跨 run 传递后再启用。
4. `LangfuseSpanMiddleware`：按 2.9「实现要点」在关键工具边界包 skill 级 span；span 结束把小中间数据写入 span output / `vfs_path` 写入 metadata。
5. 验收前用 Langfuse UI 的 Agent Graph 视图核对父子关系。

**验收**：1 次查询 = 1 条 trace（或父子成组）；子 agent 为 agent 节点；metadata 含 workspace + skills(含版本)；`vfs_path` 可点回定位磁盘文件。

**回滚**：去掉 `propagate_attributes`、`langfuse_trace_id` 转发与 `LangfuseSpanMiddleware`，回 M1 状态；session 分组兜底在，不会丢关联。

> ✅ **M2 已实施并验证（2026-08-23，chinook_aliyun 实测）**：
> - **请求级属性注入**：`custom_app.py` user_middleware 纯 ASGI 注入 `config.metadata`（`langfuse_session_id`=主线程 thread_id、`workspace`、`skills` manifest），`runs.create` 不传 metadata 即生效（服务端注入，前端零改动）。
> - **session 分组**：主 trace `query:{tid}` + 子 trace `nl2sql-agent:{tid}` + skill span trace 全部归入同一 session（主线程会话），tags=`['nl2sql']`；skill span 经 `span._otel_span.set_attribute("session.id", tid)` 兜底分组（见 §2.9 验证记录）。
> - **skill manifest**：启动扫描 14 个 SKILL.md frontmatter（name+version+path），主/子 trace metadata 均含 `workspace` + `skills`。
> - **skill span**：`LangfuseSpanMiddleware` 挂 `nl2sql_agent` + `chat_agent`，关键工具边界包 `skill:{skill}:{tool}` span；`write_file`/`read_file` → **真实 `vfs_path`**（如 `/workspace/report/artist_count_direct.md`），查询类工具（run_sql/get_db_info）→ `vfs_dir` 兜底 + **真实 db_name**（读 ToolCall dict args）。
> - **实测副产物（已修复）**：monitoring 暴露子 agent 直查时工具传 db_name 串库（imdb/aix_report/Chinook_AutoIncrement）。根因：`DbConfigStore.get()` 精确大小写匹配，调用方传 `chinook_aliyun`（小写）≠ store 的 `Chinook_Aliyun` → KeyError → 落 .env 兜底报「未配置」→ LLM 自行探索其它库级联失败。修复（2026-08-23）：`store.get()` 与 `McpSqlConfig` 的 .env 兜底改为**精确匹配优先 + 忽略大小写兜底**，统一返回 canonical 配置；同时 `upsert()` 拒绝新增仅大小写不同的库名（防歧义/防重复条目），`delete()`/`set_wren_project()` 与 get 同口径大小写容错。验证：小写 `chinook_aliyun` 直跑 nl2sql_agent → 解析到阿里云 postgres 成功查询 artist=275，span 全部 db_name=chinook_aliyun、无串库；API 新增 case-duplicate 返回 400、正常新库名新增/删除正常。
> - **验证脚本**：`d:/tmp/lf_query.py`（按 session 查 traces+spans，带重试）、`d:/tmp/langfuse_m2_accept.py`（端到端验收）、`d:/tmp/lf_vfs_direct.py`（强制 write_file 验证 vfs_path）。服务器重启需 `$env:PYTHONUTF8='1'`（start_server.py 的 🚀 print 在重定向时 GBK 报错）。

### M3 评估上线

**前置**：M2 完成；五维评分口径在 UI 侧有参照 trace。

**涉及文件**
- 新建 `src/agent/eval/evaluators.py`（五维评分：code evaluator 规则 + LLM-as-a-Judge）
- 新建 `src/agent/eval/collect_badcase.py`（BadCase 采集）
- 修改 `src/agent/trace/langfuse_client.py`（+ `create_score` 封装）
- 修改 `custom_app.py`（+ 反馈写入端点，走 LANGGRAPH_HTTP 组合根）
- 修改前端 `harness-deep-agents-ui`（回答区 好评/差评 按钮）

**实施步骤**
1. 评分写入：在对应 span 结束时 `client.create_score(trace_id=..., name="sql_valid_score", value=0.0~1.0, data_type="NUMERIC", comment=...)`。五维：`schema_match_score`/`sql_valid_score`/`sql_biz_correct_score`/`report_table_score`/`analysis_report_score`，映射见 3.1 表。
2. `sql_valid_score` 确定性规则复用现有 sql_approval 拦截逻辑；其余三维（schema/biz_correct/report_table）用 LLM-as-a-Judge（先抽样，不全部实时跑，控成本）；`analysis_report_score` 幻觉检测按 3.x 方案。
3. 用户反馈：前端加 好评/差评 → `custom_app.py` 端点 → `create_score`（`data_type` 按官方建议，comment 记原文）。⚠ 端点新增走 LANGGRAPH_HTTP 组合根 `custom_app.py`（禁全局 JSON 中间件）。
4. BadCase 采集：`collect_badcase.py` 读 Langfuse traces（`npx langfuse-cli api` 或 SDK），筛 `sql_valid_score`/`sql_biz_correct_score` 低于阈值 或 用户差评 → 落 `Dataset`（badcase）待人工；全量文件经 workspace 磁盘补读。

**验收**：线上新 trace 带五维分（抽样项至少 1 维出现）；前端反馈按钮能打分并在 UI Scores 可见；`Dataset: badcase` 有条目。

**回滚**：停 evaluator 调用（在线打分开关）、前端按钮隐藏、端点移除；已打分数保留可清理。

> ✅ **M3 已实施并验证（2026-08-23，chinook_aliyun 实测）**：
> - **评分写入**：`LangfuseSpanMiddleware._maybe_score` 在 span 结束时写分到 `span.trace_id`。确定性三维（零成本、每查询必现）：`sql_valid_score`（复用 `sql_approval.classify_sql`：read=1.0 / full_dump=0.4 / write=0.0）、`schema_match_score`（code rule：发现成功=1.0 / 异常=0.3）、`sql_exec_success`（成功=1.0 / 失败=0.0，含「dbmcp_run_sql 把 DB 报错当返回值」的文本识别 `looks_like_exec_error`）。LLM-judge 三维（`NL2SQL_EVAL_JUDGE_SAMPLE` 采样，默认 0.3，后台 daemon 线程 `agent.eval.evaluators.schedule_judge`）：`sql_biz_correct_score`（子 agent 执行 span）、`report_table_score` + `analysis_report_score`（报表/报告产物 span，正文取 `write_file` 的 content 参数、仅 report/`.md`/`.txt` 文件打分）。
> - **用户反馈**：前端 👍/👎 按钮早已存在（`MessageFeedbackActions.tsx` → `PUT /api/threads/{tid}/messages/{mid}/feedback`），M3 只在 `put_feedback` 后台转发 `user-feedback` score（好评 1 / 差评 0，`_find_session_trace_id` 按 `session_id` 查主 trace `query:{tid}`），本地 store 为准、Langfuse 是旁路。已验证差评 → Langfuse UI Scores 可见。
> - **BadCase 采集**：`python -m agent.eval.collect_badcase --days N`（PYTHONPATH=src，脚本自载 .env）扫近 N 天 traces → 按「trace 异常 / 五维 <0.6 / user-feedback=0 / sql_exec_success=0」筛选 → 去重（本地 stamp `{workspace}/eval/badcase_collected.json`）→ 写 `Dataset:badcase`（`create_dataset_item` 带 `source_trace_id` 链回 trace）。已验证 2 条坏例入集。
> - **LLM-judge 假阴性坑（实测修复）**：问题说「artists 表」而物理表是 `artist`（单复数归一化），DeepSeek judge 曾据此判 0 分 → rubric 显式声明 NL 表名可归一化为物理名、不能仅因拼写判错、以「结果是否合理回答意图」为准；修复后正确查询 judge=1.0。
> - **抽样项实测**：纯计数查询出 `sql_biz_correct_score`（0.9~1.0）；含报表写入的查询出 `report_table_score` + `analysis_report_score`（0.8~1.0）。`report_table/analysis_report` 挂到**实际写报表的 agent 的 trace**（异步委派架构下是子 agent，非方案 3.1 表的「主 agent 报表 span」——实现以实测为准）。
> - 已验证：`.env` 采样开关 `NL2SQL_EVAL_JUDGE_SAMPLE=1.0` 强制全量 judge（验收用）；生产默认 0.3 控成本。

### M4 版本管理

**前置**：M2 的 skill manifest 已能读出 version。

**涉及文件**
- 修改 `src/agent/llms/` 或 prompt 装配处：prompt 改从 Langfuse 拉取
- 修改 `src/agent/shared/skills/*/SKILL.md`：frontmatter 增加 `version`（约定式）
- ⚑ 可选：高频 SKILL.md 正文迁 Langfuse prompt

**实施步骤**
1. 主 agent / 子 agent 的 system prompt 迁移：`client.create_prompt(name="main_system_prompt", prompt=..., labels=["production"])`；运行处 `client.get_prompt(name, label="production").get_langchain_prompt()` 装配。
2. 回滚演练：改 prompt → 打 `production` 标签 → 立即生效验证；再切回旧版本标签。
3. skill 版本约定：`SKILL.md` frontmatter `version: 0.1.0`；升级即改号，trace metadata.skills 天然记录当时版本（M2 已注入）。
4. ⚑ 高频 SKILL.md（如 sql-of-thought）迁 Langfuse prompt 需权衡：文件仍在磁盘（运行时数据总线），Langfuse 仅存正文版本供 diff/回滚 —— 需跑一次「从 Langfuse 拉正文注入」的 spike 验证 SKILL.md 引用路径不破坏。

**验收**：改 prompt 打 production 即时生效且可回滚；`metadata.skills` 记录版本，历史 trace 可区分。

**回滚**：恢复本地 prompt 装配、去掉 `get_prompt` 调用；`production` 标签随时可切回旧版本。

> ✅ **M4 已实施并验证（2026-08-23，chinook_aliyun 实测）**：
> - **prompt 迁移**：主/子 system prompt 改从 Langfuse 拉取。`langfuse_client` 新增 `get_prompt_text`（拉 `production` 正文，3s 超时 + 失败回退本地文件）、`create_prompt`（上传）、`update_prompt_labels`（切标签回滚）。装配处 `main_agent._build_system_prompt` / `nl2sql_agent` 均「Langfuse 优先、失败回退本地」；`{{CHART_SPEC}}` 等占位符在 Langfuse 正文里原样保留，本地替换逻辑不变。开关 `LANGFUSE_PROMPT_ENABLED`（默认 1；0/false 强制本地，回滚第二手段）。
> - **同步脚本**：`python -m agent.prompt.sync_prompts --all`（PYTHONPATH=src，自载 .env）把本地 `MAIN_AGENT_PROMPT.md` / `NL2SQL_SYSTEM_PROMPT.md` 上传为 `main_system_prompt` / `nl2sql_system_prompt`（production+latest 标签），内容未变自动跳过（防版本堆积）；`--label staging --force` 供 M5 A/B 分流。
> - **回滚演练（全闭环实测）**：基线 v1 → 造含 marker 的 v2 打 production → 重启 → 启动日志 `main_system_prompt v2 生效`（production 服务 2401 字符含 marker）→ `update_prompt_labels('main_system_prompt', version=1, new_labels=['production'])` 切回 → 重启 → 日志 `v1 生效`，get_prompt 确认 production 不含 marker。两手段：标签切换（SDK `update_prompt`，labels 跨版本唯一、自动移除新版本上的 production）+ env 强制本地。
> - **skill 版本约定**：14 个 SKILL.md frontmatter 统一加 `version: 0.1.0`；`skill_manifest` 读出 → 实测新 trace `metadata.skills` 14 项全带 version，历史 trace 可区分。
> - **坑（实测）**：① deepagents 栈下 Langfuse 主 trace 的 `input` 只含用户消息、model CHAIN 观测 input 为空 —— system prompt 正文不进 trace，生效证据用「启动日志版本号 + get_prompt 内容」（§4.3 组件级 version 口径），非 trace 内容；② Langfuse SDK 无删 prompt 版本 API，演练残留的 v2（label=latest，含 marker）不对外服务、无影响；③ sync/查询脚本在 Windows GBK 控制台须 `sys.stdout.reconfigure(encoding='utf-8')` 防 ⚠ 等符号炸编码；④ 同步/回滚脚本均需 `PYTHONPATH=src` + 自载 .env（与 collect_badcase 同构）。

### M5 灰度 + 发版闭环

**前置**：M3（有分可判）+ M4（prompt 可版本化）就绪。

**涉及文件**
- 修改 prompt 装配处：A/B 分流（`prod-a`/`prod-b` 标签）
- 新建 CI 脚本 `scripts/run_experiment.py`：`Evaluation` + `RegressionError` 门禁
- 修改发布流程：`LANGFUSE_RELEASE` + trace metadata `version`
- 新建每日采集 cron 配置（进程外，见记忆：长跑用用户终端/PyCharm 或 Start-Process 分离启动）

**实施步骤**
1. A/B：同一 prompt 两个版本打 `prod-a`/`prod-b` 标签，流量按比例取标签拉取（先 1%/5% 小流量）。
2. 灰度判据：用 `Dataset`（badcase 集）跑 `Evaluation`，比较 A/B 在五维分上的表现；`RegressionError` 触发则回滚到 `prod-a`。
3. release 标记：发布时设置环境 `LANGFUSE_RELEASE`（如 `2026.08.23`），trace metadata 记 `version`；UI Releases 按环境/版本分组查看。
4. 每日采集：cron 跑 `collect_badcase.py` 沉淀 Dataset + 生成当日报告；进程外分离启动避免随会话被杀。

**验收**：小流量灰度一轮：A→B 切换可见 trace 分组；回归超阈自动回滚；Release 页按版本查看一周数据；每日 BadCase 报告产出。

**回滚**：分流开关回 `prod-a` 即全局旧版；`RegressionError` 自动回滚是最后防线。

> ### M5 已实施（2026-08-23，chinook_aliyun 实测）
> - **A/B 分流（进程级）**：`langfuse_client.resolve_prompt_label()` 三级解析——显式 `LANGFUSE_PROMPT_LABEL`（run_experiment/灰度演练直接用）→ `LANGFUSE_CANARY_LABEL`+`LANGFUSE_CANARY_RATIO` import 时掷骰（约 ratio*N 实例走 canary）→ 默认 `production`；import 时只掷一次（`_CANARY_RESOLVED` 缓存）。装配处 main_agent / nl2sql_agent 的 `get_prompt_text` 不再显式传 label，缺省走分流；拉到的版本记入 `_PROMPT_VERSIONS[label]`。
> - **trace 分组可见**：`LangfuseMetadataMiddleware` 注入 `metadata.prompt = {prompt_label, prompt_version}`（A→B 切换在 trace 上直接可见分组）+ `metadata.langfuse_release`（LANGFUSE_RELEASE）。
> - **release 标记**：`get_client()` 惰性创建时 `client._release = LANGFUSE_RELEASE`（4.14.4 CallbackHandler 不读 metadata 的 release 键，但 trace 创建统一取 `client._release` → Release 页按版本分组）。`.env` 已加 `LANGFUSE_RELEASE="v0.5.0-m5"` + 注释好的 canary 开关组。
> - **离线 A/B 门禁 `python -m agent.eval.run_experiment`**（src/agent/eval/run_experiment.py）：orchestrator 按 label spawn worker 子进程（`LANGFUSE_PROMPT_LABEL` import 前注入 → 进程级 prompt 生效），worker **顶层 import** graph（MCP 在 asyncio 外加载，同生产启动路径——这是进程内 ainvoke 的关键，探针 d:/tmp/lf_m5_invoke_probe.py 实测 `Cannot run the event loop` 的坑），逐条 ainvoke 抽 run_sql + 结果，算确定性三维（sql_valid/sql_exec_success/schema_match，复用 M3 evaluators）+ 可选 `--judge` LLM-judge（sql_biz_correct），JSONL 落盘；orchestrator 聚合均值 + 无SQL/执行失败比例 → 核心维 candidate 均值 < reference−threshold 即 `RegressionError`（exit 1），manifest 落 `{active_workspace}/eval/experiment_runs/`。实测：worker 单 label 2 查询全 1.0 分（含 judge）；orchestrator `production vs prod-a`（同内容）门禁通过。
> - **实验 trace 隔离**：worker ainvoke 带 `langfuse_tags=["nl2sql","experiment"]` + session `exp:{label}:{run}`，Langfuse 上 tag=experiment 可过滤，不污染生产会话。
> - **每日采集 cron（进程外）**：Windows 任务计划 `nl2sql-collect-badcase`（每日 02:13，`scripts/daily_collect_badcase.ps1` 分离启动 `collect_badcase --days 1`，日志追加 `server_collect_badcase.log`；注册脚本 `scripts/setup_collect_badcase_task.ps1`）。
> - **HTTP-run 线上验证（2026-08-23，服务器实测）**：经 `LangfuseMetadataMiddleware` 走真实 HTTP run（`chat_agent`）后，Langfuse 上主 trace `query:{tid}` 与子 trace `nl2sql-agent:{tid}`、skill span `skill:schema-linking`/`skill:sql-execution` 全部同 session、同 `release=v0.5.0-m5`、`tags=['nl2sql']`，且 `metadata.prompt={'prompt_label':'production','prompt_version':1}` 已注入（A→B 切换可见分组）。子任务异步执行完成，答案正确（chinook_aliyun artist=275）。
> - **坑（实测）**：① 进程内 invoke 必须顶层 import graph，否则 MCP `_load_mcp_servers` 自建 loop 撞上外层 loop 报 `Cannot run the event loop while another loop is running`，工具 0 个；② A/B label 不存在时 `get_prompt` 404 → 自动回退本地（与 production 同内容），不会崩；③ sync_prompts 的跳过逻辑只比 production，mint 新 label 需 `--force`；④ 评审模型在同一供应商下创建，worker 内 `NL2SQL_EVAL_JUDGE_SAMPLE=1.0` 强制全量；⑤ **`.langgraph_api/.langgraph_ops.pckl` 持久化僵尸 runs**——inmem runtime 的 `GlobalStore` 把 `runs`（含 status）落盘，客户端中断（499）的 run 及其 deepagents 子 run 会以 `status="running"` 永久留在 pckl，重启后 `Queue stats` 恒显示 `n_running=N / n_pending=0 / active=0`（worker 全空但队列像满），新 run 永远不调度。清除方法：停服 → 备份删除 `.langgraph_api/.langgraph_ops.pckl`（+`.langgraph_retry_counter.pckl`）→ 重启即 `n_running=0`（线程 checkpoint 走自定义 checkpointer 独立落盘，不受影响）。

### M6 Skill 资产纳入 Langfuse 版本管理

**前置**：M4（prompt 可版本化）+ M5（A/B 分流 / release 机制）就绪，复用同一套 label/回滚机制。

**为什么可行**：SKILL.md 是单文件纯指令（`references/`/`scripts/` 仍是多文件资产留在 git），正文可整体作为 Langfuse text prompt——一个 SKILL.md = 一个 `skill/{group}/{skill_dir}` prompt，即可获得版本/标签/一键回滚（官方「skill 管理」实践：一条 skill = 一个 prompt，正文存全文）。

**涉及文件**
- `src/agent/trace/langfuse_client.py`：新增 `get_prompt_version(name, label=None)`——拉 skill prompt 版本号；label 缺省走 M5 分流；失败/404/未启用 → None。
- `src/agent/trace/skill_manifest.py`：新增 `get_enriched_skill_manifest()`（缓存）——本地 manifest + 每 skill 解析 Langfuse 版本 → 条目带 `version` + `source`（`langfuse`=云端生效 / `local`=未同步或拉取失败，version 取本地 frontmatter 兜底）。`get_skill_manifest()` 保留不变。
- `src/api/langfuse_metadata.py`（主 trace）+ `src/agent/middlewares/deepagents_async_config_patch.py`（子 trace）：`metadata.skills` 改用 enriched manifest，主/子一致。
- `src/agent/prompt/sync_prompts.py`：新增 `--skills` 模式——扫 `shared_skills_dir` 下每个 SKILL.md → prompt `skill/{entry['path']}`；默认 `production+latest`；跳过逻辑与 system prompt 相同（内容与 production 一致则跳过，`--force` 强制新版本）。

**实施步骤**
1. 同步：`python -m agent.prompt.sync_prompts --skills`（PYTHONPATH=src，**项目 venv 解释器** `.venv/Scripts/python.exe`）。
2. 生效：重启服务（中间件首建 enriched manifest 时逐 skill 拉版本，SDK 缓存 60s）。
3. 灰度/回滚：改 skill 指令 = Langfuse UI 编辑对应 `skill/*` prompt → 打新版本 production 即生效；回滚 = `update_prompt_labels(name, version=旧版, new_labels=['production'])` 秒级切回；A/B = `--label staging`/`prod-a` 分流（与 system prompt 同 label，M5 进程级机制复用）。
4. 事后回溯：trace `metadata.skills` 记录了「本次 run 命中的 skill 版本组合」（15 条 `{name, path, version, source}`），配合 `release` 可重建任意一次运行的 skill 集。

**验收**：Langfuse Prompts 页 15 个 `skill/*` v1；真实查询后主/子 trace `metadata.skills` 15 条全 `source=langfuse, version=1`。

**回滚**：本地磁盘仍是运行时源（deepagents `FilesystemBackend` 读本地 SKILL.md），Langfuse 是管理/审计/灰度层；整链关闭走 `LANGFUSE_PROMPT_ENABLED=0`（`get_prompt_version` 返回 None → 全 `source=local`，运行时不受影响）。

> ### M6 已实施（2026-08-23，chinook_aliyun 实测）
> - **同步**：`sync_prompts --skills` 扫描 15 个 SKILL.md → `skill/{group}/{skill_dir}` 全部 v1、`labels=['latest','production']`；重跑幂等（内容一致自动跳过，`--force` 才强制新版本）。
> - **trace 元数据**：`LangfuseMetadataMiddleware`（主）与 `deepagents_async_config_patch`（子）一致改用 `get_enriched_skill_manifest()`。实测主 trace `query:{tid}` 与子 trace `nl2sql-agent:{tid}` 的 `metadata.skills` 均 15 条 `source=langfuse, version=1`（如 skills[0]={name:'alibabacloud-find-skills', path:'main/alibabacloud-find-skills', version:1, source:'langfuse'}）；`release`/`metadata.prompt` 不受影响。
> - **版本追踪**：`get_prompt_version(name, label=None)`——label 缺省走 M5 进程级 A/B 分流（与 system prompt 同 label，A→B 切换一致）；失败/404/未启用 → None → 该 skill 回退 `source=local`（version 取本地 frontmatter），运行时仍读本地磁盘，不阻塞。
> - **坑（实测）**：① 运行时 skill 加载仍走本地磁盘（deepagents `FilesystemBackend`），Langfuse 是管理/审计/灰度层——UI 改 `skill/*` prompt 后须保持本地文件同步，否则 trace 记录云端版本、实际执行用本地正文（两处不一致）；② 共享 skills 目录会动态新增 skill（如 2026-08-23 新增 `main/langfuse`），重跑 `--skills` 只补齐新项（其余内容一致自动跳过）；③ 同步脚本须用项目 venv 解释器 `.venv/Scripts/python.exe`——PATH 上的系统 python 会命中用户 site-packages 的旧 langfuse 2.x（`from langfuse import get_client` 报 `cannot import name`）；④ skill span（`skill:schema-linking` 等工具观察）不注入 metadata.skills，为既有已知限制（主/子 trace 已够回溯）。

### M7 反馈闭环：真实反馈门禁 + BadCase 回灌

**前置**：M3（五维评分 + `user-feedback` score 已接入）、M5（A/B 分流，trace `metadata.prompt.prompt_label` 记录分组）、M6（Dataset:badcase 已沉淀）。M7 把「前端反馈 → Langfuse 数据」**反向喂回**灰度放量/发版判断，补全 §6.3 迭代闭环的最后两段：

```
前端 👍/👎/评论 → Langfuse user-feedback score ─→ feedback_gate 真实反馈门禁（放量/回滚判断）
                      ↘ 差评/低分 → collect_badcase → Dataset:badcase ─→ run_experiment --from-badcase（离线回归）
```

**涉及文件**
- `src/agent/eval/collect_badcase.py`：BadCase item metadata 加 `db_name`（源：`trace.metadata.db_name`）——回灌时定位同一数据库。
- `src/agent/eval/run_experiment.py`：新增 `--from-badcase`（+`--from-badcase-limit`）——查询集改从 Dataset:badcase 装载（`api.dataset_items.list` 分页，按 `(question, db_name)` 去重，可 `--queries` 合并），随后走既有 orchestrator 回归门禁。
- `src/agent/eval/feedback_gate.py`（新建）：真实反馈门禁——`api.scores.get_many(name="user-feedback", from_timestamp=...)` 分页拉评分 → 按 trace 取最新去重 → `api.trace.get` 读 `metadata.prompt.prompt_label` 分组 → 聚合好评率 → candidate < reference − threshold 即 exit 1。
- `scripts/daily_collect_badcase.ps1`：每日 02:13 cron 在 `collect_badcase` 之后追加一次 `feedback_gate --days 7`（报表 + 放量判断落日志）。

**实施步骤**
1. `collect_badcase --days N [--force]`：新采集 BadCase 带 `db_name`（历史 item 缺省，回灌时按默认库兜底）。
2. `run_experiment --from-badcase --labels prod-a prod-b`：把 Dataset:badcase 全部已采集问题回灌到离线 A/B，让「线上暴露的问题」进入回归集（gap ⑥）。
3. `feedback_gate --days 7 --ref production --cand prod-a [--threshold 0.05 --min-rated 5]`：按 prompt_label 分组比较真实好评率（gap ④）；`--report-only` 只看报表不门禁；`--fail-insufficient` 数据不足时 exit 2 而非跳过。
4. 每日 cron：`collect_badcase` + `feedback_gate --days 7` 顺序执行，manifest 落 `{active_workspace}/eval/feedback_gates/`。

**验收**：`feedback_gate` 输出各组好评率表 + 最近差评明细 + manifest；坏版本上线时（好评率骤降）门禁 exit 1 触发回滚；`--from-badcase` 能跑起与 `--queries` 同构的回归。

**回滚**：`feedback_gate` 是只读旁路（不写 Langfuse 数据），直接不跑即回到原流程；`--from-badcase` 回 `--queries` 即可。

> ### M7 已实施（2026-08-23，chinook_aliyun 实测）
> - **collect_badcase 带 db_name**：`trace.metadata.db_name`（如 `chinook_aliyun`）实测存在 → item metadata 落 `db_name`；`--force` 重扫 30 天验证 2 条新 item 均带 db_name（旧 2 条缺省）。
> - **run_experiment --from-badcase**：`_load_badcase_queries` 从 `api.dataset_items.list(dataset_name='badcase')` 分页装载，按 `(question, db_name)` 去重（4 item → 2 查询），带 `source_trace_id` 链回线上 trace；可与 `--queries` 合并去重；空查询集 exit 2。
> - **feedback_gate**：30 天窗口实测 4 条 user-feedback score → 去重 3（同 trace 双好评合并）→ 按 `prompt_label` 分组 production=1（100%）；M5 metadata 中间件上线前的 2 条旧差评 trace 归类 unknown（报表单列，不参与门禁）。门禁退出码实测：数据不足默认 exit 0 跳过、`--fail-insufficient` exit 2、`--report-only` 恒 0。manifest 落 `{active_workspace}/eval/feedback_gates/feedback_gate_{stamp}.json`。
> - **坑（实测）**：① score 同一 trace 可能多次打分（点赞后又存评论各写一条）→ 必须按 trace 取最新去重，否则好评率重复计数；② `scores.get_many` 单页上限 100、返回按时间序，分页循环至 <100 退出；③ 无 `metadata.prompt` 的旧 trace（M5 前）归类 unknown——放量/回滚判断只看带分组的新流量，符合预期；④ 反馈数据量天然稀疏（需真实用户点击），`min_rated` 门槛保证样本不足时不被误判。

**P0 状态标记（2026-08-24 已实施）**：闭环最后一公里——badcase 从「只采不管」到「采集→复审→修复→回归验证」。

- **问题**：Langfuse Dataset API 是 append-only（无 update/delete），采集后无生命周期管理。修复 bug 后无法确认 badcase 是否消失，也无法从回归集剔除已关闭 case。
- **方案**：本地 JSON 状态文件 `{workspace}/eval/badcase_status.json`，以 `source_trace_id` 为主键，状态 `pending`→`reviewed`→`fixed`/`invalid`。`fixed`/`invalid` 视为已关闭，默认不进回归集。
- **新增文件**：`src/agent/eval/badcase_status.py`（状态存储 + CLI `mark/list/summary`，支持 trace_id 前缀匹配）。
- **改动**：`collect_badcase.py` 新采集自动注册 `pending`（已有状态不覆盖）；`run_experiment.py` 新增 `--badcase-status` 参数（默认 `pending,reviewed`，传 `all` 包含全部），`_load_badcase_queries` 按状态跳过已关闭 item 并输出跳过数。
- **用法**：
  ```bash
  # 复审后标记为已修复
  python -m agent.eval.badcase_status mark 707b335d fixed --note "修正了 JOIN 语法"
  # 查看状态分布
  python -m agent.eval.badcase_status summary
  # 回归测试只跑开放状态的 badcase
  python -m agent.eval.run_experiment --from-badcase --labels prod-a
  ```

---

### M8 v4 平台迁移（events_only 读兼容，2026-08-24 已实施）

**背景**：自托管 Langfuse 升到 v4 平台后，写模式默认 `events_only`——新的 trace/observation/score 全部落 events 新表（ClickHouse `events_core`/`events_full`），legacy 读接口（`GET /api/public/traces`、`/api/public/scores`、`/api/public/v2/scores`）读不到这些数据（events_only 下直接 404；dual 下返回 200 但空）。用户反馈可见性、feedback_gate 门禁、collect_badcase 采集全部失效——这是 8-24 之后「用户反馈在 Langfuse 看不到」的根因（前置 `trace.list` 404 → 从未调用 create_score；读侧 scores.get_many 读空）。

**平台侧**（无源码修改，官方迁移变量）：服务器 `/opt/langfuse/.env` 追加 `LANGFUSE_MIGRATION_V4_WRITE_MODE=dual`（+ `LANGFUSE_MIGRATION_V4_ALLOW_PREVIEW_OPT_IN=false`、`LANGFUSE_BACKGROUND_MIGRATION_V4_ENABLE_HISTORIC_BACKFILL=false`），docker-compose 对应 NODE_OPTIONS 行下发同名 env；`docker compose up -d` 重启。dual = 新旧表都写，legacy 读接口恢复 200（但历史 events 数据仍需 v4 读）；生产可切回 `events_only`（只写 events 新表）+ 全部走 v4 读接口。参考官方 v3→v4 升级指南。

**应用侧**：新增 `src/agent/trace/langfuse_v4_reads.py`，把 v4 原生读接口收敛成原 legacy 读接口的同名功能，三个调用点无痛替换：

| 原 legacy 读（events_only 失效） | v4 原生读（已实施） |
|---|---|
| `trace.list(session_id=...)` 定位反馈目标 trace | `observations.get_many`（`v2/observations`）filter `sessionId=` + `isRootObservation=true`，AGENT 根 `chat_agent` 取最新 → `find_session_main_trace_id()` |
| `trace.get(tid)` 读 `metadata.prompt.prompt_label` 分组 | `observations` filter `traceId=` 取 root 的 metadata → `get_trace_metadata()` + `metadata_prompt_label()` |
| `trace.list(tags="nl2sql")` 列用户 trace | `observations` filter `type=AGENT` + `isRootObservation=true`（`fromStartTime` 分页）→ `list_user_traces()` |
| `trace.get(tid)` 读五维分 | `scores_v3.get_many_v3(trace_id=...)` 按会话全部 root trace_id 逐个汇总 → `session_scores_map()` |
| `scores.get_many(name='user-feedback')` | `scores_v3.get_many_v3(name=..., from_timestamp=...)` 游标分页 → `list_scores()` |
| `trace.get(tid)` 读 input 提用户问题 | root observation 的 `input` → `extract_question_from_input()`（取最后一条非系统 human） |

**v4 数据形态要点（实测 8-24 数据）**
- 主 trace 名不是 `query:{tid}`：deepagents 栈里实际是 AGENT 根 observation `name=chat_agent`（子 `nl2sql_agent`），`langfuse_trace_name` metadata 未生效；**sessionId=thread_id 正确落库**，按 sessionId 定位即可。
- 一个会话（thread）是**多条扁平独立 trace**：chat_agent 主（多次 run）、nl2sql_agent 子、skill 工具各一条；五维分挂在工具执行 trace（`subject.kind=trace/observation`），主 chat_agent trace 通常无分——打分归属必须按会话汇总。
- root observation 的 `metadata` 落 trace 级业务元数据（prompt/workspace/skills/db_name/thread_id）；`prompt` 值被字符串化成 `"{'prompt_label': ...}"`，需 `ast.literal_eval`/`json.loads` 还原。
- v3 score 的 trace 归属在 `subject`（`kind=trace → subject.id`；`kind=observation → subject.trace_id`），不在顶层 `trace_id`。
- 最新一次 run 的主 trace input 常是 auto-continue 系统通知，用户原始问题在更早 run 的 input 里（`session_question` 遍历 agent root 取第一个命中）。

**验证（8-24 实测）**
- 写路径闭环：`find_session_main_trace_id` → `create_score(name='v4-migration-test', trace_id=...)` → `list_scores` 读回 1 条（落 events 新表；legacy 旧表 0 条）——用户反馈写路径已恢复。
- `session_scores_map` 汇总 4 个五维分（schema_match/sql_valid/sql_biz_correct/sql_exec_success 均 1.0）；`session_question` 正确提取「观看次数最多的电影」；`list_user_traces` 归组 4 个会话。

**坑（实测）**
- `observations` filter 里 `sessionId` 是**精确匹配**，传截断 UUID 返回 0；`isRootObservation` boolean 用 `value: true`。
- `scores_v3.get_many_v3(session_id=...)` 不匹配 trace/observation 挂的 score，五维分必须按 trace_id 逐个汇总。
- `dataset_items.list` 报 `Dataset not found` 是**业务 404**（badcase 数据集未建），非 API 不可用；collect_badcase 首次运行会 `create_dataset` 自动建。

**撤销 / 改评语义（v4 无 score 删除 API，软删除哨兵）**
- **结论**：v4 events 表**没有** score 删除接口——公开 API 只有 legacy `DELETE /api/public/scores/{id}`，实测只删 legacy 表、删不到 events 数据（202 入队后 `v3/scores` 仍返回该 score）；CLI（最新 SDK）同样只有一个 legacy delete。events 数据进 ClickHouse `events_core`/`events_full`，只有保留策略（batch-data-retention-cleaner）会清，彻底清理只能服务器运维层直接 `ALTER TABLE ... DELETE`。
- **软删除方案**：撤销反馈时写一条 `user-feedback` **哨兵分 `value=-1.0`**（`USER_FEEDBACK_REVOKED`，comment「已撤销」，metadata `message_feedback=revoked`）。读取端约定「**最新一条 user-feedback 即当前状态**」：-1=已撤销（忽略）、0=差评、1=好评。撤销后残留的旧点赞分被哨兵覆盖，不会污染好评率。
- **v3/scores 返回顺序**：实测按 timestamp **倒序**（最新在前）。所有按会话汇总处必须显式取最新，不能靠 dict 覆盖（会取到最旧）——`session_scores_map` 已按 name 取最新 timestamp。
- **三个改动点**（均已实施）：`delete_feedback` 撤销时写哨兵（`_schedule_langfuse_revoke`）；`feedback_gate._dedupe_scores` 取最新且 `value<0` 视为无反馈剔除；`session_scores_map` 按 name 取最新。
- **已知残留**：Langfuse UI 的 Scores 面板会看到 value=-1 的「已撤销」记录（comment 可读）。前端反馈图标回显走本地 store，不受影响。若需 UI 完全无痕，只能服务器 ClickHouse 运维层清理（见上）。

**回滚**：三处改动只读 Langfuse 数据（写路径仅替换 trace 定位方式，create_score 不变）；回旧版即删 import/调用点。`langfuse_v4_reads` 全失败返回空结构，调用方已有兜底。撤销哨兵逻辑在 `message_feedback.delete_feedback`，回旧版即删调用。

---

## 10. 附录：关键 API 速查（源自官方文档）

| 用途 | API |
|------|-----|
| 客户端 / 校验 | `from langfuse import get_client`；`get_client().auth_check()` |
| LangChain 埋点 | `from langfuse.langchain import CallbackHandler`；`CallbackHandler()` |
| 注入 graph | `graph.with_config({"callbacks": [handler]})` |
| 请求级属性 | `from langfuse import propagate_attributes`；`propagate_attributes(trace_name=..., session_id=..., tags=..., metadata=..., version=...)` |
| 合并 trace | `client.start_as_current_observation(as_type="span", name=..., trace_context={"trace_id": X})`；`span.update(input=...); span.update(output=...)` |
| 打分 | `client.create_score(trace_id=..., name=..., value=..., data_type="NUMERIC", comment=...)` |
| Prompt 管理 | `client.create_prompt(name=..., prompt=..., labels=["production"])`；`client.get_prompt(name, label="production")`；`.get_langchain_prompt()` |
| 实验（SDK） | `from langfuse import Evaluation, RegressionError, RunnerContext`；`with Evaluation(name=..., dataset_name=..., task=..., evaluators=[...])` |
| 环境 / 发版 | env `LANGFUSE_TRACING_ENVIRONMENT` / `LANGFUSE_RELEASE` |
| CLI 查数 | `npx langfuse-cli api traces list` / `api scores get`（skill 用法） |

**文档依据**（实施前按 skill 原则再核对最新版）：
- DeepAgents 集成：https://langfuse.com/integrations/frameworks/langchain-deepagents
- LangGraph 集成：https://langfuse.com/integrations/frameworks/langgraph
- 好的 trace 长什么样：https://langfuse.com/docs/observability/best-practices
- 评估核心概念：https://langfuse.com/docs/evaluation/core-concepts
- 实验 CI/CD：https://langfuse.com/docs/evaluation/experiments/experiments-ci-cd
- Prompt 版本控制：https://langfuse.com/docs/prompt-management/features/prompt-version-control
- A/B 测试：https://langfuse.com/docs/prompt-management/features/a-b-testing
- Releases & Versioning：https://langfuse.com/docs/observability/features/releases-and-versioning
- Environments：https://langfuse.com/docs/observability/features/environments
