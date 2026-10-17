# NL2SQL 智能数据查询平台 — 架构设计与详细设计文档

> **版本**: v13.0.1 | **日期**: 2026-10-14 | **作者**: 基于代码分析自动生成

---

## 目录

1. [系统概述](#1-系统概述)
2. [整体架构](#2-整体架构)
3. [部署架构](#3-部署架构)
4. [Agent 系统](#4-agent-系统)
5. [语义层（WrenAI）](#5-语义层wrenai)
6. [混合检索层](#6-混合检索层)
7. [MCP 工具体系](#7-mcp-工具体系)
8. [API 层](#8-api-层)
9. [中间件体系](#9-中间件体系)
10. [数据与存储层](#10-数据与存储层)
11. [认证与权限隔离](#11-认证与权限隔离)
12. [可观测性](#12-可观测性)
13. [蓝绿部署](#13-蓝绿部署)
14. [评估与反馈闭环](#14-评估与反馈闭环)
15. [关键设计决策](#15-关键设计决策)

---

## 1. 系统概述

### 1.1 定位

NL2SQL 是一个**基于 SQL-of-Thought 的多智能体自然语言转 SQL 系统**，面向企业数据分析场景。用户以自然语言提问，系统自动生成 SQL、执行查询、生成可视化图表和分析报告。

### 1.2 核心理念

```
SQL-of-Thought: 将 SQL 生成拆解为多个认知步骤（知识加载→模式链接→子问题分解→查询规划→SQL 生成→性能优化→纠错），
每一步由专门的 skill/prompt 驱动，通过中间件链串联为完整流水线。
```

### 1.3 技术栈

| 层次 | 技术选型 |
|------|---------|
| 前端 | Next.js (SSR), ECharts, 独立仓库 `harness-deep-agents-ui` |
| 反向代理 | Nginx (请求期 DNS 解析, 蓝绿切流) |
| API 框架 | LangGraph API (FastAPI 内核) + Starlette 自定义路由 |
| Agent 框架 | deepagents ≥ 0.6.12 (基于 LangGraph) |
| 语义层 | WrenAI ≥ 0.13.0 (mdl.json + MCP 工具服务) |
| 大模型 | DeepSeek v4-flash (主), Qwen, GLM, Kimi (备选) |
| 混合检索 | LanceDB (向量 + FTS), bge-m3 嵌入 (1024d) |
| 工具协议 | MCP (Model Context Protocol) via fastmcp ≥ 3.4.4 |
| 数据库 | MySQL / PostgreSQL / ClickHouse / SQLite |
| 持久化 | PostgreSQL (checkpoint + 平台库), SQLite (grants/feedback) |
| 可观测性 | Langfuse (tracing + eval + annotation), LangSmith |
| 部署 | Docker Compose, 蓝绿部署, 阿里云 ECS |

### 1.4 一句话架构

> **1 主 Agent (chat_agent) + 1 子 Agent (nl2sql_agent) + 7 个 Wren SOP skills + 双通道 (语义/直连) + Langfuse 全链路可观测**

---

## 2. 整体架构

> 📊 交互式架构图: `nl2sql-architecture.html`（支持缩放、搜索、聚焦视图）

### 2.1 架构分层

```
┌─────────────────────────────────────────────────────────────────┐
│                      前端层 (Next.js :3000)                      │
│   会话管理 │ 图表渲染 │ 语义库管理 │ 标注/评估 │ 模型配置         │
├─────────────────────────────────────────────────────────────────┤
│                   反向代理层 (Nginx :80)                          │
│   蓝绿切流 │ SSE 长连接 │ 请求 ID 传播 │ 缓存策略                │
├─────────────────────────────────────────────────────────────────┤
│               API 网关层 (LangGraph API :2026)                   │
│   28 路由模块 │ 5 层中间件 │ 认证/授权 │ 优雅停机                 │
├─────────────────────────────────────────────────────────────────┤
│                      Agent 编排层                                │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐   │
│  │  主 Agent     │───▶│ NL2SQL 子Agent│───▶│ WrenAI MCP 工具  │   │
│  │  (chat_agent) │    │ (nl2sql_agent)│    │ DB MCP 工具      │   │
│  │  18 中间件    │    │ 22 中间件     │    │ ECharts 工具     │   │
│  └──────────────┘    └──────────────┘    └──────────────────┘   │
├─────────────────────────────────────────────────────────────────┤
│                      数据与存储层                                 │
│  PostgreSQL (checkpoint) │ Redis (pub/sub) │ SQLite (grants)    │
│  LanceDB (检索索引)      │ 业务数据库 (MySQL/PG/ClickHouse)      │
├─────────────────────────────────────────────────────────────────┤
│                      可观测性层                                   │
│  Langfuse (trace/span/eval) │ LangSmith │ 请求日志 │ /metrics   │
└─────────────────────────────────────────────────────────────────┘
```

> 📊 请求时序图: `nl2sql-query-sequence.html`（完整请求链路的时序交互）

### 2.2 数据流

1. **用户提问** → Nginx → LangGraph API → 主 Agent
2. **主 Agent** 意图识别 → 派发给 NL2SQL 子 Agent（异步）
3. **NL2SQL 子 Agent** → 检索上下文 → 双通道路由 → SQL 生成 → 执行 → 结果返回
4. **主 Agent** 聚合结果 → 生成图表/报告 → SSE 流式推送给前端

---

## 3. 部署架构

### 3.1 服务器规格

| 环境 | 服务器 | 配置 | 用途 |
|------|--------|------|------|
| 生产 | 8.163.4.42 (阿里云 ECS) | Ubuntu 22.04, 3.4GB RAM | 正式服务 |
| 开发 | 192.168.25.34 | — | 开发测试 |
| GPU | 192.168.25.13 | — | bge-m3 嵌入服务 |
| 可观测 | 192.168.25.64:3010 | — | Langfuse 平台 |

### 3.2 容器编排

```yaml
# docker-compose.yml (简化示意)
services:
  langgraph-api:          # 核心后端
    build: .
    ports: ["127.0.0.1:2026:2026"]   # 仅回环
    mem_limit: 8g
    stop_grace_period: 240s          # 优雅停机
    environment:
      DEPLOY_ENV: prod
      LANGGRAPH_RUNTIME_EDITION: postgres
      NL2SQL_AUTH_DISABLED: "0"

  postgres:               # 双库: nl2sql_platform + nl2sql_checkpoint
    image: postgres:14
    mem_limit: 256m

  redis:                  # 跨实例 run 发布/订阅
    image: redis:7-alpine
    mem_limit: 256m

  frontend:               # Next.js SSR
    build: ../harness-deep-agents-ui
    mem_limit: 256m

  nginx:                  # 反向代理 + 蓝绿切流
    image: nginx:alpine
    ports: ["80:80"]
    mem_limit: 64m
```

### 3.3 Dockerfile 多阶段构建

```
Stage 1 (builder): python:3.13-slim
  ├── uv sync (主依赖)
  ├── langchain-qwq (Qwen 支持)
  ├── lancedb==0.37.1 (混合检索)
  └── redis==7.4.0 (PostgreSQL runtime)

Stage 2 (runtime): python:3.13-slim
  ├── 复制 .venv
  ├── Node.js 20 (ECharts MCP npx)
  ├── core-api-grpc (从 langgraph-api 官方镜像提取)
  └── 版本闸: assert langgraph-api==0.11.1
```

### 3.4 数据持久化

| 卷 | 用途 | 生命周期 |
|----|------|---------|
| `agent_data` | workspace + shared + 用户数据 | 永久 |
| `nl2sql_pg_data` | PostgreSQL 数据 | 永久 |
| `nl2sql_redis_data` | Redis 持久化 | 可重建 |
| `nl2sql_api_meta` | 旧版 pckl 快照 (回滚用) | 保留 |
| bind mount | nginx.conf, .env.prod | 配置注入 |

---

## 4. Agent 系统

### 4.1 双 Agent 架构

```
                    ┌──────────────────────────────┐
                    │        Main Agent             │
                    │     (chat_agent)              │
                    │                               │
                    │  State: MainAgentState         │
                    │   ├── todos[]                 │
                    │   ├── query_headers           │
                    │   ├── subagent_steps_map      │
                    │   ├── active_queries          │
                    │   ├── token_stats             │
                    │   └── answer_fingerprints     │
                    │                               │
                    │  VFS: CompositeBackend (5路)   │
                    │  Middlewares: 18 layers        │
                    │  Sub-agents: [nl2sql]          │
                    └───────────┬──────────────────┘
                                │ AsyncSubAgent
                                ▼
                    ┌──────────────────────────────┐
                    │      NL2SQL Sub-Agent         │
                    │     (nl2sql_agent)            │
                    │                               │
                    │  双通道路由:                    │
                    │   ├── WrenAI 语义通道         │
                    │   └── DB MCP 直连通道         │
                    │                               │
                    │  Skills: 7 wren-* SOP         │
                    │  Middlewares: 22 layers       │
                    │  Cube 摘要注入                 │
                    └──────────────────────────────┘
```

### 4.2 主 Agent (chat_agent)

**入口**: `src/agent/main_agent.py` → `create_deep_agent()`

**核心能力**:
- **意图识别**: 判断用户提问类型（数据查询、闲聊、操作指令）
- **子 Agent 派发**: 通过 `AsyncSubAgent(name="nl2sql", graph_id="nl2sql_agent")` 异步派发
- **结果聚合**: 汇总子 Agent 输出，生成最终回答
- **图表生成**: 调用 ECharts MCP 工具生成可视化
- **报告导出**: 调用 `build_report` 工具生成 Markdown 报告
- **归因分析**: 调用 `attribution_analyze` 进行贡献度分解

**VFS 路由 (CompositeBackend)**:

| 路由 | 后端 | 用途 |
|------|------|------|
| `/workspace/` | DynamicFilesystemBackend | 用户工作区 |
| `/shared/` | DynamicFilesystemBackend | 共享记忆/skills |
| `/report/` | DynamicFilesystemBackend | 报告产物 |
| `/chart/` | DynamicFilesystemBackend | 图表 HTML |
| `default` | DynamicLocalShellBackend | Shell 执行 |

**18 层中间件 (按执行顺序)**:

| # | 中间件 | 职责 |
|---|--------|------|
| 1 | `ModelRequiredMiddleware` | 确保模型配置存在 |
| 2 | `QuotaErrorMiddleware` | 配额错误识别 |
| 3 | `ModelTimeoutMiddleware` | 模型超时熔断 |
| 4 | `ThinkingToggleMiddleware` | 思考模式切换 |
| 5 | `TokenMeterMiddleware` | Token 用量计量 |
| 6 | `TraceRecorderMiddleware` | Trace 记录 |
| 7 | `LangfuseSpanMiddleware` | Langfuse span 管理 |
| 8 | `MessageSlimmerMiddleware` | 消息压缩 (按行截断) |
| 9 | `KnowledgeTrimMiddleware` | 知识库结果裁剪 |
| 10 | `QueryResultOffloadMiddleware` | 大结果落盘 |
| 11 | `DanglingToolCallsMiddleware` | 悬空工具调用修复 |
| 12 | `QueryKeywordsMiddleware` | 查询关键词标记 |
| 13 | `CurrentDbContextMiddleware` | 当前数据库上下文注入 |
| 14 | `VfsPathResolverMiddleware` | VFS 路径解析 |
| 15 | `FilesystemThreadGuardMiddleware` | 文件线程安全 |
| 16 | `ChartArtifactOwnerMiddleware` | 图表归属 |
| 17 | `ProgressBoundaryMiddleware` | 进度边界 |
| 18 | `WriteTodosMiddleware` | TODO 写入 |

### 4.3 NL2SQL 子 Agent (nl2sql_agent)

**入口**: `src/agent/graphs/nl2sql_agent.py`

**YAML 驱动**: 配置在 `src/agent/subagents/nl2sql.yaml`

**双通道路由**:
- **WrenAI 语义通道**: 库有 `mdl.json` → 走 `wrenai_*` MCP 工具 → `engine.dry_plan()` → 物理 SQL
- **DB MCP 直连通道**: 库无语义模型 → 走 `dbmcp` MCP 工具 → 直接 SQL 生成执行
- **路由闸口**: `ToolFilterMiddleware` + `DynamicMCPToolsMiddleware` 自动判断

**22 层中间件 (关键差异)**:

| 中间件 | 职责 |
|--------|------|
| `DynamicMCPToolsMiddleware` | 运行时 MCP 工具注册表桥接 |
| `ToolFilterMiddleware` | 按语义库/直连过滤工具 |
| `FailureCircuitMiddleware` | 失败熔断 (WARN2/STOP3/预算6) |
| `CaliberGateMiddleware` | 口径治理闸口 |
| `QueryGateMiddleware` | 查询守卫 (只读 SELECT) |
| `CubeRouteGateMiddleware` | Cube 路由闸口 |
| `SQLApprovalMiddleware` | SQL 审批 (HITL) |
| `ExecuteGuardMiddleware` | 执行守卫 |

**7 个 Wren SOP Skills**:

| Skill | 职责 |
|-------|------|
| `wren-orchestrator` | SQL 生成编排器 |
| `wren-retrieve` | 知识检索与上下文加载 |
| `wren-clarify` | 歧义澄清与用户确认 |
| `wren-metric-query` | 指标查询专用 |
| `wren-sql-author` | SQL 编写 |
| `wren-perf-optimize` | 性能优化 |
| `wren-writeback` | 语义记忆回写 |

### 4.4 配置透传机制

子 Agent 通过猴子补丁 (`deepagents_async_config_patch.py`) 实现配置透传:

```python
# 补丁 _ClientCache.get_sync/get_async
# → 包装 client.runs.create
# → 注入: db_name, query_keywords, user_id
# → Langfuse 元数据: session_id, trace_name, tags
# → 父 OTel trace_id (在 orig_create 前捕获)
```

---

## 5. 语义层（WrenAI）

### 5.1 架构

```
┌─────────────────────────────────────────────┐
│              WrenAI 语义层                    │
│                                              │
│  每个已建模数据库 → 一个 wren 进程             │
│  wrenai_<slug>_run_sql                       │
│  wrenai_<slug>_get_instructions              │
│  wrenai_<slug>_get_all_knowledge             │
│  ...                                         │
│                                              │
│  项目结构:                                    │
│  ├── models/        (数据模型)                │
│  ├── cubes/         (多维分析)                │
│  ├── views/         (视图定义)                │
│  ├── knowledge/     (业务知识)                │
│  │   ├── glossary/  (术语表)                  │
│  │   ├── metrics/   (指标定义)                │
│  │   ├── rules/     (业务规则)                │
│  │   ├── sql_patterns/ (SQL 模式)            │
│  │   └── caveats/   (注意事项)                │
│  └── target/mdl.json (构建产物)               │
│                                              │
│  物理 SQL 重建:                               │
│  engine.dry_plan(semantic_sql) → 物理 SQL     │
└─────────────────────────────────────────────┘
```

### 5.2 语义库发现

`SemanticDbDetector` (`src/agent/utils/semantic_db.py`) 负责发现哪些数据库有语义模型:

1. **显式关联**: `db_config.json` 中的 `wren_project` 字段
2. **回退扫描**: `WREN_PROJECT_PATH` 下的连接文件
3. **服务命名**: `wrenai_server_name(db_name)` → `wrenai_<slug>`
   - Unicode 安全 slug 派生（剥离非 ASCII，纯中文回退 sha1[:8]）

### 5.3 Cube 机制

Cube 是预定义的多维分析模型:
- **Cube 摘要注入**: 子 Agent 启动时自动注入相关 Cube 摘要
- **Cube 路由闸口**: `CubeRouteGateMiddleware` 判断查询是否命中 Cube
- **Cube 两层渲染**: LLM 摘要 + 模板结构 + 原始 YAML
- **探值劫持防护**: 通道闸防止探值 SQL 被当作报告 SQL

### 5.4 热加载

新增/修改语义库**无需重启**:
1. `_invalidate_detector()` 清除缓存
2. 触发 MCP 工具注册表 reconciliation
3. 自动重建检索索引 (best-effort)

---

## 6. 混合检索层

### 6.1 架构

```
┌─────────────────────────────────────────────────────────┐
│                    混合检索层                             │
│                                                          │
│  ┌──────────┐    ┌──────────┐    ┌──────────────────┐   │
│  │  Embedder │    │  Indexer │    │    Search         │   │
│  │  bge-m3   │    │  构建索引 │    │  RRF 融合搜索     │   │
│  │  1024d    │    │          │    │                   │   │
│  │  4s 超时  │    │ mdl.json │    │ Σ 1/(60+rank)    │   │
│  │  后台预热  │    │ knowledge│    │ k=60             │   │
│  └────┬─────┘    └────┬─────┘    └────────┬─────────┘   │
│       │               │                    │             │
│  ┌────▼───────────────▼────────────────────▼─────────┐  │
│  │                  Backend Layer                      │  │
│  │  ┌────────────┐          ┌────────────────────┐   │  │
│  │  │  Lance     │          │  JSONL (fallback)   │   │  │
│  │  │  ICU 分词  │          │  IDF n-gram (2-6)   │   │  │
│  │  │  向量+FTS  │          │  numpy 加速         │   │  │
│  │  │  mmap 磁盘 │          │  预缓存 norms       │   │  │
│  │  └────────────┘          └────────────────────┘   │  │
│  └───────────────────────────────────────────────────┘  │
│                                                          │
│  索引位置: <workspace>/.retrieval/<project>/              │
│  版本: rev = sha256(mdl.json)[:16]                       │
│  三条铁律: 默认关闭 / 无导入时 LanceDB 依赖 / 自动降级    │
└─────────────────────────────────────────────────────────┘
```

### 6.2 三种模式

| 模式 | `NL2SQL_RETRIEVAL` | 行为 |
|------|---------------------|------|
| 关闭 | `off` | 不使用检索 |
| 全文 | `fts` | 仅 FTS 词汇匹配 |
| 混合 | `hybrid` | FTS + 向量，RRF 融合；嵌入超时自动降级 FTS |

### 6.3 嵌入服务

- **模型**: bge-m3, 1024 维
- **端点**: `http://192.168.25.13:11435/v1/embeddings`
- **超时**: 查询 4s / 索引 2.5s
- **后台预热**: 5 分钟间隔, 120s 超时（应对 GPU 冷启动 13~17s）
- **断路器**: 60s 冷却期
- **三形校验**: 响应体维度 + 模型名 + 单位长度

### 6.4 RRF 融合算法

```python
# Reciprocal Rank Fusion
score(d) = Σ 1/(k + rank_i(d))   # k=60
```

向量腿和 FTS 腿各自排序后，按 RRF 公式融合为统一排名。读侧永不抛异常——任何退化都静默降级。

### 6.5 索引构建

`indexer.py` (601 行) 从两类来源提取知识项:
1. **mdl.json**: 通过 `wren.extract_schema_items()` 提取 8 类 schema 项
2. **knowledge/**: 5 个知识分类目录下的 `.md` 文件

索引按 `## heading` 分块，补充中文业务名 + 物理表引用。

---

## 7. MCP 工具体系

### 7.1 工具注册表

```
┌──────────────────────────────────────────────────────┐
│                 MCP 工具注册表                         │
│                                                       │
│  主工具 (main_tools):                                  │
│  └── mcp-server-echarts (图表生成)                     │
│                                                       │
│  子工具 (sub_tools):                                    │
│  ├── wrenai_<slug1>_* (语义库 A 的工具集)               │
│  ├── wrenai_<slug2>_* (语义库 B 的工具集)               │
│  ├── dbmcp (数据库直连 SQL 执行)                        │
│  └── ... (按数据库动态注册)                              │
│                                                       │
│  关键服务器: wrenai_* 和 dbmcp                          │
│  → 缺失时阻止启动 (除非 MCP_ALLOW_DEGRADED=1)          │
│                                                       │
│  热加载: 指纹变更检测 → 后台重载线程 → 无需重启           │
│  预检: 启动时校验所有关键服务器可用                        │
└──────────────────────────────────────────────────────┘
```

### 7.2 工具管理 (`mcp_tool.py`, 925 行)

- **MultiServerMCPClient**: 管理多个 MCP 服务器进程
- **两阶段加载**: 每个服务器尝试两次（应对瞬时故障）
- **运行时注册**: `refresh_sub_entries()` 增删库免重启
- **指纹检测**: 基于工具列表 hash 判断是否需要重载
- **`DynamicMCPToolsMiddleware`**: 桥接注册表到模型工具列表 + 执行链
  - 新增工具 → 追加到可用列表
  - 下线工具 → 从列表移除
  - 过期实例 → 拒绝并返回回退提示

### 7.3 核心工具

| 工具 | 文件 | 功能 |
|------|------|------|
| `build_report` | `report_builder.py` (1093行) | Markdown 报告组装 |
| `attribution_analyze` | `attribution_analyze.py` | 归因分析 (贡献度分解) |
| `wrenai_*_run_sql` | wren MCP | 语义 SQL 执行 |
| `wrenai_*_get_instructions` | wren MCP | 知识加载 |
| `dbmcp_*` | db MCP | 直连 SQL 执行 |
| `mcp-server-echarts` | ECharts MCP | 图表生成 |

---

## 8. API 层

### 8.1 组合根 (`custom_app.py`)

```python
# Starlette app 通过 LANGGRAPH_HTTP 环境变量钩入 LangGraph API
app = Starlette(lifespan=lifespan)

# 5 层中间件 (由外到内):
app.add_middleware(LangfuseMetadataMiddleware)    # Langfuse 元数据注入
app.add_middleware(AuthMiddleware)                # 认证/授权
app.add_middleware(DrainGateMiddleware)           # 优雅停机排空
app.add_middleware(RequestContextMiddleware)      # 请求 ID + 访问日志
app.add_middleware(CORSMiddleware)                # 跨域
```

### 8.2 路由模块 (28 个)

| 分类 | 路由模块 | 端点 |
|------|---------|------|
| **认证** | `auth_routes` | POST login/logout, GET me, PUT change-password |
| **管理** | `auth_admin` | CRUD 用户, 强制改密 |
| **运维** | `drain` | POST /api/admin/drain (排空) |
| **监控** | `metrics` | GET /metrics (Prometheus 格式) |
| **数据库** | `db_config` | CRUD 数据库配置 |
| **部署** | `deployment_info` | GET /api/deployment-info |
| **反馈** | `message_feedback` | POST 点赞/点踩 |
| **标题** | `auto_title` | POST 自动生成会话标题 |
| **模型** | `model_config` | CRUD LLM 提供商配置 |
| **报告** | `report_file` | GET/POST 报告文件 |
| **审批** | `sql_approval` | SQL 执行审批 (HITL) |
| **任务** | `task_cancel` | POST 取消任务 |
| **会话** | `thread_compact/export/fork/search/run_status` | 会话操作 |
| **语义** | `wren_semantic` | 语义库 CRUD + 重建 |
| **追踪** | `trace_routes` | Trace 查询 |
| **统计** | `feedback_stats/annotation` | 反馈统计/标注 |
| **实验** | `experiment` | A/B 实验管理 |
| **评估** | `eval_flags` | 评估标志管理 |

### 8.3 生命周期管理

```
启动:
  1. SQLite grants 迁移
  2. 指标收集器启动
  3. 子任务终态收割器启动
  4. 保留策略执行器启动
  5. 嵌入模型预热线程启动

停机 (有序):
  1. drain (排空活跃请求)
  2. Langfuse flush
  3. 停止收割器
  4. 停止维护任务
  5. 停止嵌入预热器
  6. 停止指标收集
```

---

## 9. 中间件体系

### 9.1 分类总览

| 类别 | 中间件 | 数量 |
|------|--------|------|
| **模型门控** | model_required, quota_error, model_timeout, thinking_toggle | 4 |
| **工具管理** | dynamic_mcp_tools, tool_filter, sql_approval | 3 |
| **查询守卫** | query_gate, cube_route_gate, caliber_gate, failure_circuit, execute_guard | 5 |
| **消息处理** | message_slimmer, knowledge_trim, query_result_offload, dangling_tool_calls | 4 |
| **上下文注入** | query_keywords, current_db_context, vfs_path_resolver | 3 |
| **追踪计量** | token_meter, trace_recorder, langfuse_span | 3 |
| **进度管理** | progress_boundary, write_todos | 2 |
| **安全** | filesystem_thread_guard, chart_artifact_owner | 2 |
| **合计** | | **26+** |

### 9.2 关键中间件详解

#### FailureCircuitMiddleware (失败熔断)

```
三阈值:
  WARN = 2  → 记录警告
  STOP = 3  → 停止派发新工具
  BUDGET = 6 → 硬闸收束

机制:
  按 args 文本匹配工具调用
  ⚠️ 裂口: 视图/Cube 间接引用可绕过
```

#### QueryGateMiddleware (查询守卫)

```
强制只读:
  仅允许 SELECT 语句
  禁止 INSERT/UPDATE/DELETE/DROP/ALTER

重复问题防护:
  同会话重复提问 → 模型复用上轮结论 (0 次工具调用)
  新会话裸问 → NO_FINGERPRINT 闸
  自带指标名 + 两期 → 放行
```

#### MessageSlimmerMiddleware (消息压缩)

```
按行截断大工具结果:
  阈值: LARGE_RESULT_TRUNCATE_CHARS (默认 16000)
  大结果 → 落盘 → 返回文件路径
  ⚠️ 图表 HTML 豁免 (否则 </iframe> 被截断)
```

#### CaliberGateMiddleware (口径治理)

```
口径验证:
  业务口径必须有知识库逐字原文
  闸门两分支:
    1. 响应体模型名 vs 索引 meta
    2. caliber_sql_warning 一致性
```

---

## 10. 数据与存储层

### 10.1 存储架构

```
┌─────────────────────────────────────────────────────────┐
│                     存储架构                              │
│                                                          │
│  ┌─────────────────────────────────────────────────┐    │
│  │ PostgreSQL (生产) / SQLite (开发)                 │    │
│  │                                                   │    │
│  │  nl2sql_checkpoint  → 会话状态 (checkpoints)     │    │
│  │  nl2sql_platform    → 平台数据 (threads, runs)   │    │
│  │                                                   │    │
│  │  _SelfHealingPostgresSaver:                       │    │
│  │    连接健康检查 + 自动重连                         │    │
│  │    线程局部上下文 → 防跨线程连接关闭               │    │
│  └─────────────────────────────────────────────────┘    │
│                                                          │
│  ┌─────────────────────────────────────────────────┐    │
│  │ SQLite Grants Store                               │    │
│  │  <AGENT_DATA_ROOT>/auth/auth.sqlite               │    │
│  │  WAL 模式, busy_timeout=15s                       │    │
│  │  共享连接 + RLock (读也加锁)                      │    │
│  │  表: users, grants, thread_owner,                │    │
│  │      thread_db, report_owner                     │    │
│  └─────────────────────────────────────────────────┘    │
│                                                          │
│  ┌─────────────────────────────────────────────────┐    │
│  │ Redis (生产)                                      │    │
│  │  跨实例 run 发布/订阅                              │    │
│  │  allkeys-lru 淘汰, 256MB                         │    │
│  └─────────────────────────────────────────────────┘    │
│                                                          │
│  ┌─────────────────────────────────────────────────┐    │
│  │ 文件存储                                          │    │
│  │  <AGENT_DATA_ROOT>/                               │    │
│  │  ├── workspace/        (用户工作区)                │    │
│  │  ├── shared/           (共享记忆/skills)           │    │
│  │  ├── users/<uid>/      (用户配置)                  │    │
│  │  ├── auth/             (grants + 用户文件)         │    │
│  │  └── logs/             (运行日志)                  │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

### 10.2 Checkpointer (双模式)

| 模式 | 触发条件 | 实现 |
|------|---------|------|
| PostgreSQL | `CHECKPOINT_DB_URI` 以 `postgresql://` 开头 | `_SelfHealingPostgresSaver` |
| SQLite | 默认 | `_DynamicCheckpointer` (AsyncSqliteSaver) |

**PostgreSQL 模式特殊处理**:
- 连接健康检查 + 自动重连 (`_cursor()` 重写)
- 线程局部上下文管理器（防止跨线程连接关闭 — 2026-09-24 生产事故修复）
- 幂等 `setup()` 自动建表

### 10.3 DB 配置存储

`db_config.json` 存储运行时数据库配置:
- AES-256-GCM 加密密码
- 支持 MySQL / ClickHouse / PostgreSQL / SQLite
- 原子写入 (temp + os.replace)
- 线程安全 (RLock)
- 首次加载时从 `.env` 的 `DB_N_*` 迁移

---

## 11. 认证与权限隔离

### 11.1 认证流程

```
┌────────┐    ┌──────────┐    ┌──────────────┐    ┌──────────┐
│ 浏览器  │───▶│ Nginx    │───▶│ AuthMiddleware│───▶│ 业务路由  │
│ Cookie  │    │ 透传     │    │               │    │          │
└────────┘    └──────────┘    └──────┬───────┘    └──────────┘
                                     │
                          ┌──────────▼──────────┐
                          │   Token 验证         │
                          │                      │
                          │ 1. 提取 Cookie/Bearer│
                          │ 2. HMAC-SHA256 验签  │
                          │ 3. 检查过期 (24h)    │
                          │ 4. 检查 token_version│
                          │ 5. Docker 内部旁路   │
                          │    (172.x + 无Cookie │
                          │     + 无 XFF)        │
                          └─────────────────────┘
```

### 11.2 密码存储

- **算法**: PBKDF2-HMAC-SHA256
- **迭代**: 260,000 次
- **盐**: 随机生成
- **存储**: `<AGENT_DATA_ROOT>/auth_users.json`
- **原子写入**: mkstemp + os.replace

### 11.3 权限模型

```
┌─────────────────────────────────────────────────────┐
│                   权限层次                            │
│                                                      │
│  Layer 1: 认证                                       │
│    HMAC token → 用户身份                             │
│    Docker 内部 → internal identity (fail-closed)    │
│                                                      │
│  Layer 2: 数据库授权                                  │
│    grants 表: user_id × db_name                     │
│    admin → 所有已配置库                               │
│    普通用户 → grants ∩ 已配置库                       │
│                                                      │
│  Layer 3: 线程隔离                                    │
│    thread_owner 表: thread_id → owner               │
│    fail-closed: 未注册 = 拒绝                         │
│    admin 可查看所有                                   │
│    legacy 线程: 向后兼容                              │
│                                                      │
│  Layer 4: 报告归属                                    │
│    report_owner 表: file → owner                     │
│    只能访问自己的报告                                  │
└─────────────────────────────────────────────────────┘
```

### 11.4 LangGraph Auth 钩子

```python
# backend.py — 4 个线程所有权钩子:

@auth.on("threads.create")    # 创建线程时:
  → 在 metadata 中盖章 owner    # 同时写入 grants

@auth.on("threads.search")    # 搜索线程时:
  → 过滤为自己的 + legacy 线程

@auth.on("threads.update")    # 更新线程时:
  → owner 键不可变 + 所有权过滤

@auth.on("threads")           # 兜底 (读/删/创建 run):
  → 所有权过滤
```

---

## 12. 可观测性

### 12.1 Langfuse 集成

```
┌────────────────────────────────────────────────────┐
│              Langfuse 可观测性                       │
│                                                     │
│  数据采集:                                           │
│  ├── TraceRecorderMiddleware → 每轮 LLM trace      │
│  ├── LangfuseSpanMiddleware → span 嵌套             │
│  ├── TokenMeterMiddleware → token 用量              │
│  └── LangfuseMetadataMiddleware → session/tags     │
│                                                     │
│  配置:                                               │
│  ├── LANGFUSE_ENABLE (总开关)                        │
│  ├── LANGFUSE_HOST (自托管 192.168.25.64:3010)      │
│  ├── LANGFUSE_CANARY_LABEL (A/B 灰度)               │
│  └── LANGFUSE_TRACING_ENVIRONMENT (dev/prod)        │
│                                                     │
│  评估:                                               │
│  ├── LLM-judge (30% 采样, 生产关闭)                  │
│  ├── 离线 Experiment (run_experiment CLI)            │
│  └── BadCase 自动采集 (点踩触发)                     │
│                                                     │
│  Fail-open: Langfuse 故障不影响业务                   │
└────────────────────────────────────────────────────┘
```

### 12.2 请求日志

- **自定义 Nginx 日志格式** (`nl2sql`): 包含 request_time, request_id, user_agent
- **RequestContextMiddleware**: 请求 ID 传播 + 访问日志
- **日志轮转**: TimedRotatingFileHandler, 每日轮转, 保留 7 天

### 12.3 指标

- `/metrics` 端点 (Prometheus 格式)
- 复用 `meta_metrics` 收集器
- ⚠️ 经 Nginx 是 404（需要直接访问后端端口）

---

## 13. 蓝绿部署

### 13.1 设计原则

```
铁律:
  1. 先切流，再排空
  2. A 色常驻，B 色只在切换窗口
  3. 竞态只在停旧色时发生

12 步流程:
  A→ 切 B → 停 A → 起回 A → 切回 A → 停删 B
```

### 13.2 Nginx 切流机制

```nginx
# 请求期 DNS 解析 (非启动期)
resolver 127.0.0.11 valid=10s ipv6=off;

# 蓝绿切换点 — 单行 sed 替换
set $backend_up "langgraph-api:2026";   # 或 "langgraph-api-b:2026"

# 变量式 proxy_pass → 每次请求重新解析 DNS
proxy_pass http://$backend_up;

# worker_shutdown_timeout 610s (≥ proxy_read_timeout 600s)
# → 确保长 SSE 连接在 worker 重启时不被切断
```

### 13.3 排空机制

```
DrainGateMiddleware:
  POST /api/admin/drain → 设置排空标志
  → 新请求返回 503
  → 等待活跃请求完成
  → 排空计数是进程内的 (docker exec 看不到)
  → 观测: 平台库 run 表
```

### 13.4 Color B Overlay

```yaml
# compose.colorB.yml
services:
  langgraph-api-b:        # 铁律: 只新增服务，不修改现有
    image: IMAGE_ID_PLACEHOLDER   # 发版脚本替换
    environment:
      N_JOBS_PER_WORKER: ${COLOR_B_JOBS:-0}  # 0=空闲
    volumes:
      - agent_data         # 共享数据卷
    restart: "no"          # 不自动重启
```

---

## 14. 评估与反馈闭环

### 14.1 反馈采集

```
用户点踩 → POST /api/messages/feedback
  → FeedbackStore (SQLite)
  → BadCase 自动收录 (eval_queue.sqlite)
  → Langfuse annotation

用户点赞 → Good Set 快照
  → 不进 Good Set (SQL 快照未实现)
```

### 14.2 评估流水线

```
┌─────────────┐    ┌──────────────┐    ┌──────────────┐
│  BadCase    │───▶│ eval_queue   │───▶│ LLM Judge    │
│  自动采集   │    │ .sqlite      │    │ (30% 采样)   │
└─────────────┘    └──────────────┘    └──────────────┘
                          │
                          ▼
                   ┌──────────────┐
                   │ 标注前端      │
                   │ (Dataset 模块)│
                   └──────────────┘
```

### 14.3 离线实验

```bash
# CLI 入口
python -m src.agent.eval.run_experiment \
  --dataset <dataset_name> \
  --semantic <ref> \
  --run-name <name>
```

支持:
- 数据集管理 (从 Langfuse Dataset 读取)
- A/B 测试 (不同模型/语义版本)
- 跟随主模型 (默认实验用当前主模型)

---

## 15. 关键设计决策

### 15.1 为什么用 deepagents 而非直接 LangGraph?

| 考量 | deepagents | 直接 LangGraph |
|------|-----------|----------------|
| 文件系统 | 内置 VFS + CompositeBackend | 需自建 |
| Skills | SkillsMiddleware 自动注入 | 需自建 |
| 子 Agent | AsyncSubAgent 内置 | 需自建通信 |
| 中间件 | 18+ 预制件 | 从零搭建 |
| 配置透传 | 猴子补丁 `_ClientCache` | 不适用 |

### 15.2 为什么是双通道而非统一语义层?

```
WrenAI 语义通道:
  ✅ 业务语义理解 (mdl.json)
  ✅ 知识检索 (knowledge/)
  ✅ Cube 多维分析
  ❌ 需要预先建模
  ❌ wren 进程开销

DB MCP 直连通道:
  ✅ 即开即用
  ✅ 低延迟
  ❌ 无业务语义
  ❌ 需要模型直接生成正确 SQL

路由策略: 有语义模型 → 语义通道; 无 → 直连通道
```

### 15.3 为什么 fail-open 而非 fail-closed?

系统在多个关键点采用 fail-open 策略:

| 组件 | 策略 | 理由 |
|------|------|------|
| 检索 | fail-open | 检索降级不应阻断查询 |
| 嵌入 | fail-open (降级 FTS) | GPU 故障不应阻断服务 |
| Langfuse | fail-open | 追踪故障不应阻断业务 |
| 权限 (read) | fail-open | 读权限异常不阻断 |
| 权限 (write) | fail-closed | 写操作必须确认权限 |
| 线程所有权 | fail-closed | 隔离是安全底线 |

### 15.4 为什么中间件这么多层?

```
三层防御模型:
  Layer 1: Prompt (提示词引导)
  Layer 2: Soft Gates (软闸口, 可降级)
  Layer 3: Hard Circuit Breakers (硬熔断, 不可绕过)

中间件链的排序依赖:
  模型门控 → 必须在最外层 (省钱)
  工具管理 → 必须在模型调用前
  查询守卫 → 必须在 SQL 执行前
  消息处理 → 必须在模型看到消息前
  上下文注入 → 必须在模型看到消息前
  追踪 → 跨全链路
```

### 15.5 为什么不手动发版打 git tag?

```
发版 = 整包 src 上传到服务器
  → 手动发版不打 git tag
  → 打 tag ≠ 部署
  → 判线上跑什么只有 docker exec md5sum
```

---

## 附录 A: 目录结构速查

```
nl2sql/
├── start_server.py          # 入口: 环境初始化 + 预检 + uvicorn
├── graph.json               # 生产图注册
├── langgraph.json           # 开发图注册
├── docker-compose.yml       # 开发 compose
├── Dockerfile               # 多阶段构建
├── pyproject.toml           # Python 依赖 (uv)
│
├── docker/
│   ├── compose.prod.yml     # 生产 compose
│   ├── compose.colorB.yml   # 蓝绿 B 色叠加
│   ├── compose.dev.yml      # 开发服务器 compose
│   ├── nginx.conf           # Nginx 配置
│   └── DEPLOY.md            # 部署手册
│
├── src/
│   ├── api/                 # API 层 (28 路由 + 5 中间件)
│   │   ├── custom_app.py    # 组合根
│   │   ├── auth_middleware.py
│   │   ├── auth_routes.py
│   │   └── ... (28 模块)
│   │
│   ├── agent/               # Agent 核心
│   │   ├── main_agent.py    # 主 Agent
│   │   ├── graphs/          # 子 Agent 图
│   │   ├── middlewares/     # 26+ 中间件
│   │   ├── tools/           # MCP 工具 + report_builder
│   │   ├── retrieval/       # 混合检索
│   │   ├── auth/            # 认证/授权
│   │   ├── checkpoint/      # Checkpointer
│   │   ├── settings/        # 配置管理
│   │   ├── subagents/       # 子 Agent YAML
│   │   ├── shared/          # 共享资源
│   │   ├── prompt/          # 系统提示词
│   │   ├── eval/            # 评估系统
│   │   ├── feedback/        # 反馈存储
│   │   ├── trace/           # Langfuse 追踪
│   │   └── workspace_manager/ # 工作区管理
│   │
│   └── mcp_server/          # DB MCP 服务器
│       └── db_mcp_server/
│
├── scripts/                 # 60+ 验证/运维脚本
└── docs/                    # 中文文档集
```

## 附录 B: 环境变量速查

| 变量 | 用途 | 示例值 |
|------|------|--------|
| `DEPLOY_ENV` | 环境标志 | `prod` / `dev` |
| `LANGGRAPH_HTTP` | 自定义 app 钩子 | `app=src.api.custom_app:app` |
| `CHECKPOINT_DB_URI` | PostgreSQL checkpoint | `postgresql://...` |
| `NL2SQL_AUTH_DISABLED` | 关闭认证 | `0` (生产) / `1` (开发) |
| `NL2SQL_RETRIEVAL` | 检索模式 | `hybrid` / `fts` / `off` |
| `AGENT_DATA_ROOT` | 数据根目录 | `D:\nl2sql_data` |
| `WREN_BIN_PATH` | Wren CLI 路径 | `/usr/local/bin/wren` |
| `LANGFUSE_ENABLE` | Langfuse 总开关 | `true` |
| `MCP_ALLOW_DEGRADED` | 允许降级启动 | `1` |
| `BG_JOB_ISOLATED_LOOPS` | 隔离事件循环 | `true` |
| `DB_CONFIG_SECRET` | DB 密码加密密钥 | (AES-256-GCM key) |

## 附录 C: 验证脚本索引

项目包含 **60+ 验证脚本** (`scripts/verify_*.py`)，覆盖:

| 类别 | 脚本 |
|------|------|
| 部署 | verify_blue_green, verify_release_zero_downtime, verify_graceful_drain |
| 认证 | verify_auth_middleware, verify_auth_hardening, verify_thread_ownership |
| 语义 | verify_semantic_materialize, verify_wren_offload, verify_cube_route_gate |
| 存储 | verify_store_concurrency, verify_retention, verify_pending_terminal |
| 检索 | verify_retrieval, verify_retrieval_wiring, verify_embed_warmup |
| 工具 | verify_dynamic_mcp_hint, verify_tool_pool, verify_failure_circuit |
| 报告 | verify_report_caliber, verify_caliber_evidence, verify_sql_pick |

---

> **文档结束** | 架构图: `nl2sql-architecture.html` | 时序图: `nl2sql-query-sequence.html` | 生成时间: 2026-10-14
