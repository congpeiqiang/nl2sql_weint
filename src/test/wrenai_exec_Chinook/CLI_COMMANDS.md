# WrenAI CLI 命令详解

> WrenAI — 开源 GenBI 引擎，语义 SQL 层
> 安装: `pip install wrenai`
> 帮助: `wren --help`

---

## 命令总览

```
wren
├── query           # 通过 MDL 语义层执行 SQL
├── dry-plan        # 干运行：仅解析 SQL，不访问数据库
├── dry-run         # 干运行：解析 + 验证，不返回数据
├── version         # 查看版本
│
├── profile         # 数据库连接管理
│   ├── list        #   列出所有连接
│   ├── add         #   添加连接
│   ├── import      #   从文件导入连接
│   ├── switch      #   切换当前连接
│   ├── remove      #   删除连接
│   └── show        #   查看当前连接详情
│
├── context         # MDL 项目管理
│   ├── init        #   初始化项目
│   ├── build       #   构建 → target/mdl.json
│   ├── validate    #   验证模型
│   ├── show        #   查看项目内容
│   ├── set-profile #   绑定数据库连接
│   ├── import      #   从 dbt 项目导入
│   ├── instructions#   输出业务规则
│   └── upgrade     #   升级 schema 版本
│
├── skills          # 工作流指南（内置于 CLI）
│   ├── list        #   列出所有指南
│   └── get         #   获取指定指南
│
├── docs            # 参考文档
│   └── connection-info  # 数据源连接参数
│
├── memory          # 语义记忆（需 [memory] extra）
│   ├── index       #   索引查询历史
│   ├── recall      #   检索相似查询
│   └── store       #   存储查询示例
│
├── ask             # AI Agent 问题包装
├── cube            # 指标 Cube 管理
├── utils           # 工具命令
├── genbi           # GenBI 仪表板部署
└── serve           # MCP 服务器
    └── mcp         #   启动 MCP 服务
```

---

## 一、query — SQL 查询

```bash
wren query --sql "<SQL>" [OPTIONS]
```

通过 WrenAI 语义层执行 SQL 查询。查询会经过 MDL 模型映射。

| 参数 | 说明 |
|------|------|
| `--sql, -s` | SQL 查询语句（必填） |
| `--connection-file` | 连接配置文件路径（JSON） |
| `--mdl` | MDL 文件路径 |
| `--limit` | 返回行数上限 |
| `--output, -o` | 输出格式：`table` / `json` / `csv` |
| `--quiet, -q` | 静默模式 |

**示例：**

```bash
# 基本查询
wren query --sql "SELECT * FROM Artist LIMIT 5"

# 指定连接文件
wren query --sql "SELECT COUNT(*) FROM Artist" --connection-file config/connection.json

# JSON 输出
wren query --sql "SELECT * FROM Album LIMIT 3" -o json
```

---

## 二、dry-plan — 干运行验证

```bash
wren dry-plan --sql "<SQL>" [OPTIONS]
```

仅解析 SQL，**不访问数据库**。用于验证 SQL 语法和语义正确性。

| 参数 | 说明 |
|------|------|
| `--sql, -s` | SQL 语句（必填） |
| `--datasource, -d` | 数据源类型（如 `duckdb`, `postgres`） |
| `--mdl` | MDL 文件路径 |
| `--connection-file` | 连接配置文件路径 |

**示例：**

```bash
# 验证 SQL（不访问数据库）
wren dry-plan --sql "SELECT e.FirstName, e.Title FROM Employee e" -d duckdb

# 带连接文件
wren dry-plan --sql "SELECT * FROM Artist" --connection-file config/connection.json -d duckdb
```

---

## 三、dry-run — 干运行

```bash
wren dry-run --sql "<SQL>" [OPTIONS]
```

解析 + 验证 SQL，**不返回数据**。如果需要访问数据库来验证列/表存在性，会比 `dry-plan` 更深入。

---

## 四、profile — 数据库连接管理

### 4.1 列出连接

```bash
wren profile list
```

显示所有已保存的数据库连接及其状态。

### 4.2 添加连接

```bash
wren profile add <name> [OPTIONS]
```

支持四种模式：

| 模式 | 参数 | 说明 |
|------|------|------|
| 交互 | `-i` / `--interactive` | 引导式提示输入 |
| UI 表单 | `--ui` | 浏览器表单填写 |
| 文件导入 | `--from-file <path>` | 从 JSON/YAML 导入 |
| 内联 | `-d <type>` | 指定数据源类型 |

**示例：**

```bash
# 交互模式
wren profile add mydb -i

# 从文件导入
wren profile add chinook --from-file config/connection.json

# 浏览器 UI
wren profile add mydb --ui
```

### 4.3 切换连接

```bash
wren profile switch <name>
```

### 4.4 删除连接

```bash
wren profile remove <name>
```

### 4.5 查看详情

```bash
wren profile show [name]
```

---

## 五、context — MDL 项目管理

### 5.1 初始化项目

```bash
wren context init [OPTIONS]
```

在当前目录创建 MDL 项目骨架：

```
├── wren_project.yml          # 项目配置
├── models/example/           # 示例模型
│   └── metadata.yml
├── views/example_view/       # 示例视图
│   ├── metadata.yml
│   └── sql.yml
├── relationships.yml         # 模型关系定义
├── knowledge/                # 业务知识
│   ├── rules/
│   ├── sql/
│   ├── glossary/
│   └── metrics/
└── AGENTS.md                 # AI Agent 工作流指导
```

### 5.2 构建

```bash
wren context build [OPTIONS]
```

验证模型并构建为 `target/mdl.json`（引擎可读取的格式）。

| 参数 | 说明 |
|------|------|
| `--validate / --no-validate` | 构建前是否验证（默认：是） |
| `--output, -o` | 输出路径（默认：`target/mdl.json`） |

### 5.3 验证

```bash
wren context validate [OPTIONS]
```

检查模型的完整性和一致性。

| 参数 | 说明 |
|------|------|
| `--strict` | 将 warning 视为 error |
| `--level` | 检查深度：`error` / `warning` / `strict` |
| `--verbose` | 显示详细信息 |

### 5.4 查看

```bash
wren context show [OPTIONS]
```

以不同格式展示当前项目内容。

| 参数 | 说明 |
|------|------|
| `--output, -o` | 格式：`summary` / `json` / `yaml` |

### 5.5 绑定连接

```bash
wren context set-profile <name>
```

将数据库连接 profile 绑定到当前项目。会将 profile 名和数据源类型写入 `wren_project.yml`。

### 5.6 输出业务规则

```bash
wren context instructions
```

输出 `knowledge/rules/*.md` 中的业务规则，供 LLM 消费。

---

## 六、skills — 工作流指南

WrenAI 的工作流指南**内置于 CLI 中**，按需加载，始终与安装版本匹配。

### 6.1 列出指南

```bash
wren skills list
```

### 6.2 获取指南

```bash
wren skills get <name> [--full] [--script <name>]
```

| 指南名 | 内容 |
|--------|------|
| `onboarding` | 端到端设置：安装 → 连接 → 生成 MDL → 查询 |
| `usage` | 日常使用：SQL 查询、dry-plan、上下文管理 |
| `generate-mdl` | 从数据库 Schema 生成 MDL 模型 |
| `enrich-context` | 为 MDL 添加业务上下文（单位、枚举、指标） |
| `dlt-connector` | 通过 dlt 连接 SaaS 数据源 |
| `genbi` | 构建部署 GenBI 仪表板 |

| 参数 | 说明 |
|------|------|
| `--full` | 包含完整参考文档 |
| `--script <name>` | 获取绑定的脚本 |

**示例：**

```bash
wren skills get onboarding
wren skills get generate-mdl --full
wren skills get dlt-connector --script introspect_dlt
```

---

## 七、docs — 参考文档

### 7.1 连接参数查询

```bash
wren docs connection-info <datasource>
```

查询特定数据源的连接参数（必填项和可选项）。

```bash
wren docs connection-info postgres
wren docs connection-info duckdb
wren docs connection-info snowflake
```

---

## 八、memory — 语义记忆

需要 `pip install "wrenai[memory]"`。

### 8.1 索引

```bash
wren memory index
```

索引当前项目的查询历史到语义记忆库。

### 8.2 检索

```bash
wren memory recall --question "<NL question>"
```

根据自然语言问题检索相似的已存储查询。

### 8.3 存储

```bash
wren memory store --question "<NL>" --sql "<SQL>"
```

手动存储一个 NL-SQL 对到记忆库。

---

## 九、ask — AI Agent 问题包装

`wren ask` 本身**不执行任何查询、不生成 SQL**。它只将用户问题包一层 prompt 模板，输出给 LLM Agent。

### 两阶段架构

```
┌─────────────────────────────────────────────────────────┐
│ 阶段一：wren ask（本地，无 LLM 调用）                      │
│                                                         │
│  用户问题 ──→ wren ask --direct ──→ 渲染后的 prompt 文本   │
│                   读取：ask_templates/direct.md.tmpl      │
│                                                         │
├─────────────────────────────────────────────────────────┤
│ 阶段二：LLM Agent 执行（由 Agent 驱动）                    │
│                                                         │
│  LLM 读到 prompt ──→ 自主调用 WrenAI CLI 命令完成查询       │
│                                                         │
│  1. wren context show      读取 models/*/metadata.yml    │
│     ↓                                                   │
│  2. wren memory recall     查语义记忆库（可选）             │
│     ↓                                                   │
│  3. LLM 自己写 SQL         （LLM 生成）                    │
│     ↓                                                   │
│  4. wren dry-plan          读取 target/mdl.json          │
│     ↓                                                   │
│  5. wren query             读取 target/mdl.json          │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 各步骤详解

**步骤 1 — 查看有哪些模型：`wren context show`**

```
读取：models/*/metadata.yml + relationships.yml
      （不读 target/mdl.json）
```

默认 `summary` 输出：模型名、列数、主键、关系列表。**不显示**列详情和描述。

| 模式 | 命令 | 显示内容 |
|------|------|---------|
| summary（默认） | `wren context show` | 模型名、列数、PK；关系名；视图名 |
| yaml | `wren context show -o yaml` | **全部**：每个模型的完整列定义、`properties.description`、foreign keys |
| json | `wren context show -o json` | 同上（camelCase 格式） |

> 要看到完整的列名、类型、描述和外键，用 `-o yaml` 或 `-o json`。

**关于描述信息：** 如果 `gen_models.py` 读取了 MySQL 的 `COLUMN_COMMENT` 和 `TABLE_COMMENT`，会写入 `models/*/metadata.yml` 的 `properties.description` 字段。`wren context show -o yaml` 会完整展示这些描述。

**关于外键关系：** 外键不会自动从数据库内省生成。`gen_models.py` 只生成模型文件，不生成 `relationships.yml`。如需定义外键，手动编辑 `relationships.yml`：
```yaml
- name: Album_Artist
  models: [album_t, artist_t]
  join_type: many_to_one
  condition: "album_t.ArtistId = artist_t.ArtistId"
```

Agent 从中得知可用模型名（如 `artist_t`、`album_t`）和列名（如 `ArtistId`、`Name`），作为写 SQL 的依据。

**步骤 2 — 检索相似历史查询：`wren memory recall`（可选）**

```
需要：pip install "wrenai[memory]"，并提前 wren memory index
输入：自然语言问题关键词
输出：相似的历史 NL-SQL 对
```

如果当前问题与历史查询相似，Agent 可直接参考已有 SQL，减少重复推理。无记忆库时跳过。

**步骤 3 — 写 SQL：LLM 生成**

```
输入：步骤1的模型清单 + 步骤2的历史参考 + 用户问题
输出：SQL 语句
```

LLM 根据模型名（映射到真实表名）和列名写 SQL。例如看到 `artist_t` → `table: Artist` → `SELECT ArtistId, Name FROM Artist`。

**步骤 4 — 验证 SQL：`wren dry-plan`**

```
读取：target/mdl.json（不读 models/*）
输入：步骤3的 SQL
输出：语法/语义验证结果（通过 / 报错）
```

不访问数据库，仅检查 SQL 中引用的表/列在 MDL 中是否存在、JOIN 关系是否合法。

**步骤 5 — 执行查询：`wren query`**

```
读取：target/mdl.json（不读 models/*）
输入：验证通过的 SQL
输出：查询结果（table/json/csv）
```

通过 MDL 层将模型名映射为真实表名后实际执行。

### 模式选择

| 模式 | 命令 | prompt 内容 | 适用 |
|------|------|-----------|------|
| `--direct` | `wren ask "..." --direct` | 一行提示 + 用户问题 | 强 LLM，自己知道调什么命令 |
| `--guided` | `wren ask "..." --guided` | 完整步骤清单（show→dry-plan→query） | 弱 LLM，需要告诉它每一步做什么 |

### 文件读取关系总结

| 命令 | 读取 | 不读 |
|------|------|------|
| `wren context show` | `models/*/metadata.yml`、`wren_project.yml`、`relationships.yml` | `target/mdl.json` |
| `wren memory recall` | 语义记忆库（向量索引） | `models/*`、`target/mdl.json` |
| `wren dry-plan` | **仅** `target/mdl.json` | `models/*` |
| `wren query` | **仅** `target/mdl.json` | `models/*` |
| `wren context build` | `models/*/metadata.yml` → 生成 `target/mdl.json` | — |

> `models/*` 是项目源文件（YAML），`target/mdl.json` 是构建产物（JSON）。
> `show` 读源文件，`query` 和 `dry-plan` 读构建产物。
> 修改模型后必须 `wren context build` 重新生成 `mdl.json`，否则查询用的还是旧版本。

## 十、serve — MCP 服务器

```bash
wren serve mcp
```

启动 WrenAI MCP 服务器，供 MCP 兼容的 AI Agent 调用。

---

## 连接文件格式（connection.json）

### DuckDB（本地文件）

```json
{
  "datasource": "duckdb",
  "url": "D:/path/to/database.duckdb"
}
```

### DuckDB 读取 SQLite

```json
{
  "datasource": "duckdb",
  "url": "D:/path/to/chinook.sqlite",
  "format": "sqlite"
}
```

> DuckDB 原生支持读取 SQLite 文件，通过 `format: sqlite` 指定。

### MySQL

```json
{
  "datasource": "mysql",
  "host": "localhost",
  "port": 3306,
  "database": "mydb",
  "user": "root",
  "password": "your_password"
}
```

可选字段：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `ssl_ca` | CA 证书路径 | 无（不启用 SSL） |
| `ssl_cert` | 客户端证书路径 | 无 |
| `ssl_key` | 客户端密钥路径 | 无 |
| `charset` | 字符集 | `utf8mb4` |

### PostgreSQL

```json
{
  "datasource": "postgres",
  "host": "localhost",
  "port": 5432,
  "database": "mydb",
  "user": "postgres",
  "password": "your_password"
}
```

可选字段：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `sslmode` | SSL 模式 | `prefer` |
| `sslrootcert` | CA 证书路径 | 无 |
| `schema` | 默认 schema | `public` |

### BigQuery

```json
{
  "datasource": "bigquery",
  "project_id": "my-gcp-project",
  "cred_file_path": "/path/to/service-account.json"
}
```

### Snowflake

```json
{
  "datasource": "snowflake",
  "account": "my_account",
  "user": "my_user",
  "password": "my_password",
  "database": "MY_DB",
  "schema": "PUBLIC",
  "warehouse": "COMPUTE_WH",
  "role": "ANALYST"
}
```

### 通用字段速查

| 字段 | 类型 | 适用数据源 | 说明 |
|------|------|-----------|------|
| `datasource` | string | 全部 | 数据源类型（必填） |
| `host` | string | mysql, postgres, mssql, oracle, clickhouse | 主机地址 |
| `port` | int | mysql, postgres, mssql, oracle | 端口号 |
| `database` | string | mysql, postgres, mssql, snowflake, bigquery | 数据库名 |
| `user` | string | mysql, postgres, mssql, oracle, snowflake | 用户名 |
| `password` | string | mysql, postgres, mssql, oracle, snowflake | 密码 |
| `url` | string | duckdb, datafusion | 文件路径 |
| `format` | string | duckdb | 特殊格式（如 `sqlite`） |
| `project_id` | string | bigquery | GCP 项目 ID |
| `account` | string | snowflake | Snowflake 账户名 |
| `warehouse` | string | snowflake | 计算仓库名 |
| `role` | string | snowflake | 角色名 |

### 导入连接

```bash
# 从 JSON 文件导入
wren profile add mydb --from-file config/connection.json

# 导入并激活
wren profile add mydb --from-file config/connection.json --activate

# 跳过连接验证
wren profile add mydb --from-file config/connection.json --no-validate
```

---

## 支持的数据库

| 数据源 | `datasource` 值 |
|--------|----------------|
| DuckDB | `duckdb` |
| PostgreSQL | `postgres` |
| MySQL | `mysql` |
| BigQuery | `bigquery` |
| Snowflake | `snowflake` |
| Spark SQL | `spark` |
| Trino | `trino` |
| Databricks | `databricks` |
| MS SQL Server | `mssql` |
| Oracle | `oracle` |
| ClickHouse | `clickhouse` |
| AWS Athena | `athena` |
| Redshift | `redshift` |
| DataFusion | `datafusion` |

> SQLite 无直接连接器，通过 DuckDB `format: sqlite` 读取。
