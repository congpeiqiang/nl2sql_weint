# Wren 语义库管理模块 — 设计方案（已实现）

> 状态：**已实现**。后端 `src/api/wren_semantic.py` + `src/agent/utils/git_repo.py`，
> 前端 `SemanticLibraryPanel`（设置弹窗「语义库」选项卡）。§6 验证清单可作冒烟回归。

## 1. 背景与目标

用户希望在前端增加一个「Wren 语义库」管理模块，支持：

1. **新增语义库**：把一个 Wren 项目目录（含 `wren_project.yml`）落地到本地，并关联到某条数据库配置（`DBConfig.wren_project`）。
2. **从 Git 拉取语义库**：按 tag / 分支从远程仓库拉取语义库（参考仓库 `https://github.com/congpeiqiang/aix_project`）。
3. **删除本地语义库**：删除本地项目目录（及关联）。
4. **其他管理功能建议**（见 §5）。

## 2. 现状梳理（已核实）

### 2.1 语义库的物理形态

「语义库」当前**没有独立实体**，本质是一个磁盘上的 Wren 项目目录，靠 `db_config.json` 里的 `wren_project` 路径字段与数据库配置关联。目录结构：

```
<project>/
├── wren_project.yml            # schema_version / name / catalog / schema / data_source / profile
├── models/<model>/metadata.yml # 每张物理表一个模型
├── views/<view>/{metadata.yml,sql.yml}
├── relationships.yml           # 表间关系
├── knowledge/                  # rules / sql / glossary / metrics
├── config/connection_*.json    # 数据源连接（含明文 host/port/user/password）
└── target/mdl.json             # `wren context build` 产物；query/dry-plan 只读它
```

### 2.2 关键代码位置

| 关注点 | 文件 | 说明 |
|--------|------|------|
| Wren MCP server 构建 | `src/agent/tools/mcp_tool.py:139-196` | 进程启动时为每个已建模库构建 stdio server，`_sub_tools` 单例 |
| server 前缀命名 | `src/agent/utils/semantic_db.py:34-41` | `wrenai_<db_name>`，非单词字符转下划线 |
| is_modeled 判定 | `src/agent/utils/semantic_db.py:143-153` | 严格按 `DBConfig.name` 匹配；`invalidate()` 可即时刷新 detector |
| DBConfig 结构 | `src/mcp_server/db_mcp_server/db/core/db_config_store.py:49-61` | `wren_project: str`（绝对路径，空=直连） |
| 项目扫描 | `src/api/db_config.py` `_scan_wren_projects()` | 扫 workspace 一级子目录 + `WREN_PROJECT_PATH`，找含 `wren_project.yml` 的目录 |
| API 组合根 | `src/api/custom_app.py` | 新 API 模块暴露 `routes`，加一行展开 |

### 2.3 关键约束（决定设计取舍）

1. **重启门槛**：新增/删除语义库后，Wren MCP 工具（`wrenai_*`）要**重启后端进程**才生效——`_sub_tools` 是进程启动时构建的单例。detector 缓存可 `invalidate()` 即时刷新，但 server 本身必须重启。
2. **命名/前缀**：server 前缀由 `db_name` 推导并 sanitize；`is_modeled` 严格按 `name` 匹配，与物理库名解耦。语义库 name 与库 name 不一致会导致 LLM 找不到工具（`clickhouse→aix` 误判教训，见 memory `[[wren-multi-project-design]]`）。
3. **MDL 构建产物**：`wren query`/`dry-plan` 只读 `target/mdl.json`；改 models 后必须 `wren context build`。git 仓库常把 `target/` gitignore 掉，拉取后需本地重新 build。
4. **凭据安全**：`config/connection_*.json` 含明文密码，git 分发语义库时不应携带真实凭据；应在本地用 `db_config.json` 已加密的连接信息**覆盖生成** connection 文件。
5. **数据源白名单**：`_SUPPORTED_DB_TYPES = (mysql, clickhouse, postgres, sqlite)`；wren 支持面更广（bigquery/snowflake 等），语义库引入的数据源需与直连能力对齐。

## 3. 功能设计

### 3.1 语义库列表（前端面板）

复用 `_scan_wren_projects()` 现有扫描逻辑，返回每个语义库：

- `name`（项目 name，来自 `wren_project.yml`）
- `path`（磁盘绝对路径）
- `关联的库`（哪些 `DBConfig.wren_project` 指向它）
- `来源`（本地 / git）
- `git 元信息`（若 `.git` 存在：remote、当前 branch、当前 tag/commit）
- `状态`（`target/mdl.json` 是否存在 = 是否已构建）

### 3.2 新增语义库

**场景 A — 关联本地已有目录**：用户填一个已存在的项目目录路径 → 校验含 `wren_project.yml` → upsert 某条 `DBConfig.wren_project` 指向它。

**场景 B — 从 Git 拉取（核心）**：

```
POST /api/wren-projects/from-git
  { repo_url, ref, project_name?, target_db?, overwrite_connection? }
```

流程：

1. **clone**：`git clone --depth 1 --branch <ref> <repo_url> <workspace>/<project_name>`（`ref` 可为 branch 或 tag）。浅克隆拉 tag 需 `--branch` 指向 tag（或 `git fetch origin refs/tags/<tag>` + checkout）。
2. **定位项目根**：若仓库根即项目（含 `wren_project.yml`）直接用；否则支持 `subdir` 字段指定子目录。
3. **凭据处理**：默认**不信任**仓库内的 `config/connection_*.json`。若 `target_db` 给定，用该 `DBConfig` 的已解密连接信息**重写生成** connection 文件（复用 `McpSqlConfig.from_env` 的字段映射）；若仓库自带凭据且用户显式允许（`overwrite_connection=false`）则保留。
4. **构建 MDL**：检测 `target/mdl.json` 是否在仓库内；缺则本地执行 `wren context build`（可选 `wren memory index`）。
5. **关联**：upsert `target_db` 的 `wren_project` 指向新目录；`SemanticDbDetector.invalidate()`。
6. **提示重启**：返回 `requires_restart: true`，前端提示「新增语义库后需重启后端，MCP 工具 `wrenai_*` 才生效」。

### 3.3 删除本地语义库

```
DELETE /api/wren-projects/{name}
```

- 找到项目目录；先解绑所有指向它的 `DBConfig.wren_project`（置空），再删除目录。
- `dangerouslyDeleteFiles` 需显式二次确认（删除不可逆）。
- 删除前 `wren_project.yml` 的 `name` 与请求 name 一致才执行（防误删）。
- 同样返回 `requires_restart: true`。

### 3.4 其他管理功能建议（一并纳入设计，分优先级）

| 功能 | 说明 | 优先级 |
|------|------|--------|
| **语义库校验** | 跑 `wren validate` / `dry-plan`，前端显示模型/视图/关系数量、错误列表 | P1 |
| **重新构建 / 重新索引** | 手动触发 `wren context build` + `wren memory index` | P1 |
| **MDL 概览** | 读 `target/mdl.json` 展示 models/views/relationships 数量，供用户确认拉取结果 | P1 |
| **关联关系可视化** | 列出每个库 → 语义库的映射，支持改绑（`wren_project` 改指） | P2 |
| **版本管理** | 记录 git tag/branch/commit，支持切 tag 回滚（`git checkout` + rebuild） | P2 |
| **导出** | 打包项目目录下载 / 推回 git | P3 |

## 4. 接口与文件改动清单

### 4.1 新增后端模块

- `src/api/wren_semantic.py`（暴露 `routes`，`custom_app.py` 加一行）：
  - `GET  /api/wren-projects`（列表，复用 `_scan_wren_projects` + git 元信息）
  - `POST /api/wren-projects/local`（关联本地目录）
  - `POST /api/wren-projects/from-git`（clone + build + 关联）
  - `DELETE /api/wren-projects/{name}`（解绑 + 删目录）
  - `POST /api/wren-projects/{name}/build`（重新 context build / memory index）
  - `GET  /api/wren-projects/{name}/summary`（MDL 概览）
- `src/agent/utils/git_repo.py`（新）：封装 clone/checkout/tag/commit 查询（用 `subprocess` 调 `git`，避免引入 GitPython 依赖）。

### 4.2 安全控制

- git 操作在 `workspace/` 白名单目录内，禁止 `../`、绝对路径逃逸、`--upload-pack` 等注入参数；repo_url 校验只允许 http(s)。
- 删除目录必须在 workspace 根内且 name 匹配 `wren_project.yml.name`。
- 凭据默认覆盖，不从仓库继承明文密码。

### 4.3 前端

- 新面板 `SemanticLibraryPanel`（入口放设置或侧栏）。
- 列表 + 操作按钮（新增 / git 拉取 / 删除 / 校验 / 重建）。
- git 拉取表单：repo_url、ref（branch/tag 下拉 + 手动输入）、subdir、target_db、是否覆盖凭据。
- 删除二次确认；变更后统一提示「需重启后端生效」。

## 5. 风险与边界

1. **重启是硬门槛**：除非改造 `_get_sub_server_config` 支持运行时动态重建 MCP server（改动大、风险高），否则「新增/删除语义库」的生效必须重启 2026。本设计先接受重启门槛，动态重建列为二期。
2. **浅克隆与 tag**：`git clone --depth 1 --branch <tag>` 对 tag 生效；但「按分支拉取后本地再切 tag」需要完整历史，浅克隆不够，需在文档里区分「拉取即用」与「本地多版本切换」两种模式。
3. **构建产物**：`wren context build` 依赖本地已装 wren CLI（`WREN_BIN_PATH`）；若拉取的仓库不带 `target/`，首次 build 可能因缺少真实数据源连接而失败——此时需 `target_db` 提供的连接信息先生成 connection 文件再 build。
4. **凭据**：`db_config.json` 的密码是 AES 加密的；覆盖生成 connection 文件时需解密后写入明文文件（wren CLI 只读明文 connection），这是既有架构的固有约束（imdb/aix 项目已如此）。
5. **多库共享同一语义库**：允许（多个 `DBConfig.wren_project` 指向同一目录）；删除时需解绑所有引用。

## 6. 验证

1. 本地目录关联：`POST /api/wren-projects/local` 后 `GET /api/wren-projects` 可见，`db_config.json` 的 `wren_project` 已写。
2. git 拉取：`POST /api/wren-projects/from-git`（用 `congpeiqiang/aix_project` 的某 tag）→ 目录落地 + `target/mdl.json` 生成（或报需先配 target_db）→ 列表可见 git 元信息。
3. 删除：`DELETE /api/wren-projects/{name}` → 目录删除 + 关联解绑。
4. 重启后 `wrenai_<db_name>_*` 工具出现（用户操作，破坏性重启需确认）。

## 7. 参考

- `docs/agent优化记录/deepseek-harness对标清单与实现方案.md`
- memory `[[wren-multi-project-design]]`、`[[db-config-api-launch-from-src]]`、`[[langgraph-custom-app-hook]]`
