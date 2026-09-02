# NL2SQL 多智能体数据查询与分析系统 — 技术白皮书

---

## 目录

1. [产品概述](#1-产品概述)
2. [系统架构](#2-系统架构)
3. [核心技术](#3-核心技术)
4. [SQL-of-Thought 推理管线](#4-sql-of-thought-推理管线)
5. [多智能体协作机制](#5-多智能体协作机制)
6. [知识库与语义层](#6-知识库与语义层)
7. [可视化与报告生成](#7-可视化与报告生成)
8. [部署架构](#8-部署架构)
9. [安全与可靠性](#9-安全与可靠性)
10. [技术规格](#10-技术规格)

---

## 1. 产品概述

### 1.1 产品定位

NL2SQL 多智能体数据查询与分析系统是一款面向企业数据分析场景的智能体产品。用户通过自然语言对话即可完成从数据查询、可视化图表到分析报告的全链路自动化，无需掌握 SQL 语法或数据分析工具。

### 1.2 核心价值

| 价值维度 | 说明 |
|---------|------|
| **降低数据查询门槛** | 自然语言输入，系统自动理解业务语义并生成精确 SQL |
| **提升分析效率** | 7 步结构化推理 + 3 策略路由，复杂查询一次生成准确率高 |
| **全链路自动化** | 查询 → 图表推荐 → 图表渲染 → 报告生成，用户零操作 |
| **透明可控** | 实时进度追踪，每步执行状态与耗时可视化 |
| **数据安全** | 私有化部署，数据不出内网 |

### 1.3 技术基础

系统基于以下学术研究与开源技术构建：

- **论文方法论**：*"SQL-of-Thought: Multi-agentic Text-to-SQL with Guided Error Correction"*（Chaturvedi et al., 2025）
- **智能体框架**：DeepAgents（Harness Engineering 架构）
- **编排运行时**：LangGraph API
- **语义层引擎**：WrenAI
- **工具协议**：Model Context Protocol（MCP）

---

## 2. 系统架构

### 2.1 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        前端（Next.js）                           │
│  对话界面 │ 进度条 │ 图表渲染 │ 报告预览 │ 数据库选择            │
└────────────────────────────┬────────────────────────────────────┘
                             │ SSE / REST API
┌────────────────────────────▼────────────────────────────────────┐
│                   LangGraph API Server (:2026)                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                  主智能体（Orchestrator）                  │   │
│  │  意图识别 → 任务路由 → 异步委派 → 图表渲染 → 报告生成     │   │
│  │                                                           │   │
│  │  工具集: Semiotic/ECharts MCP │ report-export 技能        │   │
│  └──────────────────┬────────────────────────────────────────┘   │
│                     │ start_async_task                            │
│  ┌──────────────────▼────────────────────────────────────────┐   │
│  │              NL2SQL 子智能体（独立线程）                    │   │
│  │  SQL-of-Thought 7步推理管线                                │   │
│  │                                                           │   │
│  │  工具集: WrenAI MCP (18个工具)                             │   │
│  └──────────────────┬────────────────────────────────────────┘   │
│                     │                                             │
│  ┌──────────────────▼────────────────────────────────────────┐   │
│  │               后台同步进程（守护线程）                      │   │
│  │  子智能体进度 → 合并 → 主智能体 state → 前端进度条         │   │
│  └───────────────────────────────────────────────────────────┘   │
└────────────────────────────┬────────────────────────────────────┘
                             │ MCP 协议 (stdio)
┌────────────────────────────▼────────────────────────────────────┐
│                      外部服务层                                   │
│  ┌────────────┐  ┌──────────────────┐  ┌────────────────────┐   │
│  │ WrenAI CLI │  │ Semiotic/ECharts │  │ DB MCP Server      │   │
│  │ 语义层引擎  │  │   图表渲染引擎    │  │ (MySQL/SQLite/...) │   │
│  └──────┬─────┘  └──────────────────┘  └────────┬───────────┘   │
│         │                                        │               │
│  ┌──────▼────────────────────────────────────────▼───────────┐   │
│  │              数据库层（MySQL / SQLite / ...）               │   │
│  └───────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 核心组件

| 组件 | 技术栈 | 职责 |
|------|--------|------|
| **前端** | Next.js 16 + React 19 + TypeScript + Tailwind CSS | 对话界面、进度条、图表渲染、报告预览 |
| **LangGraph API** | langgraph-api ≥ 0.11.1 (uvicorn) | 线程管理、状态检查点、SSE 流式输出 |
| **主智能体** | DeepAgents + LangGraph | 意图路由、异步委派、图表渲染、报告生成 |
| **NL2SQL 子智能体** | DeepAgents + LangGraph | SQL-of-Thought 7 步管线执行 |
| **后台同步进程** | Python asyncio + threading | 子智能体进度同步到主状态 |
| **WrenAI MCP Server** | WrenAI CLI (stdio) | 语义层查询、Schema 发现、Cube 指标 |
| **图表 MCP Server** | Semiotic / ECharts (stdio) | 图表推荐与渲染 |
| **DB MCP Server** | FastMCP ≥ 3.4.4 (HTTP) | 多数据库 SQL 执行 |

### 2.3 数据流

```
用户输入自然语言
    │
    ▼
主智能体: 意图识别
    │
    ├── 通用问答 → 直接回复
    ├── 文件操作 → 文件系统工具
    │
    └── 数据查询 → start_async_task(nl2sql)
                      │
                      ├── 子智能体: SQL-of-Thought 管线
                      │     Step 1: 知识加载 (knowledge.json)
                      │     Step 2: Schema链接 (schema.json)
                      │     Step 3: 子问题分解 (subproblem.json)
                      │     Step 4: 查询计划 (query_plan.txt)
                      │     Step 5: SQL生成 + dry_run验证
                      │     Step 5.5: 性能优化审计
                      │     Step 6: run_sql 执行
                      │     Step 7: 纠错 (如需, 最多3轮)
                      │
                      └── 结果返回主智能体
                            │
                            ├── 推荐图表类型
                            ├── 渲染图表 (SVG/HTML)
                            └── 生成 Markdown 报告
```

---

## 3. 核心技术

### 3.1 SQL-of-Thought 方法论

基于 2025 年学术论文 *"SQL-of-Thought: Multi-agentic Text-to-SQL with Guided Error Correction"*，系统实现了以下核心设计原则：

| 原则 | 说明 |
|------|------|
| **分阶段推理不可跳过** | Query Plan 必须先于 SQL 生成，避免提前锚定 |
| **Query Plan 禁止输出 SQL** | 纯文本推理步骤，确保思维链不受 SQL 语法干扰 |
| **分类学引导纠错** | 结构化错误分类（9 大类 31 小类）诊断"为什么错"而非"哪里错" |
| **温度必须为 0** | 所有 LLM 调用使用 temperature=0，确保确定性输出 |
| **纠错不共享历史** | 每轮纠错从新的上下文开始，避免错误累积 |
| **结构化推理先于 SQL 再生** | 纠错计划智能体在错误检测与修复之间介入 |

### 3.2 三策略智能路由

系统根据查询复杂度自动选择最优执行路径：

| 策略 | 触发条件 | 管线路径 | 典型耗时 |
|------|---------|---------|---------|
| **A: 标准管线** | 复杂多表 JOIN / 聚合 / 窗口函数 | 完整 7 步 (Steps 1-7) | 2-5 分钟 |
| **B: 快速通道** | 单表 / 简单筛选 / COUNT / ORDER BY LIMIT | Steps 1, 5, 6 | 30-60 秒 |
| **C: Cube 通道** | 匹配预定义 Cube 指标 | list_cubes → describe_cube → query_cube | 10-20 秒 |

策略选择由 `sql-of-thought` 编排技能在 Step 1 完成后自动判断。

### 3.3 MCP 协议集成

系统通过 **Model Context Protocol (MCP)** 实现智能体与外部工具的标准化集成：

```
智能体 ←→ MCP Adapter ←→ MCP Server ←→ 外部服务
              (langchain_mcp_adapters)
```

**已集成的 MCP Server：**

| MCP Server | 传输方式 | 工具数量 | 功能 |
|-----------|---------|---------|------|
| WrenAI CLI | stdio | 18 | 语义层查询、Schema 发现、Cube 指标、历史查询召回 |
| Semiotic MCP | stdio | ~15 | 图表推荐、渲染（SVG）、交互式图表 |
| ECharts MCP | stdio | 1 | 图表生成（SVG/PNG），支持 5 种图表类型 |
| DB MCP Server | HTTP | 2 | 多数据库 SQL 执行、数据库信息查询 |

### 3.4 技能系统（Progressive Disclosure）

系统采用**渐进式披露**的技能加载策略：

1. **系统提示词**仅注入技能名称和简要描述（~50 tokens/技能）
2. 智能体根据任务需要，按需调用 `read_file` 读取完整 SKILL.md
3. 每个 SKILL.md 包含详细的执行步骤、输入输出规范、示例和约束

**优势：** 9项技能的完整内容约 15,000 tokens，但系统提示词中仅占 ~550 tokens。智能体按需加载，避免上下文膨胀。

---

## 4. SQL-of-Thought 推理管线

### 4.1 管线总览

```
Phase 1: 业务知识预处理
  Step 1 → Knowledge Loader
           输入: 用户问题 + 知识库文件
           输出: knowledge.json (过滤后的业务规则/指标/陷阱)

Phase 2: SQL-of-Thought 主流程
  Step 2 → Schema Linking
           输入: knowledge.json + 用户问题
           工具: list_models, describe_model, get_mdl, get_context
           输出: schema.json (最小化相关Schema)

  Step 3 → Subproblem Decomposition
           输入: schema.json + 用户问题
           输出: subproblem.json (子句级子问题分解)

  Step 4 → Query Plan Generation
           输入: subproblem.json + schema.json
           输出: query_plan.txt (纯文本推理步骤，禁止SQL)

  Step 5 → SQL Generation
           输入: query_plan.txt
           工具: dry_run (验证)
           输出: sql.sql (可执行SQL，最多3次修复)

  Step 5.5 → Performance Optimization
             输入: sql.sql
             输出: optimization.json (10条规则审计报告)

  Step 6 → SQL Execution
           工具: run_sql
           结果: 成功 → 返回数据 / 失败 → Phase 3

Phase 3: 分类学纠错 (条件触发)
  Step 7 → Correction
           输入: 错误信息 + 分类学体系
           流程: 错误分类 → CoT诊断 → 纠错计划 → SQL修复
           循环: 最多3轮
```

### 4.2 错误分类学体系

系统内置 9 大类 31 小类的 SQL 错误分类体系：

| 大类 | 小类示例 | 说明 |
|------|---------|------|
| **JOIN 错误** | join_missing, join_wrong_type, join_extra | 表关联缺失/类型错误/多余关联 |
| **聚合错误** | agg_no_groupby, agg_wrong_func, agg_misplaced | 缺少 GROUP BY/函数错误/位置错误 |
| **筛选错误** | where_wrong_column, where_wrong_operator | 筛选列/操作符错误 |
| **排序错误** | order_wrong_column, order_wrong_direction | 排序列/方向错误 |
| **子查询错误** | subquery_correlated, subquery_scope | 相关子查询/作用域错误 |
| **类型错误** | type_mismatch, type_cast_missing | 类型不匹配/缺少类型转换 |
| **语法错误** | syntax_keyword, syntax_bracket | 关键字/括号语法错误 |
| **逻辑错误** | logic_negation, logic_quantifier | 否定/量词逻辑错误 |
| **语义错误** | semantic_ambiguity, semantic_unit | 语义歧义/单位错误 |

### 4.3 性能优化规则

SQL 执行前自动检查 10 条性能规则：

| # | 规则 | 风险 |
|---|------|------|
| 1 | SELECT * | 返回多余列，增加网络传输 |
| 2 | 无 LIMIT | 全表扫描，可能 OOM |
| 3 | 笛卡尔积 JOIN | 数据量指数级膨胀 |
| 4 | 函数包裹索引列 | 索引失效，全表扫描 |
| 5 | NOT IN (含 NULL) | 结果集异常 |
| 6 | DISTINCT 滥用 | 不必要的去重排序 |
| 7 | LIKE '%prefix' | 索引失效 |
| 8 | OR 条件未优化 | 全表扫描 |
| 9 | LIMIT 无 ORDER BY | 结果不确定性 |
| 10 | 子查询未改写 JOIN | 执行效率低 |

---

## 5. 多智能体协作机制

### 5.1 架构设计

```
┌──────────────────────────────────────────┐
│           主智能体 (chat_agent)            │
│                                           │
│  ┌─────────┐  ┌─────────┐  ┌──────────┐  │
│  │ 意图识别 │→│ 任务路由 │→│ 结果聚合  │  │
│  └─────────┘  └────┬────┘  └──────────┘  │
│                    │                       │
│  工具: 图表MCP │ 报告技能 │ 文件系统       │
└────────────────────┼──────────────────────┘
                     │ start_async_task
                     ▼
┌──────────────────────────────────────────┐
│        NL2SQL 子智能体 (nl2sql_agent)      │
│        (独立线程, 独立图)                  │
│                                           │
│  知识→Schema→子问题→计划→SQL→执行→纠错    │
│                                           │
│  工具: WrenAI MCP (18个工具)              │
└──────────────────────────────────────────┘
                     │
         ┌───────────┼───────────┐
         │    后台同步进程        │
         │  进度合并 → 主状态    │
         └───────────────────────┘
```

### 5.2 异步执行与进度追踪

1. **启动**：主智能体调用 `start_async_task`，子智能体在独立线程启动
2. **同步**：后台守护进程每 0.5s 读取子智能体 `write_todos`，合并写入主智能体 state
3. **展示**：前端每 2s 轮询主智能体 state，渲染进度条（步骤 + 状态 + 耗时）
4. **完成**：子智能体完成后，主智能体自动继续后续步骤

### 5.3 状态管理

主智能体扩展状态包含三个独立字段：

| 字段 | 写入方 | 说明 |
|------|--------|------|
| `todos` | 主智能体 (write_todos) | 主任务列表（查询/绘图/报告） |
| `query_header` | 同步进程 | 用户查询标题（不被 write_todos 覆盖） |
| `subagent_steps` | 同步进程 | 子智能体步骤 + 耗时（不被 write_todos 覆盖） |

---

## 6. 知识库与语义层

### 6.1 WrenAI 语义层

系统通过 WrenAI 引擎构建**业务语义层**，将物理数据库表结构映射为业务概念：

```
物理层:  tables, columns, foreign_keys
   ↕ MDL (Model Definition Language)
语义层:  models, metrics, dimensions, relationships
```

**核心能力：**

| 能力 | MCP 工具 | 说明 |
|------|---------|------|
| MDL 建模 | `get_mdl()` | 获取完整模型定义 |
| Schema 发现 | `list_models()`, `describe_model()` | 列出模型、查看列定义 |
| 语义检索 | `get_context(question)` | 基于问题语义检索相关 Schema |
| 历史召回 | `recall_queries(question)` | 检索相似历史 NL→SQL 对 |
| Cube 指标 | `list_cubes()`, `query_cube()` | 预定义指标直接查询 |
| SQL 方言转换 | `dry_plan(sql)` | 语义 SQL → 目标方言 |
| SQL 验证 | `dry_run(sql)` | 语法验证不执行 |
| SQL 执行 | `run_sql(sql)` | 通过语义层执行查询 |

### 6.2 知识库结构

```
knowledge/
├── rules/
│   ├── business_rules.md      # 业务规则定义
│   ├── metrics_definitions.md  # 指标计算口径
│   └── field_mappings.md       # 字段映射关系
├── sql/
│   ├── historical_qna.json     # 历史优质 NL→SQL 对
│   └── common_patterns.sql     # 常见查询模式
└── caveats/
    └── common_pitfalls.md      # 常见陷阱与注意事项
```

### 6.3 技能体系

**主智能体技能（3 项）：**

| 技能 | 功能 |
|------|------|
| main-agent | 意图分类、子智能体选择、委派策略、安全约束 |
| report-export | 3 种报告模板、5 段式结构、文件命名规范 |

**NL2SQL 子智能体技能（8 项）：**

| 技能 | 管线步骤 | 功能 |
|------|---------|------|
| sql-of-thought | 编排器 | 3 策略路由（标准/快速/Cube） |
| nl2sql-knowledge-loader | Step 1 | 业务规则、指标、字段映射加载 |
| nl2sql-schema-linking | Step 2 | Schema 发现与裁剪 |
| nl2sql-subproblem | Step 3 | 子句级子问题分解 |
| nl2sql-query-plan | Step 4 | CoT 查询计划推理 |
| nl2sql-sql-generation | Step 5 | SQL 生成 + dry_run 验证 |
| nl2sql-performance-optimization | Step 5.5 | 10 条性能规则审计 |
| nl2sql-correction | Step 7 | 分类学引导纠错 |

---

## 7. 可视化与报告生成

### 7.1 图表引擎

系统支持两种图表引擎，通过 `CHART_ENGINE` 环境变量切换：

| 特性 | Semiotic | ECharts |
|------|---------|---------|
| 输出格式 | SVG | SVG / PNG |
| 图表类型 | BarChart, LineChart 等 | bar, line, pie, scatter, area |
| 智能推荐 | `suggestCharts(data)` | 手动配置 |
| 交互式 | `renderInteractiveChart` | — |
| Schema 发现 | `getSchema(component)` | ECharts 文档 |

### 7.2 报告模板

系统内置 3 种 Markdown 报告模板：

| 模板 | 结构 |
|------|------|
| **数据查询报告** | 概览 → 数据表格 → SQL→关键发现 |
| **分析报告** | 背景 → 分析结果 → SQL→结论与建议 |
| **图表报告** | 数据概览 → 图表描述 → 详细数据 → SQL→管线追踪 → 统计信息 |

**标准 5 段式结构：**
1. 概览（查询意图 + 结果摘要）
2. 核心数据（GFM 表格）
3. 生成的 SQL（代码块）
4. 分析与解读
5. 附录（原始数据、SQL 等）

---

## 8. 部署架构

### 8.1 部署拓扑

```
┌─────────────────────────────────────────────┐
│              企业内网服务器                    │
│                                              │
│  ┌──────────────┐    ┌──────────────────┐    │
│  │ 前端 (Next.js)│    │ 后端 (:2026)     │    │
│  │ nginx 代理    │←──→│ langgraph_api    │    │
│  └──────────────┘    └───────┬──────────┘    │
│                              │               │
│  ┌───────────────────────────▼────────────┐  │
│  │           本地服务                      │  │
│  │  WrenAI CLI │ Chart MCP │ DB MCP       │  │
│  └───────────────────────────┬────────────┘  │
│                              │               │
│  ┌───────────────────────────▼────────────┐  │
│  │         数据库集群                      │  │
│  │  MySQL │ SQLite │ PostgreSQL │ ...     │  │
│  └────────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
         ↕ 仅 LLM API 调用
┌─────────────────────────────────────────────┐
│         云端 LLM 服务                        │
│  DeepSeek API │ 通义千问 API │ OpenAI API   │
└─────────────────────────────────────────────┘
```

### 8.2 环境要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Linux / Windows / macOS |
| Python | ≥ 3.13 |
| Node.js | ≥ 18 (前端构建) |
| 内存 | ≥ 4GB |
| 磁盘 | ≥ 10GB |
| 网络 | 需访问 LLM API（DeepSeek/通义千问） |

---

## 9. 安全与可靠性

### 9.1 数据安全

| 措施 | 说明 |
|------|------|
| **私有化部署** | 系统部署在企业内网，数据不出内网 |
| **SQL 验证** | `dry_run` 预验证，防止危险 SQL 执行 |
| **权限隔离** | 数据库连接使用只读账号，禁止 DDL/DML |
| **LLM 隔离** | 仅发送 Schema 元数据和查询问题，不发送实际数据 |

### 9.2 自定义沙箱执行环境

系统支持基于 **OpenSandbox** 的自定义沙箱，为智能体的代码执行提供容器级隔离环境：

```
智能体代码执行请求
    │
    ▼
OpenSandboxBackend (自定义实现)
    │
    ▼
OpenSandbox Docker 容器
┌─────────────────────────────────────┐
│  镜像: code-interpreter:v1.0.2      │
│  资源: CPU 2核 / 内存 4Gi           │
│  超时: N小时自动销毁                 │
│                                     │
│  预装环境:                           │
│  ├── Python 3.11 + venv             │
│  │   └── numpy, pandas, matplotlib  │
│  ├── Go 1.25.5                      │
│  ├── Node.js v22.2.0                │
│  └── Java 21 (OpenJDK)              │
│                                     │
│  网络策略:                           │
│  ├── 默认拒绝所有出站流量            │
│  ├── 白名单: pypi.org (pip安装)     │
│  └── 白名单: *.github.com           │
│                                     │
│  文件系统:                           │
│  ├── /analysis/ (工作目录)           │
│  └── /skills/ (技能文件, 自动播种)   │
└─────────────────────────────────────┘
```

**核心安全特性：**

| 特性 | 说明 |
|------|------|
| **容器隔离** | 每次执行在独立 Docker 容器中运行，进程/文件/网络完全隔离 |
| **资源限制** | CPU 2核、内存 4Gi 硬限制，防止资源耗尽 |
| **超时销毁** | 容器 N 小时自动销毁，防止僵尸进程 |
| **网络管控** | 支持自定义网络策略（默认拒绝 + 白名单），限制出站流量 |
| **环境注入** | PATH 自动注入，Python/Go/Node/Java 多语言环境开箱即用 |
| **依赖预装** | Python venv 隔离 + 预装常用包（numpy, pandas, matplotlib 等） |
| **文件播种** | 技能文件自动同步到沙箱，增量更新避免重复上传 |
| **路径防护** | 文件操作限制绝对路径，防止路径遍历攻击 |

**自定义扩展能力：**

- 支持自定义 Docker 镜像（`image` 参数），适配企业内部基础镜像
- 支持连接已有沙箱实例（`sandbox_id` 参数），复用预热环境
- 支持自定义网络策略（`NetworkPolicy`），精细控制出站规则
- 支持自定义资源配额（`resource` 参数），按需调整 CPU/内存

### 9.3 可靠性

| 措施 | 说明 |
|------|------|
| **状态检查点** | SQLite/PostgreSQL 检查点，会话可恢复 |
| **自动纠错** | 最多 3 轮分类学纠错，提升 SQL 成功率 |
| **超时控制** | LLM 调用 60s 超时 + 3 次重试 |
| **子智能体隔离** | 子智能体在独立线程运行，异常不影响主智能体 |
| **沙箱超时保护** | 命令级超时（默认 1h）+ 容器级超时（2h），双重保障 |

---

## 10. 技术规格

### 10.1 量化指标

| 指标 | 数值 |
|------|------|
| 智能体数量 | 2（主编排 + NL2SQL 子智能体） |
| 技能数量 | 9 项 SKILL.md |
| MCP 工具总数 | 36+（WrenAI 18 + 图表 ~15 + DB 2 + 文件 ~5） |
| 支持数据库引擎 | 10 种（MySQL, SQLite, PostgreSQL, BigQuery, Snowflake, MSSQL, Oracle, ClickHouse, Presto, DuckDB） |
| SQL 错误分类 | 9 大类 31 小类 |
| 性能审计规则 | 10 条 |
| 最大纠错轮数 | 3 轮 |
| 执行策略 | 3 种（标准/快速/Cube） |
| 报告模板 | 3 种 |
| 图表引擎 | 2 种（Semiotic / ECharts） |
| LLM 上下文窗口 | 120,000 tokens |
| LangGraph 递归上限 | 500 |
| 进度同步频率 | 0.5 秒 |

### 10.2 依赖版本

| 依赖 | 版本要求 |
|------|---------|
| Python | ≥ 3.13 |
| deepagents | ≥ 0.6.12 |
| langgraph-api | ≥ 0.11.1 |
| langchain-deepseek | ≥ 1.1.0 |
| langchain-openai | ≥ 1.3.5 |
| fastmcp | ≥ 3.4.4 |
| wrenai | ≥ 0.13.0 |
| pandas | ≥ 3.0.3 |
