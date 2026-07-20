# WrenAI 完整使用教程 — Chinook 数据库实战

> WrenAI — 开源 GenBI 引擎，DuckDB 内核，支持 22+ 数据源
> GitHub: https://github.com/Canner/WrenAI
> 数据库: Chinook (SQLite, 11 张表, 3503 首曲目)

---

## 一、安装

```bash
pip install wrenai pyyaml

# wrenai版本
wrenai==0.13.0
```

---

## 二、数据库连接

Chinook SQLite 数据库路径：`D:/code_work_space/llm/nl2sql/src/agent/data/Chinook_Sqlite.sqlite`

WrenAI 使用 DuckDB 引擎读取 SQLite 文件：

```bash
# 方式1: 从 JSON 文件导入（推荐）
wren profile add chinook --from-file config/connection_sqlite.json
wren profile add chinook --from-file config/connection_mysql.json

# 方式2: 交互模式
wren profile add chinook -d duckdb --activate
# Database URL: D:/code_work_space/llm/nl2sql/src/agent/data/Chinook_Sqlite.sqlite
# Format: sqlite
# datasource: duckdb

# 查看连接
wren profile list
```

**注意：** WrenAI 当前版本不直接支持 `sqlite` 数据源。使用 DuckDB + `format: sqlite` 组合。

---

## 三、初始化 MDL 项目

```bash
wren context init
wren context set-profile chinook
```

这会创建：
- `wren_project.yml` — 项目配置
- `models/` — 模型定义目录
- `views/` — 视图定义目录
- `knowledge/` — 业务知识目录
- `relationships.yml` — confirmed NL-SQL pairs (wren memory store)
- `AGENTS.md` — AI agent workflow guidance

---

## 四、自动生成表模型

从 SQLite Schema 自动提取全部 11 张表的列定义，生成 MDL 模型：

```bash
python gen_models.py
```

生成结构：

```
models/
├── album_t/metadata.yml        # AlbumId, Title, ArtistId
├── artist_t/metadata.yml       # ArtistId, Name
├── customer_t/metadata.yml     # CustomerId, FirstName, ...
├── employee_t/metadata.yml     # EmployeeId, LastName, ...
├── genre_t/metadata.yml        # GenreId, Name
├── invoice_t/metadata.yml      # InvoiceId, CustomerId, ...
├── invoiceline_t/metadata.yml  # InvoiceLineId, InvoiceId, ...
├── mediatype_t/metadata.yml    # MediaTypeId, Name
├── playlist_t/metadata.yml     # PlaylistId, Name
├── playlisttrack_t/metadata.yml# PlaylistId, TrackId
└── track_t/metadata.yml        # TrackId, Name, AlbumId, ...
```

**关键技术点：**
- 模型名加 `_t` 后缀（如 `artist_t`），避免 WrenAI 生成的 CTE 与真实表名冲突
- `table_reference` 中显式设置 `catalog: ""` 和 `schema: ""`，避免 DuckDB 加 `wren.public.` 前缀

---

## 五、构建与验证

```bash
# 构建项目
wren context build

# 输出: target/mdl.json

wren context show

# 验证项目
wren context validate
```

---

## 六、查询数据

> WrenAI 的 `wren query` 通过 MDL 层执行，对 SQLite（无 schema）兼容性有限。
> 推荐使用 DuckDB 直接查询，或 `wren dry-plan` 做语法验证。

### 6.1 DuckDB 直接查询（推荐）

```bash
# 单次查询
python query_chinook.py "SELECT COUNT(*) FROM Artist"

# 列出所有表
python query_chinook.py

# JOIN 查询
python query_chinook.py "SELECT a.Title, r.Name FROM Album a JOIN Artist r ON a.ArtistId = r.ArtistId LIMIT 5"

# 聚合
python query_chinook.py "SELECT Country, COUNT(*) FROM Customer GROUP BY Country ORDER BY 2 DESC"
```

### 6.2 WrenAI 干运行验证（不访问数据库）

```bash
wren dry-plan --sql "SELECT e.FirstName, e.Title FROM Employee e" -d duckdb
```

### 6.3 WrenAI MDL 查询（需要完整语义层配置）

```bash
wren query --sql "..." --connection-file config/connection.json
```
> 需要 `wren_project.yml` 中 catalog/schema 与数据库匹配。SQLite 无 schema，配置较复杂。

---

## 七、目录结构总览

```
wrenai_exec/
├── config/
│   └── connection.json          ← 数据库连接配置
├── models/                      ← MDL 模型定义（gen_models.py 生成）
│   ├── artist_t/metadata.yml
│   ├── album_t/metadata.yml
│   └── ... (11 张表)
├── views/                       ← 视图定义
├── knowledge/                   ← 业务知识（规则、指标等）
├── target/
│   └── mdl.json                 ← wren context build 构建产物
├── wren_project.yml             ← 项目配置
├── gen_models.py                ← 从 Schema 自动生成模型
├── README.md                    ← 本教程
└── run_all.py                   ← 一键演示
```

---

## 八、NL2SQL 集成

WrenAI 在 NL2SQL 流水线中的角色：

| 阶段 | 命令 | 用途 |
|------|------|------|
| Phase 0 预处理 | `gen_models.py` + `wren context build` | 建立语义层 |
| Phase 2 Schema Linking | `wren context show` | 查看 MDL 语义模型 |
| Phase 2.5 SQL 验证 | `wren dry-plan --sql '...' -d duckdb` | 执行前验证 |
| Phase 3 纠错 | `wren memory recall` | 检索相似正确 SQL |

---

## 九、常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| `unknown datasource 'sqlite'` | WrenAI 不直接支持 sqlite | 用 DuckDB + format:sqlite |
| `table 'wren.public.Artist' not found` | 默认加了 schema 前缀 | table_reference 设 catalog:"" schema:"" |
| `Circular reference to CTE` | 模型名与表名冲突 | 模型名加 `_t` 后缀 |
| `UTF-8 decode error` | metadata.yml 含 GBK 编码 | 用 UTF-8 编码保存 |
| `target/mdl.json missing` | 未构建项目 | 先执行 `wren context build` |

---

## 十、脚本清单

| 脚本 | 说明 |
|------|------|
| `gen_models.py` | 从 SQLite 生成全部 11 张表的 MDL 模型 |
| `query_chinook.py` | **DuckDB 直接查询 Chinook（推荐）** |
| `gen_models.py` | 从 SQLite Schema 生成 11 张表的 MDL 模型 |
| `run_all.py` | 演示：profile → skills → dry-plan |
| `01_help.py` | WrenAI CLI 帮助 |
| `09_profile_list.py` | 查看数据库连接 |
| `11_dry_plan.py` | SQL 干运行验证 |
| `12_sql_exec.py` | 执行 SQL 查询 |
