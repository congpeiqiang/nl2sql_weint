# NL2SQL + WrenAI + Harness Engineering 项目方案

> 基于 WrenAI 语义层引擎 + Harness Engineering Agent 架构的 NL2SQL 项目
> 参考项目架构: ERP_OPENCLAW (马士兵AI大模型直播课)

---

## 项目概述

本项目实现一个 NL2SQL 系统，核心设计思路：
- **WrenAI** 作为 NL2SQL 语义层引擎，以 Skill 形式嵌入 Agent 体系
- **Harness Engineering** 模式驱动 Agent 架构（参考 ERP_OPENCLAW 项目）
- 主 Agent 协调 + 子 Agent 技能 + MCP 工具 + 沙箱隔离 + 记忆系统

---

## 整体架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                    前端 (Next.js + TypeScript)                       │
│                POST /threads/{id}/runs (SSE 流式)                    │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                   LangGraph API Server (port 2026)                    │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  graphs:                                                      │   │
│  │    chat_agent    → src/agent/main_agent.py:agent               │   │
│  │    nl2sql_agent  → src/agent/graphs/nl2sql_agent.py:agent     │   │
│  │                                                                │   │
│  │  checkpointer:                                                │   │
│  │    → src/agent/checkpoint/checkpointer_factory.py:checkpointer │   │
│  │      (AsyncSqliteSaver，按工作区隔离)                          │   │
│  │                                                                │   │
│  │  custom_app:                                                   │   │
│  │    → src/api/custom_app.py (合并自定义 API 路由)               │   │
│  └──────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     主 Agent (chat_agent)                             │
│            意图识别 → 异步委派子 Agent → 结果汇总                     │
│                                                                      │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────────┐   │
│  │ Semiotic MCP│  │  技能系统     │  │  SubAgentMiddleware       │   │
│  │ (图表渲染)  │  │  (按需加载)   │  │  (异步子 Agent 管理)      │   │
│  └─────────────┘  └──────────────┘  └───────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  NL2SQL 子 Agent (nl2sql_agent)                       │
│               SQL-of-Thought 流水线 + 分类法纠错                      │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  SkillsMiddleware → 自动加载 /workspace/skills/nl2sql/       │   │
│  │  SkillDataMiddleware → 中间数据读写                            │   │
│  │  SQL 审批闸门 → 写操作/DDL 拦截                                │   │
│  │  ProgressTrackerMiddleware → 步骤进度追踪                     │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  MCP 工具:                                                    │   │
│  │    WrenAI MCP (18 个工具) → 语义层/SQL 执行/Schema 检索       │   │
│  │    DB MCP Server → 直连数据库（10 种引擎）                    │   │
│  └──────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                多工作区隔离（WorkspaceManager）                       │
│                                                                      │
│  按工作区隔离: checkpoint/ feedback/ db_config/ semantic/ report/    │
│  全局共享:     memory/ skills/                                       │
└──────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    目标数据库 (MySQL/PG/ClickHouse/...)               │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 项目目录结构

```
nl2sql/
├── start_server.py                # 服务启动脚本（读取 graph.json）
├── graph.json                     # LangGraph 图注册（start_server.py 使用）
├── langgraph.json                 # LangGraph 图注册（langgraph dev 使用）
├── pyproject.toml                 # 项目依赖
├── .env                           # 环境配置
├── README.md                      # 项目说明
├── ARCHITECTURE.md                # 本文档
│
├── docs/                          # 文档和参考资料
│   ├── agent优化记录/              # 架构优化方案
│   ├── chinook数据库-aliyun/       # Chinook 测试数据
│   └── 论文SQL-of-Thought/        # 论文 PDF + 中文翻译
│
└── src/
    ├── agent/                     # Agent 核心
    │   ├── main_agent.py          # 主智能体（意图路由 + 异步委派）
    │   ├── graphs/                # Agent 图定义
    │   │   └── nl2sql_agent.py    # NL2SQL 子智能体（SQL-of-Thought 流水线）
    │   ├── checkpoint/            # 持久化层
    │   │   └── checkpointer_factory.py # 自定义 Checkpointer（AsyncSqliteSaver）
    │   ├── workspace_manager/     # 多工作区管理
    │   │   ├── __init__.py        # 重导出（向后兼容）
    │   │   ├── manager.py         # WorkspaceManager 单例
    │   │   └── workspaces.json    # 工作区注册表
    │   ├── workspace/             # 默认工作区数据目录
    │   │   ├── checkpoint/        # Checkpoint 数据库
    │   │   ├── feedback/          # 用户反馈
    │   │   ├── db_config.json     # 数据库连接配置
    │   │   ├── model_config.json  # 模型配置
    │   │   ├── memory/            # 共享记忆
    │   │   ├── skills/            # 技能定义
    │   │   ├── report/            # 生成报告
    │   │   └── tmp/               # 临时文件
    │   ├── llms/                  # LLM 模型
    │   │   └── model.py           # 模型工厂（DeepSeek/GLM/Kimi）
    │   ├── tools/                 # 工具层
    │   │   └── mcp_tool.py        # MCP 多服务器客户端
    │   ├── prompt/                # 系统提示词
    │   │   ├── MAIN_AGENT_PROMPT.md
    │   │   └── NL2SQL_SYSTEM_PROMPT.md
    │   ├── subagents/             # 子 Agent 管理
    │   │   ├── configs/           # 子 Agent 配置（YAML）
    │   │   │   └── nl2sql.yaml
    │   │   ├── loader.py          # YAML 配置加载
    │   │   ├── track_progress.py  # 进度追踪中间件
    │   │   ├── check_progress.py  # 异步任务状态检查
    │   │   ├── sync_launcher.py   # 同步启动器
    │   │   └── sync_subagent_todos.py # 子 Agent 任务同步
    │   ├── skills/                # 技能定义（SKILL.md）
    │   │   ├── main/              # 主智能体技能
    │   │   └── nl2sql/            # NL2SQL 技能（7 个）
    │   ├── middlewares/           # 中间件
    │   │   ├── write_todos.py     # Todo 写入协议
    │   │   ├── sql_approval.py    # SQL 审批闸门
    │   │   └── skill_data.py      # 技能数据管理
    │   ├── feedback/              # 反馈存储
    │   │   └── store.py           # 反馈 SQLite 存储
    │   ├── memory/                # 长期记忆
    │   │   └── ORCHESTRATOR.md    # 编排器行为准则
    │   ├── backends/              # 沙箱后端
    │   ├── settings/              # 配置与权限
    │   │   ├── setting.py         # 全局配置
    │   │   ├── file_permissions.py # 文件权限控制
    │   │   └── model_config_store.py # 模型配置存储
    │   └── utils/                 # 工具函数
    │       ├── path_resolver.py   # 路径解析
    │       └── semantic_db.py     # 语义库检测
    │
    ├── api/                       # 自定义 API 路由（合并进 LangGraph API）
    │   ├── custom_app.py          # 组合根（注册所有路由）
    │   ├── workspace.py           # 工作区管理 API
    │   ├── db_config.py           # 数据库配置 API
    │   ├── model_config.py        # 模型配置 API
    │   ├── wren_semantic.py       # Wren 语义库管理 API
    │   ├── message_feedback.py    # 用户反馈 API
    │   ├── sql_approval.py        # SQL 审批 API
    │   ├── thread_fork.py         # Thread 分支 API
    │   ├── thread_search.py       # Thread 搜索 API
    │   └── auto_title.py          # 自动标题 API
    │
    └── mcp_server/                # 独立 DB MCP 服务器
        └── db_mcp_server/
            ├── db_server.py       # FastMCP 服务入口
            └── db/engine/         # 10 种数据库 Runner
```

---

## 关键设计决策

### 1. 多工作区隔离

```
多工作区（WorkspaceManager）
├── 默认工作区: src/agent/workspace/（零配置回退）
├── 共享资源:   src/agent/shared/（独立于工作区，可被 SHARED_RESOURCES_PATH 覆盖）
├── 注册表:    src/agent/workspace_manager/workspaces.json
├── 隔离项:     checkpoint/ feedback/ db_config.json/ semantic/
│               report/ tmp/ nl2sql_process_data/ large_tool_results/
└── 共享项:     memory/ skills/ model_config.json（model_config 可被工作区覆盖）
```

- 切换工作区即时生效（DynamicFilesystemBackend 延迟解析 root_dir）
- 所有 `get_store()` 函数追踪工作区路径，切换时自动重建

### 2. SQL-of-Thought 流水线

```
用户输入 → 主 Agent → 意图路由
                │
        ┌───────┴───────┐
        ▼               ▼
  数据查询类        其他意图
        │
        ▼
  NL2SQL 子 Agent
  ├── Phase 0（可选）: WrenAI 语义层预处理
  ├── Phase 1: Schema 知识准备
  ├── Phase 2: 策略 A/B/C 流水线
  │   ├── Schema Linking → Subproblem → Query Plan → SQL Generation → 执行
  │   └── 快速通道: 跳过子问题分解
  └── Phase 3: 分类法引导纠错（最多 3 次）
```

### 3. VFS 复合后端

```
CompositeBackend (最长前缀匹配)
├── /workspace/memory/  → shared_memory_backend (共享)
├── /workspace/skills/  → shared_skills_backend (共享)
├── /workspace/         → workspace_data_backend (当前工作区)
└── /                   → shared_code_backend (代码只读)
```

### 4. 双通道路由

根据当前数据库是否在 Wren 语义层建模，动态注入通道路由：
- **语义层通道**: 使用 `wrenai_<库名>_*` 工具链
- **直连通道**: 使用 `dbmcp_*` 工具链

### 5. SQL 安全控制

- SQL 审批中间件：写操作/DDL/疑似全表拉取触发 HITL interrupt
- 文件权限控制：声明式规则，默认 allow + 显式 deny 兜底

### 6. 技术栈

| 层次 | 技术 |
|------|------|
| LLM | DeepSeek / GLM / Kimi（via langchain） |
| Agent 框架 | deepagents ≥ 0.6.12 |
| 编排运行时 | LangGraph API ≥ 0.11.1（uvicorn） |
| MCP 协议 | langchain-mcp-adapters + fastmcp |
| 语义层 | WrenAI ≥ 0.13.0 |
| 图表 | Semiotic MCP |
| 数据库 | MySQL / PG / ClickHouse / SQLite 等 10 种 |
| 持久化 | AsyncSqliteSaver（checkpoint）+ SQLite（feedback/config） |
| 可观测性 | LangSmith tracing |
| 前端 | Next.js + TypeScript + Tailwind CSS（独立项目） |
