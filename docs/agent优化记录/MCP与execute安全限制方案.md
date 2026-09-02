# MCP 与 execute 安全限制方案（设计选项）

> 日期：2026-08-12
> 状态：**方案设计稿，未实施**（先不实施，确认后再落地）
> 涉及项目：nl2sql 后端（`D:\code_work_space\llm\nl2sql`）
> 相关前置：`代理文件权限控制方案.md`（deepagents FilesystemPermission，已实施）——本次补上它记录的「execute 与 MCP 不受文件权限控制」这两个已知局限

---

## 1. 背景与目标

上一步已用 deepagents `FilesystemPermission` 约束了 agent 的**文件工具**（只读根、仅 `workspace/{report,tmp,nl2sql_process_data}` 可写），但该机制管不到两件事，本次方案专门覆盖：

1. **`execute` 执行脚本**：deepagents 框架明确不支持 execute 的权限控制，主 agent 的 shell 执行在 Windows 宿主任意跑命令。
2. **MCP 工具**：`dbmcp_run_sql` 可执行任意 SQL（含 DDL/DML/多语句），是独立于文件权限模型的写通道；图表 MCP 落盘由 path_resolver 收敛到 `workspace/report`，另有一处信息泄露面。

**目标**：给出可落地的安全限制方案选项（execute 与 MCP 分开），含注入点、配置入口、验证方式与回滚。本次只写方案，不实施。

---

## 2. 风险面现状（探索结论）

### 2.1 execute —— 宿主任意命令执行

- 主 agent backend（`src/agent/main_agent.py:100-102`）：
  ```python
  shell_backend = LocalShellBackend(root_dir=Path(base_dir) / "workspace", inherit_env=True, virtual_mode=True)
  composite_backend = CompositeBackend(default=shell_backend, routes={"/": file_backend})
  ```
- `LocalShellBackend.execute`（`.venv/.../deepagents/backends/local_shell.py:325-336`）把命令字符串**原样**传给 `subprocess.run(..., shell=True)`（Windows 上是 cmd.exe），`inherit_env=True` 继承宿主完整环境——`cwd` 限制只是设工作目录，不是 chroot。
- 构造参数只有 `root_dir / virtual_mode / timeout(120s) / max_output_bytes / env / inherit_env`，**无白名单/黑名单/命令拦截/审计**，docstring 明确写 "unrestricted local shell execution"。
- `_create_execute_tool`（`.venv/.../deepagents/middleware/filesystem.py:1674`）只做超时校验（`timeout<0` 或 `>max_execute_timeout(3600s)` 直接返回错误），**不校验命令内容**；`EXECUTE_TOOL_DESCRIPTION` 承诺"isolated sandbox"但实际取决于后端。
- 子 agent（`nl2sql_agent.py:48`）backend 是纯 `FilesystemBackend`，`supports_execution=False` → execute 工具被自动过滤，**子 agent 无执行能力**（风险面只在主 agent）。

### 2.2 dbmcp_run_sql —— 任意 SQL 写通道

- `src/mcp_server/db_mcp_server/db/db_server.py:110-144`：docstring 明确「支持单条 SELECT/INSERT/UPDATE/DELETE、多条语句、DDL+DML 混合」。`split_sql_statements` 仅按分号切分、无类型检查。
- 各引擎 runner **无语句类型拦截**：
  - postgres（`engine/postgres/sql_runner.py:90-111`）：非 SELECT 走 `conn.commit()` 返回 `rows_affected`（**写操作被提交**）
  - mysql（`engine/mysql/sql_runner.py:79-93`）：`cursor.execute` 任意语句直接执行
  - clickhouse（`engine/clickhouse/sql_runner.py:74-80`）：`client.query` 任意语句
- **无只读模式、无语句白名单、无敏感库表保护**；server 层**无超时**（超时全靠上层 wrap_tool 的 120s dbmcp / 300s wrenai 兜底）。
- **行数限制软性可绕过**：`_apply_default_limit`（`db_server.py:33-46`）只对 SELECT/WITH 且 SQL 无 `LIMIT` 时追加 `LIMIT {int(limit)}`；`limit` 是 LLM 可控参数，传 `0` 或负数即跳过（`if not limit or limit <= 0: return stmt`），fetch 后无截断。
- 语义层 `wrenai_<库>_run_sql` 由 WrenAI Go 二进制实现（`wren serve mcp`），Python 侧只能限自己发的请求，无法确认其内部只读性。

### 2.3 图表 MCP —— 写路径与信息泄露面

- echarts 当前版本（`CHART_ENGINE=echarts`）**不写本地文件**：`outputType="option"`/`svg` 返回字符串，`png` 返回 base64；仅配 MinIO 时才写临时文件上传（当前未启用）。
- 实际落盘由 path_resolver 干：HTML → `_save_echarts_html_to_workspace`（`path_resolver.py:114-152`）、SVG → `_save_svg_to_workspace`（`295-335`）、PNG 路径 → `_move_echarts_image_to_workspace`（`259-282`），全部收敛到 `workspace/report`。文件名经 `re.sub(r'[\\/:*?"<>|]', "_", t)` 清洗，无路径穿越。
- **信息泄露面**：`_move_echarts_image_to_workspace` 拷贝失败时 `return src_path` 原路径并让 LLM 用它渲染 `file:///...`（`path_resolver.py:424-425`）——任意文件路径可能被带进结果回传；且 `shutil.copy2(src, report_dir/src.name)` 可把任意路径文件复制进 report。
- semiotic（备选引擎）全内存渲染 SVG，无文件写入。

### 2.4 子 agent 工具白名单 —— 宽松子串匹配

- `src/agent/subagents/configs/nl2sql.yaml:13-51` 用裸名（无前缀），`nl2sql_agent.py:37-43` 按 `pattern in tn` **子串匹配**：`run_sql` 一个词同时放行 `dbmcp_run_sql` 与全部 `wrenai_<库>_run_sql`；主 agent 的 `main_tools`（图表）**无按工具名白名单**，17 个 echarts 工具全量注入。

### 2.5 wrap_tool —— 唯一全量包装层

`src/agent/utils/path_resolver.py:748-870` 对每个 MCP 工具 `_run`/`_arun` 都包一层：超时（`_TOOL_TIMEOUTS`）、db_name 注入（`_inject_db_name`，仅 dbmcp 前缀）、图表 props 打包、结果 sanitize。**没有按工具名的 allow/deny、没有入参校验**，SQL 内容/limit/db_name 合法性都不在此校验——这是新增统一闸门的最合适位置。

### 2.6 配置层

- `src/agent/settings/setting.py`：pydantic-settings，与安全相关的现有开关只有 `NL2SQL_DBMCP_ENABLED`（布尔）与 `CHART_ENGINE`；无 read_only/白名单/超时/行数上限配置。
- ⚠️ agent 进程与 db_server 子进程（`db/core/settings.py:52-54` 是第二份 Settings）**各自独立读 env**，配置要两边都加或经 env 透传。

---

## 3. execute 方案（按侵入性升序）

| 方案 | 做法 | 优点 | 代价/局限 |
|---|---|---|---|
| **E1 后端子类化白名单**（推荐保留 execute 时用） | 继承 `LocalShellBackend` 覆写 `execute()`，对 `command` 做前缀/关键字白名单（如只允许 `python /workspace/...`、`pip`、`mkdir`），其余返回错误。即 deepagents 官方建议的「子类化后端」路线 | 侵入小、单点、贴合框架原设计；可顺带全量审计 | 需自行维护白名单；`shell=True` 透传特性（管道/重定向/命令替换）需额外校验或一并限制 |
| **E2 中间件校验/审计** | 自写 middleware 在 `wrap_model_call` 阶段解析 `request.tools` 里 execute 的参数做校验 + 全量日志（与 `message_slimmer.py` 同模式） | 可审计「谁执行了什么命令」；不改后端行为 | 校验只在调用前挡，execute 实际仍透传；需处理 sync/async 双通道 |
| **E3 隔离到远程容器** | 把仓库已存在但**未接线**的 `OpenSandboxBackend`（`src/agent/backends/custom_opensandbox.py`，阿里云 OpenSandbox 远程 Linux 容器）接为 `CompositeBackend.default`，并启用 `sandbox_setup.py` 里被注释的 `NetworkPolicy(defaultAction="deny", egress=[允许 pypi/github])` | 执行彻底挪出宿主；自带网络策略；`opensandbox 0.1.14` 已安装 | 需远程服务/密钥；每次 execute 走网络延迟；执行环境（cwd/依赖）变化可能导致既有脚本行为差异 |
| **E4 直接禁用 execute** | 把 composite default 换成不支持执行的 backend（或从主 agent 移除 execute），提示词同步删掉那个失效的 mkdir 兜底 | 最彻底，攻击面归零 | 失去 shell 能力；需确认主 agent 流程确实不再需要 shell（探索显示提示词里 execute 仅一个失效兜底，使用面小） |

> 共同点：deepagents **没有现成命令白名单**，`max_execute_timeout`（默认 3600s）只限时长不校验内容——内容限制必须自己实现。

---

## 4. MCP 方案（DB/SQL 为主落点）

| 方案 | 做法 | 优点 | 代价/局限 |
|---|---|---|---|
| **M1 SQL 语句类型白名单（只读模式）** | 在 `db_server.py:run_sql` 的 `split_sql_statements` 之后拦截：只放行 SELECT/WITH（可加 EXPLAIN），拒绝 INSERT/UPDATE/DELETE/DDL/多语句。加 `NL2SQL_READ_ONLY` 或 `NL2SQL_ALLOWED_STATEMENTS` 配置开关 | **主落点**，直接堵死 DB 写入 | 语义层 `wrenai_run_sql` 由 Go 二进制实现，Python 侧只能限自己发的请求；若存在数据入库类任务需开关放开 |
| **M2 强制行数上限 + 防绕过** | 修 `_apply_default_limit`：`limit` clamp 到 `[1, MAX]`，`0`/负数一律用默认；`fetchall` 后再截断兜底 | 防大表全量拉取拖垮/泄密 | 治标（数据面），治不了写 |
| **M3 server 层查询超时** | db_server 内部对 `run_sql` 加超时，不再只靠 wrap_tool 的 120s/300s 兜底 | 慢 SQL 在源头被切断 | 各引擎 runner（pg/mysql/ck）需分别包超时 |
| **M4 敏感库/表黑名单 + db_name 校验** | 加 `NL2SQL_DENY_DBS`/拒写表列表，runner 层拦截；校验 `db_name` 必须属于已配置连接 | 精确控制数据面 | 黑名单要配白名单兜底才稳 |
| **M5 wrap_tool 层加参数校验与工具名过滤** | `path_resolver.py:748-870` 是唯一全量包装点：入口按工具名前缀做 allow/deny、校验入参（sql/limit/db_name）、可加只读注入 | 一处全管（图表+DB），与现有超时/db_name 注入同层 | 与 M1/M2 重复防护（建议两层都做，纵深） |
| **M6 子 agent 白名单精确匹配** | 把 `substring in name` 改成精确匹配 + 按 `dbmcp_`/`wrenai_<库>_` 前缀分组 | 收紧「一个词放行全部 DB 工具」 | 需维护前缀清单，新增库时同步 |
| **M7 DB 账号只读权限（本质防御）** | dbmcp 连接配置给 DB 账号建只读账号（GRANT SELECT only），DB 层彻底禁写 | 最强兜底，即使 agent 层被绕过也写不进去 | 运维成本；`db_config_store` 已加密存密码，需配套新增只读凭证 |

**图表 MCP 写路径（小修）**：`_move_echarts_image_to_workspace`（`path_resolver.py:259-282`）拷贝失败时**不要** `return src_path` 原路径，改为返回错误消息——堵住「任意文件路径被带进结果」的泄露面。当前 MinIO/临时目录外部写未启用，建议保持不启用。

---

## 5. 配置层统一入口

新增开关（建议）统一加到 `src/agent/settings/setting.py`（pydantic-settings，复用现有布尔解析写法）：

- `NL2SQL_READ_ONLY` / `NL2SQL_ALLOWED_STATEMENTS`（M1）
- `NL2SQL_MAX_ROWS`（M2）
- `NL2SQL_QUERY_TIMEOUT`（M3）
- `NL2SQL_DENY_DBS`（M4）

⚠️ agent 进程与 db_server 子进程各自独立读 env（两份 Settings），配置须两边都加或经 env 透传。

---

## 6. 推荐组合

按「低成本高收益」排序的落地顺序：

1. **M1（只读模式）+ M2（强制 LIMIT）**——治 DB 写入面，最痛的点，改动集中在一个文件；
2. **E4 或 E1**——治 execute：E4 最省事（提示词里 execute 本就近乎没用）；要保留 shell 就上 E1 白名单；
3. **M5 wrap_tool 入口校验**做统一闸门 + **M7 只读账号**做本质兜底（可延后）；
4. 顺手修图表 `_move_echarts_image_to_workspace` 拷贝失败回退原路径的信息泄露。

---

## 7. 验证方式（实施后执行，本次未实施）

1. **单元级**：对 `_apply_default_limit` 改造后的规则直接断言（`limit=0`/负数 → 默认值；超上限 → clamp）；对语句类型白名单（M1）用 SELECT/INSERT/DDL 各一例断言放行/拒绝。
2. **E2E（真实 2026 运行时，独立 thread）**：
   - 指示 agent `dbmcp_run_sql` 执行 `INSERT/DROP/ALTER` → 期望被拒（只读模式）；
   - `SELECT` 大表不传 limit / 传 `limit=0` → 期望行数被强制截断；
   - 主 agent 触发 execute（如让执行 `python --version`）→ 按所选方案验证白名单命中/拒绝或已移除；
   - 图表生成一轮 → 确认 report 落盘正常、`copy2` 失败路径不再回传原路径。
3. **回归**：2026 重启后两个 graph 正常加载，前端轮询、子 agent 委派、图表流程无回归。

---

## 8. 相关文件索引

| 动作 | 文件 |
|------|------|
| 本次只产出 | `docs/agent优化记录/MCP与execute安全限制方案.md`（本文件） |
| execute 注入点（E1/E4） | `src/agent/main_agent.py:100-102`（backend 组装）、`:157-167`（create_deep_agent）；`src/agent/backends/custom_opensandbox.py`、`sandbox_setup.py`（E3 死代码） |
| execute 框架层（只读参考） | `.venv/Lib/site-packages/deepagents/backends/local_shell.py`、`middleware/filesystem.py:1674`（`_create_execute_tool`） |
| MCP DB 注入点（M1/M2/M3/M4） | `src/mcp_server/db_mcp_server/db/db_server.py`、`engine/{postgres,mysql,clickhouse}/sql_runner.py` |
| MCP 统一闸门（M5） | `src/agent/utils/path_resolver.py:748-870`（wrap_tool） |
| 工具白名单（M6） | `src/agent/subagents/configs/nl2sql.yaml`、`src/agent/nl2sql_agent.py:37-43` |
| 配置入口 | `src/agent/settings/setting.py`、`src/mcp_server/db_mcp_server/db/core/settings.py`（第二份） |
| 前置方案（已实施） | `docs/agent优化记录/代理文件权限控制方案.md` |
