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
+----------------------------------------------------------------------------+
|                           Agent Layer (主Agent)                              |
|  main_agent.py (11阶段初始化流水线)                                          |
|  config.py / schema.py / middleware_config.py                                |
+----------------------------------------------------------------------------+
|                           Middleware Stack (7层)                             |
|  context_injection -> skills_sync -> user_skills_restore ->                  |
|  tools_summarization -> memory_update -> sql_feedback ->                     |
|  query_router                                                                |
+----------------------------------------------------------------------------+
|                           Skills 技能系统                                    |
|  +---------------------+   +----------------------------------------+       |
|  | 通用技能             |   | WrenAI NL2SQL Skill                    |       |
|  | (分析/图表/搜索等)   |   |  +----------------------------------+ |       |
|  |                     |   |  | Model Layer: 语义模型             | |       |
|  |                     |   |  | View Layer: 指标/维度             | |       |
|  |                     |   |  | SQL Gen Layer: Text2SQL           | |       |
|  |                     |   |  | Execution Layer: 执行             | |       |
|  |                     |   |  | Training Pipe: 标注流水线          | |       |
|  |                     |   |  +----------------------------------+ |       |
|  +---------------------+   +----------------------------------------+       |
+----------------------------------------------------------------------------+
|                           Tools 工具层                                      |
|  mcp_client.py / sql_executor.py / chart_generator.py                       |
|  web_search / hitl_tools / assign_skill                                     |
+----------------------------------------------------------------------------+
|                     MCP Server (NL2SQL 业务接口)                             |
|  +----------------------------------------------------------------------+  |
|  |  db_metadata_tools.py    - 表结构/关系发现                             |  |
|  |  model_management_tools.py - 语义模型 CRUD                            |  |
|  |  nl2sql_tools.py         - NL->SQL->执行->返回                         |  |
|  |  training_data_tools.py  - 标注数据管理                                |  |
|  |  feedback_tools.py       - 反馈收集                                    |  |
|  +----------------------------------------------------------------------+  |
+----------------------------------------------------------------------------+
|                           Sandbox (数据隔离沙箱)                            |
|  - 每个用户的 SQL 查询在隔离沙箱中执行                                      |
|  - 只读数据库连接 / 资源限制 / 超时控制                                    |
+----------------------------------------------------------------------------+
|                           WrenAI Engine (外部服务)                          |
|  - 语义模型解析 -> SQL 生成                                                |
|  - 通过 HTTP API 集成                                                      |
+----------------------------------------------------------------------------+
|                           API 层 (FastAPI)                                  |
|  web_main.py / agent_loader.py                                             |
|  api/chat.py (SSE流式) / api/nl2sql.py (专用端点)                          |
+----------------------------------------------------------------------------+
|                           Memory / 持久化层                                  |
|  MongoDB: 会话历史 / SQL 查询缓存 / 标注数据 / 反馈记录                     |
|  StoreBackend: 用户技能和偏好持久化                                        |
+----------------------------------------------------------------------------+
```

---

## 项目目录结构

```
nl2sql-harness/
+-- src/
|   +-- agent/                              # Agent 核心
|   |   +-- main_agent.py                   # 主 Agent 入口 (11阶段初始化流水线)
|   |   +-- config.py                       # 全局配置 (模型/沙箱/MongoDB/WrenAI)
|   |   +-- schema.py                       # 数据结构 Schema
|   |   +-- env_utils.py                    # 环境变量读取
|   |   +-- middleware_config.py            # 子 Agent 中间件工厂
|   |   +-- memory/
|   |   |   +-- AGENTS.md                   # 主 Agent 通用行为准则
|   |   |   +-- prompts.py                  # 主 Agent system_prompt
|   |   +-- backends/
|   |   |   +-- sandbox_setup.py            # 沙箱创建 + 数据库播种
|   |   |   +-- custom_opensandbox.py       # 沙箱后端封装 (含只读DB)
|   |   +-- middlewares/                    # 中间件栈 (7层)
|   |   |   +-- context_injection.py        # 1. 注入用户上下文 + 数据库Schema
|   |   |   +-- skills_sync.py              # 2. 技能同步 (含WrenAI模型同步)
|   |   |   +-- user_skills_restore.py      # 3. StoreBackend 恢复持久化技能
|   |   |   +-- tools_summarization.py      # 4. 工具摘要封装
|   |   |   +-- memory_update.py            # 5. 自动更新查询历史/数据域偏好
|   |   |   +-- sql_feedback.py             # 6. NL2SQL 反馈闭环
|   |   |   +-- query_router.py             # 7. 查询路由 (NL->哪个Skill)
|   |   +-- subagents/                      # 子 Agent 配置
|   |   |   +-- loader.py                   # YAML 配置加载
|   |   |   +-- configs/
|   |   |       +-- data_analyst.yaml       # 数据分析子Agent (调WrenAI)
|   |   |       +-- data_steward.yaml       # 数据管理员 (模型维护/标注)
|   |   |       +-- sql_reviewer.yaml       # SQL 审核子Agent
|   |   +-- tools/
|   |       +-- mcp_client.py               # MCP 多服务器连接
|   |       +-- sql_executor.py             # SQL 执行器 (沙箱隔离)
|   |       +-- hitl_tools.py               # Human-in-the-Loop: SQL确认/修正
|   |       +-- chart_generator.py          # 图表生成 (SQL结果->可视化)
|   |       +-- assign_skill.py             # 技能分配
|   |       +-- download_sandbox_file.py    # 沙箱文件下载到本地
|   |
|   +-- skills/                             # 技能资源
|   |   +-- main/skill-management/          # 技能生命周期管理
|   |   +-- nl2sql/                         # WrenAI NL2SQL 技能
|   |   |   +-- SKILL.md                    # 技能描述: 调用时机/参数/注意事项
|   |   |   +-- model_definitions/          # 语义模型定义 (YAML)
|   |   |   |   +-- erp_sales.yaml          # 销售域语义模型
|   |   |   |   +-- erp_inventory.yaml      # 库存域语义模型
|   |   |   |   +-- erp_finance.yaml        # 财务域语义模型
|   |   |   +-- view_definitions/           # 指标/维度定义 (WrenAI View层)
|   |   |   |   +-- revenue_views.yaml
|   |   |   |   +-- inventory_views.yaml
|   |   |   +-- sql_templates/              # SQL 模板 + few-shot 样本
|   |   |   |   +-- aggregation_patterns.md
|   |   |   |   +-- join_patterns.md
|   |   |   +-- training_data/              # 标注数据集
|   |   |       +-- nl2sql_pairs.jsonl
|   |   +-- common/
|   |   |   +-- data_dictionary.md          # 数据字典
|   |   |   +-- sql_style_guide.md          # SQL 书写规范
|   |   +-- data/
|   |       +-- schema-discovery/           # 数据库Schema自动发现
|   |       +-- query-cache/                # SQL 查询缓存策略
|   |
|   +-- wrenai_engine/                      # WrenAI 核心封装
|   |   +-- __init__.py
|   |   +-- wrenai_client.py                # WrenAI API 客户端包装
|   |   +-- semantic_model.py               # 语义模型管理 (CRUD)
|   |   +-- sql_generator.py                # SQL 生成器 (调用WrenAI语义层)
|   |   +-- sql_validator.py                # SQL 验证器 (语法+语义+安全)
|   |   +-- result_interpreter.py           # 结果解释 (自然语言转述SQL结果)
|   |   +-- training_pipeline.py            # 标注数据->微调/优化流水线
|   |   +-- feedback_collector.py           # 用户反馈收集 (喜欢/不喜欢/修正SQL)
|   |
|   +-- mcp_server/                         # MCP Server (NL2SQL 业务接口代理)
|   |   +-- server_main.py                  # MCP 服务入口
|   |   +-- tools/
|   |       +-- db_metadata_tools.py         # 数据库元数据: get_schema/list_tables
|   |       +-- model_management_tools.py    # 语义模型管理: create/update/list_models
|   |       +-- nl2sql_tools.py             # NL2SQL 核心: ask/explain/refine
|   |       +-- training_data_tools.py      # 标注数据 CRUD
|   |       +-- feedback_tools.py           # 反馈收集
|   |
|   +-- api_view/                           # FastAPI 层 (纯后端接口)
|   |   +-- web_main.py                     # FastAPI 入口 (lifespan/路由/CORS)
|   |   +-- agent_loader.py                 # Agent 懒加载 + MongoDB 管理
|   |   +-- web_config.py                   # API 元信息
|   |   +-- api/
|   |       +-- chat.py                     # SSE 流式对话 (含NL2SQL流式返回)
|   |       +-- nl2sql.py                   # NL2SQL 专用端点 (非流式)
|   |       +-- models.py                   # 语义模型管理 API
|   |       +-- training.py                 # 标注数据 API
|   |       +-- history.py                  # 历史会话管理 (MongoDB CRUD)
|   |
|   +-- test/
|       +-- test_nl2sql_pipeline.py         # 端到端 NL2SQL 测试
|       +-- test_wrenai_client.py           # WrenAI 客户端单元测试
|       +-- test_data/sample_queries.json   # 测试数据集
|
+-- config/                                 # 配置目录
|   +-- agent.yaml                          # Agent 主配置
|   +-- wrenai.yaml                         # WrenAI 配置 (端点/模型/语义模型路径)
|   +-- databases.yaml                      # 目标数据库连接配置
|   +-- sandbox.yaml                        # 沙箱配置
|
+-- scripts/                                # 运维脚本
|   +-- init_semantic_models.py             # 初始化语义模型
|   +-- bootstrap_db.py                     # 播种测试数据
|   +-- train_pipeline.py                   # 标注数据->微调流水线
|
+-- requirements.txt
+-- Dockerfile
+-- docker-compose.yml                      # 含 WrenAI 服务编排
+-- README.md
```

---

## 关键设计决策

### 1. WrenAI 作为 Skill 的工作流

```
用户输入 -> 主 Agent -> Query Router 中间件判断意图
                                |
                        +-------+-------+
                        v               v
                  数据查询类问题     业务操作类问题
                        |               |
                        v               v
                调用 nl2sql Skill    调用其他技能
                        |
                        v
           wrenai_engine/sql_generator.py 处理
               +- 匹配语义模型 (erp_sales.yaml / erp_finance.yaml)
               +- 构造 WrenAI MDL (Modeling Definition Language) 请求
               +- 调用 WrenAI API -> 返回 SQL
               +- sql_validator 校验语法 + 安全规则
               +- 沙箱执行 -> 结果 -> result_interpreter 转自然语言
                        |
                        v
              主 Agent 整合结果 -> 返回用户
```

### 2. Harness Engineering 映射

| ERP_OPENCLAW 概念 | 本项目映射 |
|---|---|
| main_agent.py 11阶段初始化 | 同样保留，增加 WrenAI Client 初始化阶段 |
| context_injection 中间件 | 注入 DB Schema + 数据字典到上下文 |
| memory_update 中间件 | 更新 frequent_queries、用户偏好的数据域 |
| **新增** sql_feedback 中间件 | **闭环反馈：用户反馈 -> 标注数据 -> 触发重训练** |
| **新增** query_router 中间件 | **判断 NL 是"查询数据"还是"操作业务"，路由到不同 Skill** |
| subagent procurement_analyst | -> data_analyst (调WrenAI做数据分析) |
| subagent procurement_order | -> data_steward (管理语义模型/标注数据) |
| 工具 chart_generator | 复用，输入从 SQL 结果改为 NL2SQL 执行结果 |
| 工具 hitl_tools | -> SQL 确认/修正 HITL，关键 SQL 执行前让人确认 |
| MCP 工具 suppliers_tools | -> nl2sql_tools (ask/explain/refine 三个核心) |
| Sandbox 沙箱 | 只读数据库沙箱，每个请求独立连接，自动超时 |

### 3. SQL 安全沙箱

NL2SQL 项目最关键的安全层设计：

```python
# sandbox_setup.py 中的核心策略
class SQLSandboxConfig:
    read_only: True                    # 只读事务
    statement_timeout: 30              # 单条SQL超时30秒
    max_rows_returned: 1000            # 最大返回行数
    allowed_schemas: ["public"]        # 只允许查询指定 schema
    forbidden_patterns: [              # 禁止执行的 SQL 模式
        "DROP", "ALTER", "TRUNCATE",
        "INSERT", "UPDATE", "DELETE",
        "CREATE", "GRANT", "EXECUTE"
    ]
    resource_limits: {
        "max_memory_mb": 512,
        "max_temp_tables": 0
    }
```

### 4. 反馈闭环 (核心差异化点)

```
用户查询 -> NL2SQL -> 执行 -> 返回结果
                         |
                   用户反馈: 点赞/踩/修正SQL
                         |
                   feedback_collector.py
                         |
                   存储到 training_data/nl2sql_pairs.jsonl
                         |
                   training_pipeline.py (定时/手动触发)
                         |
                   优化 few-shot 样本 / 微调模型参数
                         |
                   下次查询效果提升
```

### 5. WrenAI 概念映射到项目中

| WrenAI 概念 | 项目中的位置 | 说明 |
|---|---|---|
| **Model** (语义模型) | skills/nl2sql/model_definitions/*.yaml | 描述业务对象和关系的 DSL |
| **View** (指标/维度) | skills/nl2sql/view_definitions/*.yaml | 预定义的业务分析视角 |
| **Metrics** (度量) | 嵌入在 View 定义中 | 聚合函数和计算字段 |
| **SQL Generation** | wrenai_engine/sql_generator.py | 调用 WrenAI 语义解析 API |
| **MDL** (建模语言) | MCP model_management_tools 管理 | 创建/更新/删除语义模型 |

### 6. 技术栈

```
Python 3.11+                 # 与参考项目一致
FastAPI + Uvicorn            # API 层
Pydantic v2                  # Schema 定义
WrenAI Engine / WrenAI SDK   # NL2SQL 核心
OpenSandbox / E2B            # 沙箱执行环境
MongoDB                      # 记忆/历史/缓存/标注数据
Docker Compose               # 服务编排 (含 WrenAI Server)
LangChain / LiteLLM          # Agent LLM 调用 (可选)
SQLAlchemy 2.0 + asyncpg     # 数据库连接
```

---

## 实现路线图

### Phase 1 - 骨架搭建 (1-2天)
- 创建项目目录结构
- 编写 config.py、schema.py、env_utils.py
- 搭建 main_agent.py 初始化流水线 (空壳)
- 搭建 web_main.py + FastAPI 基础框架
- 编写 docker-compose.yml 编排 WrenAI Server

### Phase 2 - WrenAI 集成核心 (2-3天)
- 实现 wrenai_engine/wrenai_client.py - 封装 WrenAI API 调用
- 实现 wrenai_engine/semantic_model.py - 语义模型 CRUD
- 实现 wrenai_engine/sql_generator.py - 核心 SQL 生成逻辑
- 实现 wrenai_engine/sql_validator.py - 安全校验
- 定义首批语义模型 (model_definitions/*.yaml)

### Phase 3 - MCP + Skill 系统 (2天)
- 实现 mcp_server/server_main.py + 所有 MCP 工具
- 编写 skills/nl2sql/SKILL.md - 技能定义
- 集成 agent/tools/mcp_client.py
- 实现 middlewares/query_router.py 查询路由

### Phase 4 - 沙箱 + 安全 (1天)
- 实现 backends/sandbox_setup.py - 只读沙箱
- 实现 SQL 执行器 + 超时/限流/安全拦截

### Phase 5 - 反馈闭环 (1-2天)
- 实现 wrenai_engine/feedback_collector.py
- 实现 middlewares/sql_feedback.py
- 实现标注数据存储 + API
- 实现 wrenai_engine/training_pipeline.py

### Phase 6 - 优化 & 测试 (1天)
- 端到端测试
- few-shot 优化
- HITL (Human-in-the-Loop) 交互完善

---

## 待决策事项

1. **WrenAI 部署方式** - 自部署 WrenAI Server 还是使用 WrenAI Cloud？
2. **目标数据库** - MySQL / PostgreSQL / ClickHouse / 其他？
3. **标注数据来源** - 有现成的 NL-SQL 标注数据，还是边用边积累？
4. **SQL 输出形式** - 直接返回表格数据，还是 Agent 用自然语言解释后再展示？
