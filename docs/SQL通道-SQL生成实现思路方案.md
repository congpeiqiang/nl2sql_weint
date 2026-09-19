# SQL 通道 · SQL 生成实现思路与改造方案

> 目的：把「SQL 通道真正下发到物理库的 SQL」在应用侧拿到并留档，用于**报告展示、审计、后期验证**。
> 受众：Claude Code（据此优化 `D:\code_work_space\llm\nl2sql` 现有代码）。
> 依据：wren 0.7.1 源码实测（2026-09-15），非推测；涉及文件与行号均为当前 venv 内的真实位置。

## 0. 给 Claude Code 的执行摘要（TL;DR）

**前提（本项目现状：走 wrenai MCP）**：语义层经 **stdio MCP 子进程**访问 ——
`src/agent/tools/mcp_tool.py:208-245` 启动 `wren serve mcp --project <wren项目> --profile wren_mcp_<库>`
（`command = settings.WREN_BIN_PATH`），工具名统一为 `wrenai_<库>_*`
（`run_sql` / `dry_run` / `dry_plan` / `query_cube` / `list_cubes` / `describe_cube` / `get_mdl` / `describe_schema` / memory 等）；
未传 `--allow-write` → **只读**（wren `ServeContext.allow_write`）。**方案必须在此约束内落地**。

**问题**：应用记录的「真实执行 SQL」是 `run_sql` 的**工具入参**（模型写的语义层 SQL，如 `FROM v_story`），
它在物理库里**跑不了**；真正下发的 SQL 由 wren 引擎在 `dry_plan` 阶段生成（`dialect_sql`），当前**成功路径拿不到**。

**要做的三件事**（MCP 前提下）：

1. **拿到 `dialect_sql`**（二选一，详见 §三）：
   - **方案 B（推荐，长期）**：自建 **stdio MCP 封装**，工具名保持 `wrenai_<库>_*` 不变 →
     应用侧只改 `mcp_tool.py` 的 command/args，中间件零改动。
     B2 直构式：复用 wren 的 `_build_engine` + `ServeContext` + `run_server`，`run_sql` 内先
     `engine.dry_plan(sql)` 再 `engine.query(...)`，**一次调用同时返回物理 SQL 与结果（零额外往返）**；
     B1 代理式：透传全部工具到真正的 `wren serve mcp`，仅增强 `run_sql` 的返回。
   - **方案 A（最小改动，先打通）**：`run_sql` 成功后在**代码里**补一次 MCP `dry_plan(sql)`。
     ⚠️ **必须由中间件/包装器调用，不能让 LLM 自己调**：应用把 `dry_plan` 归类为 exec 工具
     （`progress_boundary.py:52`、`query_gate.py:50`、`feedback_type.py:48`），LLM 自己调会污染进度判定、
     触发"先理解建模"提醒、把反馈归类弄脏。
2. **贯通数据契约**：工具返回体统一加 `dialect_sql`（见 §四 JSON 契约），
   并在 `check_progress.py` 写入 `result["dialect_sql"]`、`langfuse_span` 记录、`query_result_offload` 白名单保留。
3. **报告两段式展示**：`report_builder.py` 的 SQL 区块同时输出「① 语义层 SQL」与「② 物理 SQL（可直接在 MySQL 执行）」。

**红线**：只读语义不变（`readOnlyHint` / `validate_sql_policy` / 不传 `--allow-write` 全部保留）；
不新增任何写操作；不依赖 DBA 侧配置（`general_log`、`performance_schema` 均不可用，见方案 E）。

**验收**：用 §六 的每日缺陷用例（31 行 / 新增 1309 / 修复 1184），
确认 `dialect_sql` 直连 MySQL 的结果与 wren 官方结果逐行一致。

---

## 一、先明确问题：现在拿到的「真实执行 SQL」是假的

| | cube 通道 | SQL 通道 |
|---|---|---|
| 工具 | `wrenai_<库>_query_cube` | `wrenai_<库>_run_sql` |
| 应用侧记录的 SQL | 无 SQL，只有 `cube_query`（查询定义）<br>（见 `src/agent/subagents/check_progress.py` 的 `_CUBE_TOOL_HINT`、`report_builder.py` 的 `cube_query` 分支） | 记的是**工具入参**，即模型写的语义层 SQL（`src/agent/subagents/check_progress.py::_run_sql_meta`） |
| 真正下发 MySQL 的 SQL | 引擎内部生成 | **同样由引擎生成，但工具返回体里没有** |

结论：报告里所谓「真实执行 SQL」= 模型写的 `SELECT ... FROM v_story ...`。
而这条 SQL **在物理库里跑不了**（`v_story` 是语义层视图，物理库中不存在）——用户拿去 MySQL 执行必报错。
真正能执行的版本，是 wren 引擎在 `dry_plan` 阶段把模型/视图展开成 CTE 之后的 `dialect_sql`。

---

## 二、SQL 通道完整生成链（源码级）

**通道入口是 MCP 工具**：应用侧（langchain MCP 适配器）→ `wrenai_<库>_run_sql` → wren 的 stdio MCP server 进程。

```
① 模型/用户写 SQL（引用 MDL 对象：模型 do_story / 视图 v_story）
        │
        │  MCP 工具 run_sql(sql, limit)            wren/mcp_server.py:90
        ▼
② _query_with_limit_probe(ctx, sql, limit)        wren/mcp_server.py:73
        │   effective_limit = min(limit or 1000, 10000)；探测 limit+1 行判断截断
        ▼
③ WrenEngine.query(sql, limit+1)                  wren/engine.py:107
        │
        ├─(3.1) dialect_sql = self.dry_plan(sql)  wren/engine.py:114  ← ★物理 SQL 在这一步产生
        │
        └─(3.2) connector.query(dialect_sql, limit) wren/engine.py:117
                 └ MySqlConnector.query()          wren/connector/mysql.py:88
                     · _apply_limit(sql, limit) → SQL 尾部追加 LIMIT n
                     · cursor.execute(sql)          ← ★真正打到 MySQL 的语句
                     · _build_mysql_arrow_table(cursor) → Arrow 表
```

`dry_plan` 内部 6 步（`wren/engine.py::_plan`，163-237 行）：

| 步 | 动作 | 关键代码 |
|---|---|---|
| 1 | 用 **sqlglot**（目标方言 mysql）解析用户 SQL 成 AST | `parse_one(sql, dialect=get_sqlglot_dialect(data_source))` |
| 2 | 策略校验：strict/黑名单时禁止访问 manifest 之外的对象 | `validate_sql_policy(ast, queryable_names, config)`（`wren/policy.py`） |
| 3 | 收集 AST 里所有表引用 → 解析成 MDL 规范名 | `resolve_model_name(t.name, quoted, queryable_names)` |
| 4 | **抽取最小 manifest**（只含被引用的模型/视图及其依赖） | `get_manifest_extractor(manifest_str).extract_by(tables)` → `to_json_base64` |
| 5 | 起 wren-core session，用 **CTERewriter** 逐对象 `transform_sql` 后注入成 CTE，再生成目标方言 | `CTERewriter(effective_manifest, session, data_source, fallback)` + `rewriter.rewrite(sql)` |
| 6 | 返回 dialect SQL（顺带清理 `..`） | `wren/engine.py:228-230` |

产物形态（`v_bug` 视图被展开、模型变成 CTE）：

```sql
WITH do_bug AS (SELECT `wren_src_do_bug`... FROM (SELECT ... FROM do_bug AS __source) AS do_bug) AS `wren_src_do_bug`),
     do_project AS (...), v_bug AS (...)
SELECT ... FROM v_bug WHERE ...
```

要点：
- **视图/模型只是 CTE 名**，真正扫描的是物理表（`FROM do_story AS __source` 这类），所以 dialect SQL 可以独立在 MySQL 上执行；
- 会话要求：`MySqlConnector.__init__` 会执行 `SET sql_mode=CONCAT(@@sql_mode, ',ANSI_QUOTES')`（mysql.py:76），
  故 MDL 约定的双引号标识符才被 MySQL 接受；
  实测**带 `--connection-file` 生成的 dialect SQL 用反引号**，直连跑不需要该设置（但工具里仍照做以防万一）。

### 2.1 现在能从哪拿到 dialect SQL

| 途径 | 位置 | 说明 |
|---|---|---|
| MCP 工具 `dry_plan(sql) -> str` | `wren/mcp_server.py:159-164`（实现即 `return ctx.engine.dry_plan(sql)`） | 返回展开后的**目标方言 SQL 字符串**；**SQL 通道最直接的入口**（要作为工具结果附加时注意：它返回 str，不是 dict） |
| MCP 工具 `query_cube(..., sql_only=True)` | `wren/mcp_server.py:151-152` | 返回 `{"sql": <cube 层 SQL>}`（注意：**不是** dialect SQL，仍需再 `dry_plan` 一次） |
| 错误元数据 `metadata.DIALECT_SQL` | `wren/engine.py:125 / 141 / 211 / 236` | 只在失败路径带出，成功路径拿不到 |
| Python API | `WrenEngine.dry_plan()` / `.query()` | 进程内调用，可一次拿到两者 —— **在自建 stdio 封装里使用（方案 B2）** |
| CLI | `wren dry-plan --sql "<SQL>" --connection-file config/xxx.json` | 人工验证用；**必须带连接文件**，否则方言不对 |

行数上限（`wren/mcp_server.py:23-24`）：`DEFAULT_ROW_LIMIT = 1000`、`MAX_ROW_LIMIT = 10000`，
且探针多取 1 行判断是否截断（返回体里的 `truncated`）。

---

## 三、改造方案（MCP 前提下 4 选 1，含取舍）

### 方案 A：应用侧补一次 MCP `dry_plan`（最小改动，先打通）

`run_sql` 成功后在**代码里**调一次同一 server 的 `dry_plan(sql)`，把返回字符串作为 `dialect_sql` 挂到结果上。

- 优点：**完全不改 wren**（不 fork、不打补丁）；`dry_plan` 与 `run_sql` 内部调用的是同一个纯函数，
  同一 MDL + 同一 profile ⇒ 结果必然一致。
- 缺点：多一次 MCP 往返 + 多一次 plan 计算（视图展开后 SQL 可达 10 KB+，毫秒~百毫秒级）。
- ⚠️ **必须由中间件/包装器调用，不能让 LLM 自己调**（三处中间件都把它当 exec 工具，见 §0）。
- 落地位置（建议 2+3 先做）：
  1. `src/agent/tools/mcp_tool.py`：包装 `wrenai_*` 的 `run_sql` 工具（langchain 的 `StructuredTool` 可包装 `coroutine`），
     调完原工具后再向同一 server 请求 `dry_plan`；
  2. `src/agent/subagents/check_progress.py`：在 `result["sql"]` 旁补 `result["dialect_sql"]`；
  3. `src/agent/tools/report_builder.py`：SQL 区块两段式展示（见 §四）。

### 方案 B：自建 stdio MCP 封装（推荐，长期最稳）

关键：**工具名/前缀保持 `wrenai_<库>_*` 不变** → 应用中间件（tool_filter / query_gate / progress_boundary / feedback_type）**零改动**，
`mcp_tool.py` 只换 `command` / `args`。

**B2 直构式**（一次调用同时拿到物理 SQL 与结果，零额外往返）——复用 wren 自己的 serve 装配代码
（`wren/serve_cli.py:183` 导入、`:235` 建 engine、`:240-247` 建 `ServeContext`、`:258` 起 server）：

```python
# 自建 server 模块（例如 src/agent/mcp/wren_mcp_proxy.py）
import json
from pathlib import Path

from wren.cli import _build_engine                    # wren/serve_cli.py 同款装配
from wren.mcp_server import ServeContext, run_server  # wren 官方 server 主体

project = Path("<wren 项目>")
mdl_path = project / "target" / "mdl.json"
connection_info = json.dumps({"datasource": "mysql", "host": ..., "port": 3306,
                              "database": ..., "user": ..., "password": ...})
engine = _build_engine(str(mdl_path), connection_info, None, conn_required=True)

# 用 wren 的 run_server 起服务，但把 run_sql 换成"带 dialect_sql"的版本：
#   官方实现见 wren/mcp_server.py:73-97（_query_with_limit_probe / run_sql）
def run_sql_with_dialect(sql: str, limit: int | None = None) -> dict:
    dialect_sql = engine.dry_plan(sql)                   # ④ 真正下发物理库的 SQL
    result = _official_limit_probe(engine, sql, limit)   # 复用官方探针逻辑（limit+1 判截断）
    result["sql"] = sql                                  # 语义层 SQL
    result["dialect_sql"] = dialect_sql                  # ★ 新增字段
    result["dialect"] = "mysql"
    return result
```

> 若不想改 wren 的 `run_server`，**B1 代理式**更省事：**透传**所有工具到真正的 `wren serve mcp` 子进程，
> 只对 `run_sql` 的返回值追加 `dialect_sql`（内部再向被代理进程要一次 `dry_plan`）。
> 行为与现状最接近，风险最低。

- 优点：一次往返；绝对一致；可顺手加 sqlglot AST 黑名单（你们《备注.md》§2 的兜底防护）、只读校验、截断标记；
  升级 wren 不受影响（只用 `_build_engine` / `ServeContext` / `run_server` 这几个稳定入口）。
- 缺点：多维护一个小 server（约百行），要保证 stdio 协议干净（日志必须走 stderr）。

### 方案 C：给 wren 的 MCP server 打补丁（约 3 行，见效最快）

改 venv 内的 `wren/mcp_server.py::_query_with_limit_probe`（73-81 行），返回前塞一个字段：

```python
result = _table_to_result(table, truncated=truncated)
result["dialect_sql"] = ctx.engine.dry_plan(sql)      # ← 新增一行
result["dialect"] = "mysql"
return result
```

- 优点：立刻见效，`run_sql` 直接带 `dialect_sql`，应用侧只加一个字段读取。
- 缺点：**改的是第三方包**——`pip install -U wren` 会覆盖；需要在部署脚本里落成 patch 文件（可版本化重放），
  多实例部署时每台都要打。适合"先验证价值"，不建议长期。

### 方案 D：只在错误路径拿（现状兜底）

`metadata.DIALECT_SQL`（`wren/engine.py:125 / 141 / 211 / 236`）已有，但只覆盖失败场景，撑不起报告与审计。
可作为"失败时也把物理 SQL 落 trace"的补充。

### 方案 E：抓 DB 侧日志（不可行，不要走）

`performance_schema.events_statements_*` 对 `airead` 无权限（1142），`general_log` 默认关闭；
打开 general_log 属于改数据库配置且影响全库，违背"不动物理库"的约束。

> **推荐组合**：短期落 **方案 A 的 2+3**（当天可验证）；两周内换成 **方案 B1 代理式**（工具名不变、应用零改动）；
> 若上游愿意接受贡献，再推动把 `dialect_sql` 加进 wren 官方 `run_sql` 返回体（即方案 C 的上游化）。

---

## 四、应用侧集成点清单（Claude Code 直接照此改）

| 文件 | 现状 | 改造点 |
|---|---|---|
| `src/agent/tools/mcp_tool.py` | 208 行 `args = ["serve","mcp","--project",project]`（+ `--profile wren_mcp_<库>`）；234 行 `command=settings.WREN_BIN_PATH`；232-245 行 stdio transport/env | 方案 B/B1：`command` 改为自建封装（如 `sys.executable -m agent.mcp.wren_mcp_proxy --project <项目> --profile <p>`），**工具名与 `tool_name_prefix` 不变**；方案 A：在此包装 `run_sql` 工具 |
| `src/agent/subagents/check_progress.py` | 285 行 `_run_sql_meta(messages, i)` 从工具入参取 sql（340 行返回）；547 行 `_extract_last_sql(messages)`；1045 行写入 `result["sql"]`；1071 行写入 `result["cube_query"]` | 新增 `result["dialect_sql"]`（来源：方案 A 的 `dry_plan` 或方案 B 的工具返回） |
| `src/agent/tools/report_builder.py` | 265 行取 `result["sql"]`；267-271 行注释说明 cube 通道无 SQL；300/314 行 `sql_note`/`cube_note` 注记 | SQL 区块改成两段：**① 语义层 SQL**（模型写的，便于复现推理）+ **② 物理 SQL（真正执行，可直接在 MySQL 跑）**；cube 通道同样可标注"由 cube 编译" |
| `src/agent/middlewares/query_gate.py` | Cube 通道豁免 A/B 检索 | 不变（本次不涉及策略分流） |
| `src/agent/middlewares/langfuse_span.py` | 工具 span 记录入参 | 建议把 `dialect_sql` 写进 span output/metadata（审计与回归对账） |
| `src/agent/middlewares/query_result_offload.py` | 大结果落盘瘦身 | `dialect_sql` 属"元信息"，不要被瘦身丢掉（加入白名单字段） |

数据契约（工具/中间件之间统一）：

```json
{
  "rows": [{"...": "..."}],
  "sql": "SELECT ... FROM v_bug ...",          // 语义层 SQL（模型写的）
  "dialect_sql": "WITH do_bug AS (...) SELECT ... ",  // 物理 SQL（真正执行）
  "dialect": "mysql",
  "truncated": false,
  "row_count": 31
}
```

---

## 五、必须避开的坑（都已实测踩过）

1. **`dry_plan` 不能脱离连接/方言**：不带 connection 时按默认方言输出，会出现 `DATE_DIFF(...)`、
   `formatDateTime(...)` 这种 MySQL 里**不存在**的函数（直连会报
   `1370 execute command denied ... for routine 'witops.DATE_DIFF'`，而 `witops` 里一个存储函数都没有）。
   应用侧务必传 `--profile` / connection_info。
2. **`LIMIT` 会被追加**：`MySqlConnector.query` 用 `_apply_limit` 把 `LIMIT n` 拼到 SQL 末尾，
   所以线上实际执行的语句可能比 `dry_plan` 输出多一个 `LIMIT`；记录时请标注（或按同一 limit 复算）。
3. **引擎限制（视图/用户 SQL 都要遵守）**：不支持派生表（`FROM (SELECT ...)`）、没有 `group_concat`、
   没有 `DATE()`（要用 `CAST(x AS DATE)`）；非聚合列必须进 `GROUP BY`。
   → 用户 SQL 若含派生表，plan 阶段就会失败，`run_sql` 直接报错。
4. **cube 通道一次只能用一个时间维度**：像「每日新增 + 每日修复」这类双序列问题，
   要么两次 cube 查询，要么走 SQL 通道单条查询（见 §六验收用例）。
5. **比对结果时要归一化**：wren 的 JSON 把度量输出成字符串（`"8567"`）、把时间维度输出成
   **epoch 毫秒**（`1785542400000`），与直连 MySQL 的 `"2026-08-01"` 是同一语义的不同表示，
   不归一化会误判"结果不一致"。

---

## 六、验收用例（可直接抄进测试）

**问题**：「2026 年 8 月 1 日到 2026 年 8 月 31 日，统计该月每天的新增和修复缺陷，折线图展示。」

SQL 通道参考实现（单条 SQL 同时给两列，并用 `do_date_config` 保证 31 天齐全、缺失日补 0）：

```sql
SELECT d.`date` AS stat_date,
       (SELECT COUNT(*) FROM do_bug b
         WHERE b.deleted = 0 AND CAST(b.mention_time AS DATE) = d.`date`) AS new_cnt,
       (SELECT COUNT(*) FROM do_bug b
         WHERE b.deleted = 0 AND CAST(b.complete_time AS DATE) = d.`date`) AS fixed_cnt
FROM do_date_config d
WHERE d.deleted = 0 AND d.`date` >= '2026-08-01' AND d.`date` < '2026-09-01'
ORDER BY d.`date`
```

验收点（已实测通过，可作为回归基线）：

| 检查 | 期望 |
|---|---|
| 语义层执行结果 | 31 行；合计 **新增 1309 / 修复 1184** |
| 缺日补零 | `2026-08-02 / 08-16 / 08-23` 的 `fixed_cnt = 0`（不补零折线会错位） |
| `dialect_sql` 可用性 | 展开后约 1.1 KB，`WITH do_date_config AS (...), do_bug AS (...)`，**直连 MySQL 结果与 wren 官方结果逐行一致** |
| 口径注解 | 新增=`mention_time`（覆盖 94%）、修复=`complete_time`（覆盖 90%）；`do_bug` 无独立"解决时间"字段 |

复现命令：

```bash
set PYTHONUTF8=1
set WREN_BIN=D:\code_work_space\llm\nl2sql\.venv\Scripts\wren.exe
cd D:\code_work_space\llm\witops-wrenai

# 物理 SQL（④）
wren dry-plan --sql "<上面的 SQL>" --connection-file config\connection_mysql.json

# 语义层执行（官方结果）
wren --sql "<上面的 SQL>" --connection-file config\connection_mysql.json --output json
```

已留档的完整产物（含 cube 通道两种写法、物理 SQL、结果、ECharts 配置）：
`witops-wrenai/target/cube_sql_dump/缺陷每日新增修复/`（该目录默认不入库）。

---

## 七、模块速查（改代码时按此定位）

**wren 包（`.venv/Lib/site-packages/wren/`）**

| 文件 | 关键位置 | 作用 |
|---|---|---|
| `engine.py` | `query()` 107、`dry_plan()` 87、`_plan()` 163 | 方言 SQL 生成 + 执行；`DIALECT_SQL` 元数据 |
| `connector/mysql.py` | `MySqlConnector.__init__` 58（ANSI_QUOTES 76）、`query()` 88、`dry_run()` 96（EXPLAIN 校验） | 真正与 MySQL 通信 |
| `mdl/cte_rewriter.py` | `CTERewriter`、`get_sqlglot_dialect()` | 模型/视图 → CTE 注入 + 方言映射 |
| `mcp_server.py` | `run_sql` 90、`_query_with_limit_probe` 73、`query_cube` 114（`sql_only` 151）、`dry_plan` 159 | MCP 工具面 |
| `policy.py` | `validate_sql_policy`、`resolve_model_name` | strict 模式 / 函数黑名单（AST 级兜底） |
| `profile.py` | `add_profile` | `~/.wren/profiles.yml` 连接配置 |

**nl2sql 应用（`src/`）**

| 文件 | 作用 |
|---|---|
| `agent/tools/mcp_tool.py` | 启动 `wrenai_*` / `dbmcp` MCP 子进程；工具加载与筛选 |
| `agent/subagents/check_progress.py` | 从会话消息里取"最后一条 run_sql 的 SQL"与结果，写进 check 结果 |
| `agent/tools/report_builder.py` | 报告渲染（数据结果 / 完整数据表 / SQL 区块 / 图表） |
| `agent/middlewares/query_gate.py` | 两通道硬闸（cube 通道豁免） |
| `agent/utils/semantic_db.py` | 库 → Wren 项目映射、`wrenai_<库>` 命名 |
| `docs/WIT运营管理平台数据库-验证/语义库查询问题集.md` | 验证问题集（含附录 3：如何拿真正下发的 SQL） |

---

## 八、风险与兼容

- **只读约束不变**：本方案只增加"读取已生成的 SQL"，不新增写操作；wren 侧 `readOnlyHint=True`、
  `validate_sql_policy` 仍生效。
- **不影响 cube 通道**：cube 仍走 `query_cube`；若也想给它物理 SQL，
  可在 cube SQL 之上再做一次 `dry_plan`（cube 层 SQL → 物理 SQL），路径与 SQL 通道完全一致。
- **性能**：方案 A 每次多一次 MCP 往返 + 一次 plan（视图展开，10 KB 级 SQL 生成）；
  方案 B 零额外开销（`run_sql` 一次调用内完成）；方案 C 同样零额外开销。建议 A 只在"要出报告/要留档"的路径上取。
- **升级兼容**：方案 B 只用 wren 的稳定入口（`_build_engine` / `ServeContext` / `run_server` / `WrenEngine`），
  升级不受影响；方案 A 依赖 MCP `dry_plan(sql) -> str` 签名稳定；**方案 C 改的是 venv 内第三方包，`pip install -U` 会被覆盖**，
  必须落成可重放的 patch。
- **stdio 协议约束（自建封装时）**：stdout 是 MCP 协议通道，**任何日志必须走 stderr**
  （wren 官方 `_print_connection_help` 的注释也是这个原因）；`WREN_LOG_LEVEL=ERROR`（`mcp_tool.py:238`）保持。
- **只读**：不要给自建封装加 `--allow-write` / `allow_write=True`，与现状一致。
