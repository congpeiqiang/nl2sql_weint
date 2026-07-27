# NL2SQL — 基于 SQL-of-Thought 的多智能体自然语言转 SQL 系统

基于 **WrenAI 语义层引擎** + **Harness Engineering Agent 架构**的 NL2SQL 项目。参考论文 *"SQL-of-Thought: Multi-agentic Text-to-SQL with Guided Error Correction"* (Chaturvedi et al., 2025)。

## 核心特性

- **SQL-of-Thought 流水线** — 5 步结构化推理：Schema Linking → Subproblem → Query Plan → SQL Generation → Execution
- **多智能体架构** — 主智能体（意图路由 + 异步委派）+ NL2SQL 子智能体（专业 SQL 流水线）
- **WrenAI 语义层** — 以 MCP 工具形式嵌入，提供 MDL 建模、语义检索、Cube 指标查询
- **分类法引导纠错** — 9 大类 31 小类错误分类体系，最多 3 次自动纠错循环
- **图表可视化** — 通过 Semiotic MCP 渲染 SVG 图表（柱状图、折线图等）
- **实时进度追踪** — 异步子智能体执行过程中可查看每步进度和耗时
- **技能系统** — 6 个 NL2SQL 技能按需加载，覆盖完整 Text-to-SQL 流程

## 架构概览

```
前端 (Next.js UI)
  │  LangGraph API (REST/SSE)
  ▼
LangGraph API Server (port 2026)
  ├── chat_agent    → 主智能体（意图识别 → 子智能体委派 → 结果汇总）
  └── nl2sql_agent  → NL2SQL 子智能体（SQL-of-Thought 流水线）
        │  MCP (stdio)
        ├── WrenAI  MCP     → 语义层、MDL、SQL 执行、知识检索（18 个工具）
        └── Semiotic MCP    → 图表渲染 SVG（15 个工具）
```

### 主智能体

- 意图识别 → 子智能体委派 → 结果汇总
- 异步子智能体管理：`start_async_task` / `check_async_task` / `cancel_async_task`
- 图表渲染（Semiotic MCP）
- 技能：意图路由、报告导出、阿里云技能搜索

### NL2SQL 子智能体

- **Phase 0**（可选）：WrenAI 语义层预处理（generate-mdl → enrich-context → context-build）
- **Phase 1**：Schema 知识准备
- **Phase 2**：顺序流水线 5 步（Schema Linking → Subproblem → Query Plan → SQL Generation → 执行）
- **Phase 3**：分类法引导纠错循环（最多 3 次）

### 三种执行策略

| 策略 | 适用场景 | 路径 |
|------|---------|------|
| **A 标准流水线** | 复杂多表 JOIN / 聚合 / 窗口函数 | Schema Linking → Subproblem → Query Plan → SQL Generation |
| **B 快速通道** | 单表 / 简单筛选 / 计数 | 跳过子问题分解，直接 Query Plan → SQL |
| **C Cube 通道** | 预定义指标查询 | 直接调用 `query_cube` |

## 技术栈

| 层次 | 技术 |
|------|------|
| LLM | DeepSeek deepseek-v4-flash（via `langchain-deepseek`） |
| Agent 框架 | `deepagents` ≥ 0.6.12 |
| 编排运行时 | LangGraph API ≥ 0.11.1（uvicorn） |
| MCP 协议 | `langchain-mcp-adapters` + `fastmcp` ≥ 3.4.4 |
| 语义层 | WrenAI ≥ 0.13.0 |
| 图表 | Semiotic MCP（`npx semiotic-mcp`） |
| 数据库 | MySQL（主要）、SQLite，支持 10 种数据库引擎 |
| 可观测性 | LangSmith tracing |
| 前端 | Next.js + TypeScript + Tailwind CSS（独立项目） |

## 项目结构

```
nl2sql/
├── start_server.py                # 服务启动脚本
├── graph.json                     # LangGraph 图注册
├── pyproject.toml                 # 项目依赖
├── .env                           # 环境配置
├── ARCHITECTURE.md                # 详细架构文档
│
├── docs/                          # 文档和参考资料
│   ├── agent优化记录/              # 架构优化方案
│   ├── imdb数据库/                 # IMDB schema + 测试计划
│   ├── chinook数据库/              # Chinook DDL + 测试
│   └── 论文SQL-of-Thought/        # 论文 PDF + 中文翻译
│
└── src/
    ├── agent/                     # Agent 核心
    │   ├── main_agent.py          # 主智能体
    │   ├── nl2sql_agent.py        # NL2SQL 子智能体
    │   ├── llms/model.py          # LLM 模型工厂
    │   ├── tools/mcp_tool.py      # MCP 多服务器客户端
    │   ├── prompt/                # 系统提示词
    │   │   ├── MAIN_AGENT_PROMPT.md
    │   │   └── NL2SQL_SYSTEM_PROMPT.md
    │   ├── subagents/
    │   │   ├── configs/nl2sql.yaml    # 子智能体配置
    │   │   ├── track_progress.py      # 进度追踪中间件
    │   │   └── check_progress.py      # check_async_task 增强补丁
    │   ├── skills/                # 技能定义（SKILL.md）
    │   │   ├── main/              # 主智能体技能
    │   │   └── nl2sql/            # NL2SQL 技能（6 个）
    │   ├── backends/              # 沙箱后端
    │   ├── utils/path_resolver.py # 工具包装器
    │   └── workspace/             # 运行时工作空间
    │
    └── mcp_server/                # 独立 DB MCP 服务器
        └── db_mcp_server/
            ├── db/db_server.py    # FastMCP 服务
            └── db/engine/         # 10 种数据库 Runner
```

### NL2SQL 技能列表

| 技能 | 描述 |
|------|------|
| `sql-of-thought` | 编排器，策略 A/B/C 路由 |
| `nl2sql-schema-linking` | Schema 发现与裁剪 |
| `nl2sql-subproblem` | 子句级问题分解 |
| `nl2sql-query-plan` | CoT 查询计划（禁止输出 SQL） |
| `nl2sql-sql-generation` | SQL 生成 |
| `nl2sql-correction` | 错误分类法引导纠错 |

## 快速开始

### 前置条件

- Python ≥ 3.13
- Node.js（用于 Semiotic MCP）
- [uv](https://docs.astral.sh/uv/) 包管理器
- MySQL 数据库（或 SQLite 文件）
- WrenAI MCP（通过 `wrenai` 包安装）

### 安装

```bash
# 克隆项目
cd nl2sql

# 安装依赖
uv sync

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入 LLM API Key、数据库连接信息等
```

### 启动

```bash
python start_server.py
```

启动后：

| 地址 | 说明 |
|------|------|
| `http://localhost:2026` | API 服务 |
| `http://localhost:2026/docs` | API 文档 |
| `http://localhost:2026/ui` | LangGraph Studio UI |
| `http://localhost:2026/ok` | 健康检查 |

或使用 LangGraph CLI：

```bash
langgraph dev --port 2026
```

## 配置

### .env 主要配置项

```env
# AI 模型
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash

# LangSmith 可观测性
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_pt_xxx
LANGSMITH_PROJECT=nl2sql

# WrenAI
WREN_PROJECT_PATH=D:\path\to\wrenai_project
```

### graph.json — LangGraph 图注册

```json
{
  "graphs": {
    "chat_agent":   { "path": "./src/agent/main_agent.py:agent" },
    "nl2sql_agent": { "path": "./src/agent/nl2sql_agent.py:agent" }
  }
}
```

### nl2sql.yaml — 子智能体配置

定义 NL2SQL 子智能体的工具集、技能路径和系统提示词：

```yaml
name: nl2sql
description: NL2SQL 查询专家
tools:
  - run_sql, dry_run, dry_plan, query_cube    # WrenAI 核心工具
  - list_models, describe_model, get_context   # Schema 检索
  - suggestCharts, renderInteractiveChart      # 图表工具
  - ...
skills:
  - /workspace/skills/nl2sql/
system_prompt_file: prompt/NL2SQL_SYSTEM_PROMPT.md
```

## MCP 工具

### WrenAI MCP（18 个工具）

| 工具 | 功能 |
|------|------|
| `run_sql` | 通过语义层执行 SQL |
| `dry_run` | 验证 SQL 语法 |
| `dry_plan` | MDL SQL → 真实 SQL 转换 |
| `query_cube` | Cube 指标查询 |
| `list_models` / `describe_model` | 语义模型管理 |
| `get_context` | 语义 Schema 检索 |
| `recall_queries` | 相似 NL→SQL 示例检索 |
| `get_instructions` | 业务规则加载 |

### Semiotic MCP（15 个工具）

`suggestCharts`、`renderChart`、`getSchema`、`diagnoseConfig`、`renderInteractiveChart` 等，用于图表推荐、渲染和验证。

### DB MCP Server（自建）

独立的多数据库 MCP 服务器，支持 10 种数据库引擎：

```bash
python -m mcp_server.db_mcp_server.db.db_server --transport http --port 8000
```

支持：MySQL、PostgreSQL、SQLite、BigQuery、Snowflake、MSSQL、Oracle、ClickHouse、Presto、DuckDB

## 进度追踪

异步子智能体执行过程中，`ProgressTrackerMiddleware` 记录每步状态转换和时间戳。通过 `check_async_task` 可查看：

```json
{
  "status": "running",
  "elapsed": "2m30s",
  "progress": "4/6 (67%)",
  "steps": [
    "✅ Schema发现 (20s)",
    "✅ SQL生成与验证 (40s)",
    "🔄 查询执行 (15s...)",
    "⬜ 结果汇总"
  ],
  "current_step": "查询执行",
  "current_action": "run_sql"
}
```

## 设计原则

1. **阶段化推理不可跳过** — 每个流水线阶段必须顺序执行
2. **Query Plan 严禁生成 SQL** — 纯文本步骤式计划，SQL 由专门阶段生成
3. **分类法引导纠错** — 基于 9 大类错误分类体系诊断修复
4. **纠错历史隔离** — 每次纠错尝试不共享前次历史
5. **结构化推理先于 SQL** — 必须先有推理过程，再输出 SQL

## 参考

- 论文：*SQL-of-Thought: Multi-agentic Text-to-SQL with Guided Error Correction*
- 架构文档：[ARCHITECTURE.md](./ARCHITECTURE.md)
- WrenAI：https://github.com/Canner/WrenAI
- Semiotic：https://github.com/nteract/semiotic
