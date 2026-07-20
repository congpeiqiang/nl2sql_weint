---
name: wrenai
description: "触发条件：用户希望使用WrenAI语义层增强NL2SQL流水线；用户想从数据库Schema生成MDL语义模型；用户想为Schema添加业务上下文（枚举含义、单位、指标定义）；用户想通过干运行计划（dry-plan）验证生成的SQL；用户提到WrenAI、语义层、MDL、governed SQL、业务上下文。本技能可作为SQL-of-Thought流水线的预处理器（Phase 0）和验证层（Phase 2.5）。跳过条件：用户已有完整的语义层方案；用户只需要纯Schema映射而不需要业务上下文；用户使用的是不支持WrenAI的数据库。"
---

# WrenAI 语义层集成

## 概述

WrenAI 是一个开源的 **GenBI 引擎**，提供语义 SQL 层，让 AI Agent 能够在 Schema 之外理解业务语义。它通过 **MDL（Model Definition Language）** 定义业务实体、关系、指标和上下文，使 NL2SQL 流水线生成的 SQL 更准确、更可信。

## 在 NL2SQL 流水线中的角色

```
                    Phase 0: WrenAI 预处理（可选）
                    ┌─────────────────────────────┐
                    │ wren generate-mdl           │ ← 从数据库生成 MDL
                    │ wren enrich-context         │ ← 添加业务上下文
                    │ wren context build/validate │ ← 构建验证 MDL
                    └──────────────┬──────────────┘
                                   │
                                   ▼
                    Phase 2: SQL-of-Thought 标准流水线
                    使用 MDL 语义上下文增强 Schema Linking
                                   │
                                   ▼
                    Phase 2.5: WrenAI 验证（可选）
                    ┌─────────────────────────────┐
                    │ wren dry-plan --sql '...'   │ ← 干运行验证 SQL
                    │ wren memory recall          │ ← 检索相似历史查询
                    └─────────────────────────────┘
```

## WrenAI vs 纯 Schema 方式

| 维度 | 纯 Schema (现有方式) | + WrenAI 语义层 |
|------|---------------------|-----------------|
| 表/列识别 | 基于自然语言模糊匹配 | MDL 中明确定义业务实体 |
| 业务含义 | 依赖 LLM 自行推断 | MDL 中显式声明枚举值、单位 |
| 指标定义 | 每次生成时重新推导 | MDL 中预定义指标/Cube |
| SQL 验证 | 仅执行后错误检查 | `dry-plan` 执行前语法语义验证 |
| 历史复用 | 无 | `memory recall` 检索相似查询 |

## 安装与设置

```bash
pip install wrenai
```

## 工作流指南

WrenAI 的工作流指南内置于 CLI 中：

```bash
wren skills list                        # 查看所有可用工作流
wren skills get onboarding              # 端到端设置
wren skills get generate-mdl            # 从数据库 Schema 生成 MDL
wren skills get dlt-connector           # 连接 SaaS 数据源
wren skills get enrich-context          # 添加业务上下文
wren skills get genbi                   # 构建部署 GenBI 仪表板
```

### Phase 0：预处理——建立语义层

**步骤 1：配置数据库连接**

```bash
wren profile add --name <project_name> --type <db_type>   --host <host> --port <port> --database <db_name>   --user <user> --password <password>
wren profile list
```

**步骤 2：从 Schema 生成 MDL**

```bash
wren generate-mdl --profile <project_name>
```

此命令分析数据库 Schema，自动生成 MDL 项目，包含：
- `models/` — 每个表的语义模型（表名、列定义、关系）
- `project.yaml` — 项目配置

**步骤 3：丰富业务上下文**

```bash
wren enrich-context --profile <project_name>
```

为 MDL 添加业务语义：
- 枚举值含义（如 `status = 'A'` 表示"活跃"）
- 列单位（如 `amount` 的单位是 CNY）
- 预定义指标 Cube（如 ARR、DAU、流失率）

**步骤 4：构建与验证**

```bash
wren context build --profile <project_name>
wren context validate
```

### Phase 2：增强 Schema Linking

在 NL2SQL Schema Linking 阶段，除了使用 `search_pages` 搜索原始 Schema 知识，还可结合 WrenAI 的 MDL 语义上下文：

1. 读取 MDL 模型文件获取表/列的**业务含义**
2. 使用 `wren context show` 查看完整的语义模型
3. 在 Schema Linking 推理时，将 MDL 中的业务定义作为附加上下文

### Phase 2.5：SQL 验证

在 SQL 生成后、数据库执行前，使用 WrenAI 进行干运行验证：

```bash
wren dry-plan --sql 'SELECT ...'
```

`dry-plan` 仅做 SQL 解析和语义验证，**不实际访问数据库**：
- 验证 SQL 语法正确性
- 检查引用的表/列在 MDL 中是否存在
- 验证 JOIN 关系是否合法
- 发现语义问题（如错误的枚举值引用）

### 语义记忆

WrenAI 内置语义记忆，可检索相似的历史查询：

```bash
wren memory recall --question "查询各部门的平均薪资"
# 返回：相似查询示例、正确 SQL、执行结果
```

在 NL2SQL 纠错阶段，可利用记忆检索避免重复错误。

## 集成到 SQL-of-Thought 流水线

### 完整流程

```
输入：NL 问题 + db_id
    │
    ▼
[Phase 0: WrenAI]
  wren generate-mdl            ← 有 MDL 则跳过
  wren enrich-context          ← 有业务上下文则跳过
    │
    ▼
[Phase 2: SQL-of-Thought]
  Step 1: Schema Linking       ← 使用 MDL 语义上下文增强
    search_pages + wren context show
  Step 2: Subproblem Decomp
  Step 3: Query Plan           ← 引用 MDL 中的指标定义
  Step 4: SQL Generation
    │
    ▼
[Phase 2.5: WrenAI Validation]
  wren dry-plan --sql '...'    ← 验证 SQL
    │                    │
  通过                 失败
    │                    │
    ▼                    ▼
[DB Execute]       [修正 SQL]
    │                    │
    ▼                    ▼
  返回结果          [Phase 3: 纠错]
                    可结合 wren memory recall
```

### 推荐策略

| 场景 | WrenAI 使用方式 |
|------|----------------|
| 首次使用数据库 | Phase 0 全程：generate-mdl → enrich-context → context build |
| 日常查询 | Phase 2.5：仅 dry-plan 验证 + memory recall |
| 复杂指标查询 | Phase 2：MDL 指标定义替代手工推导 |
| SQL 纠错 | Phase 3：memory recall 检索相似正确 SQL |

## 常见陷阱

| 陷阱 | 纠正方式 |
|------|---------|
| MDL 与 Schema 脱节 | 每次 Schema 变更后运行 `wren generate-mdl` 更新 |
| 忽略业务上下文 | 即使 Schema 不变，也定期 `wren enrich-context` 补充新业务知识 |
| dry-plan 通过但执行失败 | dry-plan 仅验证语义层定义，不验证数据存在性 |
| 过度依赖记忆 | memory recall 提供参考，但不能替代当前 Schema 的精准推理 |
| MDL 与 llmwiki 冲突 | MDL 关注业务语义层，llmwiki 关注 Schema 知识文档——两者互补不冲突 |

## 支持的数据库

WrenAI 支持 22+ 数据源：PostgreSQL、MySQL、BigQuery、Snowflake、Spark SQL、DuckDB、SQLite、Trino、Databricks、MS SQL Server、Oracle、ClickHouse 等。

```bash
wren docs connection-info postgres    # 查看特定数据源连接参数
wren docs connection-info sqlite
```
