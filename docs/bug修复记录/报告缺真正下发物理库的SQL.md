# 报告里的「执行 SQL」在物理库里跑不了 / Cube 通道整节没有 SQL

**日期**：2026-09-15
**类型**：链路缺口（工具返回体丢弃了真正执行的语句）+ 记录错了对象（记的是工具入参）
**严重程度**：中高（报告是交付物，用户要拿 SQL 复核结论；现有 SQL 粘进 MySQL 必报错）
**参考**：[SQL通道-SQL生成实现思路方案.md](../SQL通道-SQL生成实现思路方案.md)（问题与链路分析）

---

## 一、问题描述

走 wren 语义层的两条通道，生成报告里都拿不到**真正下发到目标库执行**的语句：

| 通道 | 报告原来记录的东西 | 粘进 MySQL 的结果 |
|---|---|---|
| Cube `wrenai_<库>_query_cube` | 只有「查询定义」（cube/measures/dimensions），**整节没有 SQL** | —（无可执行内容） |
| SQL `wrenai_<库>_run_sql` | 工具**入参**，即模型写的**语义层 SQL**（`FROM v_story` / `do_bug`） | 报错：`v_story`/`do_bug` 是 MDL 对象，物理库不存在 |

### 复现条件

- 当前数据库已建模（走语义层，工具前缀 `wrenai_*`）；
- 报告需要 SQL 节：Cube 通道**必然**（一条 SQL 都没有），SQL 通道**必然**（记的是不可执行的那份）。

---

## 二、根因分析

### 真语句在 wren 引擎内部产生后就被丢掉了

```
模型写 SQL（引用 do_* / v_*）
  └─ MCP 工具 run_sql / query_cube          wren/mcp_server.py:90 / 114
       └─ _query_with_limit_probe           wren/mcp_server.py:73
            └─ WrenEngine.query(sql, limit+1)        wren/engine.py:107
                 ├─ dialect_sql = dry_plan(sql)      wren/engine.py:114  ← ★真语句在这里产生
                 └─ connector.query(dialect_sql, limit)                   ← ★这里才是真正打库的
```

- `dry_plan` 把 MDL 视图/模型展开成 CTE（只扫物理表），产物可直接独立执行；
- 但工具**返回体只有** `{columns, rows, row_count, truncated}`——编译出的语句从未回传；
- Cube 通道更彻底：`query_cube` 生成的 cube 层 SQL 也只用于内部执行，报告只能拿到查询定义。

### 为什么不能「直接把 SQL 内联进 check 结果」

物理 SQL 实测 **3.5~10.9 KB**（`witops-wrenai/target/cube_sql_dump/*.mysql.sql`），
超过 `MessageSlimmerMiddleware` 的 8000 字符落盘阈值 → 整条 check 结果会被替换成
「前 1000 字符预览」，**连 `full_result_files` 指针和结果摘要一起丢**。
所以全量 SQL 必须落盘，check 结果里只放指针。

---

## 三、解决方案

**进程内复算**（不新增 MCP 往返：MCP 每次调用要新起 `wren serve mcp` 子进程，实测 2.5~4.2 s；
复算建引擎 ~0.9 s 每进程一次、之后 `dry_plan` 63~367 ms）。

### 数据流

```
子 agent 的 cube / run_sql 调用
 │
 ▼  check_async_task（check_progress._enhanced_build_check_result）
 进程内复算物理 SQL（引擎按 mdl.json 指纹缓存）
 ├─ 全量 SQL 落盘 <工作区>/nl2sql_process_data/<sid>/wren_plan/<sha10>.sql
 └─ check 结果只加小字段：dialect_sql_file 指针 / dialect_sql_chars / dialect / cube_sql / 注记
 │
 ▼  build_report（report_builder）
 ├─ SQL 通道：`## N. 执行 SQL`（语义层）→ `## N+1. 执行 SQL（物理，实际下发）`
 └─ Cube 通道：`## N. 执行 SQL（由 Cube 语义层编译，实际下发）` → `## N+1. 查询定义（Cube 语义层）`
```

### 复算链路（与官方逐字节对拍）

```python
cube:  cube_query_to_sql(json.dumps(_build_cube_query(cube, ",".join(measures),
                       ",".join(dimensions), time_dimension, filters or [], limit, offset)),
                       mdl_text)
物理:  engine.dry_plan(cube_sql)  →  尾部按连接器镜像追加 LIMIT
```

- **MDL 文本取 `<项目>/target/mdl.json`**，不用 `wren.context.build_json`：MCP 的
  `query_cube` 用后者，两者**只差空白**（解析后逐键相等，已实测）；而后者
  `_load_cubes_v2` 对 `cubes/*/metadata.yml` 调 `read_text()` 不带 encoding，
  Windows 上中文 cube 元数据直接 `UnicodeDecodeError: 'gbk' codec`；
- **`wren serve mcp` 的引擎也是用 `target/mdl.json` 建的**（`serve_cli.py`），
  连接字典就是 `--profile` 展开后的 `{"datasource": ds, **profile}`——与
  `mcp_tool.wren_conn_dict` 同形（本次把它抽成了唯一真源）；
- **LIMIT 镜像**：`_query_with_limit_probe` 是 `min(limit or 1000, 10000) + 1`（多取 1 行判截断），
  `MySqlConnector._apply_limit` **无条件**追加 `\nLIMIT n`（且先去掉尾部空分号）。
  复算如实镜像，双 LIMIT 场景（cube 自带 limit）置 `dup_limit` 并在注记里如实说明，
  **不「修正」成与线上不一致的语句**；
- **全程零 DB 连接**：`dry_plan` 不碰连接器（`_get_connector` 只在 `query`/`dry_run` 里懒建）。

### 改动

| 文件 | 内容 |
|---|---|
| `src/agent/utils/wren_plan.py` | **新增**。`plan_cube_sql` / `plan_run_sql` / `write_plan_file` + 引擎 LRU(4) 缓存 + 私有 API 能力探测；任何异常 fail-open 返回 `{}` / `""` |
| `src/agent/tools/mcp_tool.py` | 抽出 `wren_conn_dict(db_name)`（profile 与复算引擎连接的唯一真源，消除漂移） |
| `src/agent/subagents/check_progress.py` | `_extract_last_cube_call`（与定义文本同一次扫描）、`_resolve_wren_ctx`（`wrenai_<库>_<工具>` → 项目 + 连接）、`_attach_physical_plan`（落盘 + 小字段）、注记三件套；两条分支接线 |
| `src/agent/tools/report_builder.py` | `_wren_plan_sql_section`（VFS 指针读盘，读不到退内联，都没有则整节不出现）+ 两条通道的节装配 |

---

## 四、验证

**新增离线脚本（`D:\tmp\`，零 DB 连接，手写 `check()`）**

| 脚本 | 覆盖 | 结果 |
|---|---|---|
| `test_wren_plan_unit.py` | LIMIT 边界与连接器镜像逐字对拍 `wren.connector.mysql._apply_limit`、引擎缓存（同键一次 / 连接或 MDL 变则重建 / 8 线程并发）、fail-open 九例、落盘幂等 | **47/47** |
| `test_wren_plan_real_api.py` | 真 wren API：**10 条留档规格**（cube_sql + 物理 SQL 逐字 == `target/cube_sql_dump/*`）、**3 条官方 CLI 对拍**（`wren cube query --sql-only` / `wren dry-plan`）、run_sql 通道、`build_json` 与 `mdl.json` 解析等价、反例固化 | **60/60** |
| `test_wren_plan_check_writeback.py` | check 结果字段（大 SQL 只放指针 / 小 SQL 内联兜底）、sidecar 内容逐字、重复 check 幂等、拿不到 plan → 零新字段、直连通道不受影响 | **25/25** |
| `test_report_physical_sql.py` | 两条通道的节序与编号连续、读盘失败退内联、都没有则退回今天的行为、结果内嵌同 SQL 时物理节仍出、双 LIMIT 注记 | **21/21** |

**既有脚本（含新增用例，原断言一字未动）**

- `test_report_cube_section.py` **14/14**（原 11，加带 plan 的一组节序用例）；
- `test_cube_full_table.py` **27/27**（原 24，加「物理节插在完整数据表之后」）；
- `test_check_progress_fix.py` **ALL PASS**；`verify_report_sql.py` **19/19**；
  `test_report_exists_head.py` **18/18**；`test_failure_report.py` **26/26**。

> 既有 fixture 没有 plan → 它们的 `"执行 SQL" not in md` 断言正好成为
> 「拿不到物理 SQL 就不造假」的降级回归。

**发版后 E2E（无前端改动）**

1. 重启后端；
2. **Cube 通道**问一个 WIT 问题 → `build_report` 后确认报告有「执行 SQL（由 Cube 语义层编译，实际下发）」节、以 `LIMIT 1001` 结尾，整段粘进 MySQL 能跑且数值与报告一致；
3. **SQL 通道**同问 → 确认「语义层 SQL + 物理 SQL」两节都在；
4. 核对 sidecar：`<工作区>/nl2sql_process_data/<会话>/wren_plan/*.sql`；
5. 回归一条不建模的直连库问题（`dbmcp_*`）→ 行为不变（不出物理节）。

---

## 五、边界与风险

| 风险 | 处置 |
|---|---|
| wren 私有 API 漂移（`_build_engine` / `_build_cube_query`） | 启动能力探测（`inspect.signature`）+ 全程 fail-open：不符就整节不出现，绝不给错 SQL |
| 复算 ≠ 实际执行（查询与报告之间语义库被重建） | 缓存键含 `mdl.json` 指纹 → 重建即换新引擎；check 紧跟子任务完成，窗口极小 |
| 双 LIMIT（cube 自带 limit） | ~~按连接器行为原样复现 + `dup_limit` + 注记说明；正常路径不触发~~ —— **该假设被线上证伪**：模型常态带 `limit` 调 cube，线上**必报**语法错。已在调用边界统一剥离、改由平台截窗（见 §八） |
| 引擎构建耗时 | 每进程一次 ~0.9 s（之后 63~367 ms），发生在子任务完成后的 check 里，不占子 agent 等待时间 |
| check 结果膨胀 | 只增指针 + `cube_sql` + 注记 ≈ 1 KB，远低于 8000 字符截断阈值；全量 SQL 只在磁盘与报告文件里 |
| 落盘/读盘失败 | 写失败 → 靠内联兜底（≤3000 字符）；读失败 → 该节不出现并如实降级，**不编造 SQL** |
| Windows GBK | 全程不调 `wren.context.build_json`；读文件一律显式 `encoding="utf-8"` |

---

## 六、发版后首跑暴露的真 bug（2026-09-16）：上下文解析**恒**失败，两条通道都拿不到物理 SQL

### 现象

发版重启后第 1 次 E2E（Cube 通道问题），报告**没有**「执行 SQL」节、退回「查询定义」节；
该节注记是**取不到物理 SQL 时的诚实文案**（本次新写的 `_CUBE_NOTE`），
check 结果里 `dialect` / `dialect_sql_chars` / `dialect_sql_file` **一个都没有**。

### 根因：从工具名**反推**库名前缀，少切了一段

```python
rest   = "WIT_query_cube"                      # 去掉 "wrenai_"
prefix = "wrenai_" + rest.rsplit("_", 1)[0]    # → "wrenai_WIT_query"  ★多留了 "query"
```

`rsplit("_", 1)` 只切掉**最后一段**，而 wren 的工具名后缀是**两段**
（`query_cube` / `run_sql` / `get_instructions` / `introspect_schema`…），
于是前缀恒为 `wrenai_WIT_query`，与 `wrenai_server_name("WIT运营管理平台数据库") == "wrenai_WIT"` **永不相等**
→ `for db in discover()` 一次都没进循环 → `return "", {}`
→ `plan_cube_sql` / `plan_run_sql` 拿到空连接，第一道判空就 `return {}` → **Cube 与 SQL 两条通道同时、稳定地**没有物理 SQL。
（本地在同一份代码上 1 分钟复现：`prefix='wrenai_WIT_query'`，`server='wrenai_WIT'`，`== prefix: False`。）

正确写法仓库里**早就有**：`langfuse_client.display_wrenai_tool_name` 用的是
「slug 长→短 + `tail.startswith(slug + "_")`」——本次改为同一套**正向最长匹配**：

```python
prefix, db = "", ""
for _d in sorted(detector.discover()):
    _p = wrenai_server_name(_d)                 # 边界取 `server名_`，防止 wrenai_WIT 吞掉 wrenai_WIT2_…
    if name.startswith(f"{_p}_") and len(_p) > len(prefix):
        prefix, db = _p, _d
```

失败面同时从 DEBUG 提到 **WARNING**（原来三种失败——工具名不匹配任何已建模库 / 库无项目路径 / 解析抛异常——全被 fail-open 静默吞掉，容器日志里零痕迹，这才是排查绕远路的真正原因）。

### 为什么 177 项离线测试没拦住

写回测试（`test_wren_plan_check_writeback.py`）为保证隔离，
**把 `_resolve_wren_ctx` 整个打桩**成 `lambda tool: (witops-wrenai, {…mysql})` —— 于是
「工具名 → 库名」这唯一依赖真实世界映射的一步**从未被测试覆盖**，25/25 全绿地放过。
真 API 套件（`test_wren_plan_real_api.py`）直接传项目路径与连接，也绕过了这一步。

### 新增回归 `D:\tmp\test_wren_ctx_resolve.py`

| 段 | 内容 | 结果 |
|---|---|---|
| A（假检测器/假连接源，14 例） | 中文库名 + 4 种真实工具名后缀、纯中文库名（哈希骨架）、前缀重叠取最长、非 wren 工具、未知 server、空工具名、无项目路径、连接源抛异常 / 返回 None、检测器抛异常 —— 全部 fail-open 不抛 | **14/14** |
| B（真 db_config + 真语义库项目，4 例，`--real`） | 真 store 解析出项目与连接（datasource=mysql）→ `plan_cube_sql` 出真物理 SQL、以 `LIMIT 1001` 结尾、小字段齐备 | **4/4** |

既有套件回归：`test_wren_plan_check_writeback` 25/25、`test_report_physical_sql` 21/21、
`test_cube_full_table` 27/27、`test_report_cube_section` 14/14、`test_check_progress_fix` ALL PASS、
`test_wren_plan_unit` 47/47 —— **断言一字未改，全绿**。

### 需重做的 E2E

后端重新发版（仅后端，无前端改动）→ 重复 §四 的 1~5 步；
**判据不变，但这次 `sql_note` 应为 `_CUBE_DEF_NOTE`（带物理节时的「来源」注记）而不是「取不到」文案**。

---

## 七、报告锚到了「最后一次 Cube 调用」，而它常常是失败的（2026-09-16）

### 现象

同一会话（生产 thread `01a0a850`）里物理 SQL 又**取不到**了。上下文解析这次是对的——
问题在**选错了调用**：报告锚在「最后一次 Cube 调用」上，而子 agent 拿到 110 行数据后
**继续试过滤**，最后一次是失败调用：

```
filters: ['story_count:gte:20']
→ Error during planning: Unknown filter dimension 'story_count' in cube 'story_delivery'
```

引擎没编译出任何语句、也没产出任何数据；复算必然同样失败 → 物理节整节消失。
而报告里的数据表来自**第 12 步那次成功调用**——`_extract_last_cube_query` 与
`_extract_last_sql` 都取「最后一条」，是同一个系统性错锚。

### 改动：锚「最后一次**成功**的调用」

`_extract_last_cube_call` 在原有扫描里给每个候选配一条结果消息
（`_call_result_message`：先按 `tool_call_id` 精确对齐，同一轮并发多次调用也分得清；
缺 id 时退回「其后第一条同名结果」），用 `_tool_result_error` 判成败：

| 判为失败的三种形态（生产实测） | |
|---|---|
| langchain_mcp_adapters 的错误包装 | 文本前缀 `Error executing tool <名>:` |
| `sql_approval._deny` 的只读拦截 | ToolMessage `status="error"` |
| 少数路径的 JSON | `{"isError": true}` / 有 `error` 但无 `rows`/`columns`/`row_count` |

**只认这三类**——宁可把失败调用当成功（少跳一次），也不把成功调用误判成失败而丢掉产出数据的定义。
判不出来一律按成功处理。

选择与降级：

- 有成功调用 → 取**最后一次成功**的；`skipped_failed` 记其后的失败调用数（序 = 消息下标 +
  消息内调用位置），注记如实说明「以上取的是最近一次成功的 Cube 调用：其后的 N 次未成功」；
- 全部失败 → **退回最后一次**（保持旧行为，不假装有成功调用），`ok=False`，
  注记「本次所有 Cube 调用都未成功（引擎未编译出语句），没有产出数据」；
- SQL 通道（`_extract_last_sql`）同一处逻辑：候选剔掉失败的 run_sql，**全部失败时退回全集**
  ——不因过滤把 SQL 节整节抹掉，此时报告里的 SQL 本就是失败的语句，但至少可查。

### 回归

新增 `D:\tmp\test_cube_last_success.py`（40 例，零网络）：

| 段 | 内容 |
|---|---|
| A~C | 锚最后一次成功（含其后 1/2/3 次失败计数）、同一 AI 消息并发两次调用按 id 对齐（正反两向）、缺 id 退回同名匹配、全部失败退回最后一次、结果消息缺失按成功处理、非 Cube 通道 / 空消息 / 非法 args → `{}` |
| D | `_tool_result_error` 10 种形态边界（含「带 rows 的 error 字段不算失败」「list[content-block] 形态」） |
| E | run_sql 通道同样锚成功的调用；全部失败退回全集；**产出明细的 SQL 仍按值/行数启发式优先**（过滤未破坏原启发式） |
| F | 三种降级注记与 `_physical_sql_note` 的窗口/双 LIMIT 文案、`_window_text` 三形态 |

---

## 八、Cube 带显式 `limit` **线上必错**（2026-09-16）

### 现象与根因

§五 把「双 LIMIT」列为「正常路径不触发」的风险——**证伪**。生产 thread `01a0a850`
最后一次 Cube 调用带 `limit: 200`，连接器回来的就是 MySQL 语法错：

```
1064, "You have an error in your SQL syntax; ... near 'LIMIT 201' at line 2"
```

条款在 wren 0.13.0 里是**两条无条件动作**叠加：

| 位置 | 行为 |
|---|---|
| `wren/cube_cli._build_cube_query` | 把 `limit`/`offset` **编译进语句**：`… GROUP BY 1 LIMIT 200` |
| `wren/mcp_server._query_with_limit_probe` | `effective_limit = 1000 if limit is None else limit`，`min(…, 10000)`，然后 `query(sql, effective_limit + 1)` |
| `wren/connector/mysql._apply_limit` | **无条件** `f"{sql.rstrip().rstrip(';').rstrip()}\nLIMIT {limit}"`（其 docstring 记录：子查询包裹方案曾因 `ER_DUP_FIELDNAME` 被否） |

即 `limit=200` → `… LIMIT 200` + `\nLIMIT 201` → 1064；
`offset=5` → `… OFFSET 5` + `\nLIMIT 1001`（MySQL 要求 LIMIT 在 OFFSET 之前）→ 同样必错；
`limit=0` → `LIMIT 0\nLIMIT 1`。本机实测三种形态见 `D:\tmp\probe_cube_limit_shapes.py`。

**不能靠 monkeypatch 修**：wrenai MCP 是 stdio 子进程，每次工具调用现起
（`settings.WREN_BIN_PATH serve mcp --project … --profile …`），父进程打补丁够不着；
改 site-packages 又依赖 wren 私有的脆弱路径。

### 改动：调用边界剥离 + 平台侧截窗

与仓库里既有的同族护栏同构（`sql_approval._normalize_wrenai_limit` 早已在剥 run_sql
正文尾部的 LIMIT，防服务端双重追加；SQL 生成 skill 也写明「run_sql 正文不写 LIMIT」）——
cube 通道此前缺的正是这一层：

- **剥离**（唯一真源 `agent/utils/wren_plan.strip_cube_window`）：`sql_approval._normalize_wrenai_cube`
  就地把 `limit`/`offset` 从 tool_call args 里摘掉（`sql_only=True` 不动——只看生成的 SQL 时，
  剥了反而看不出模型要的 LIMIT 长什么样）；随后 wren 走 `limit=None` 路径 → 只有一条 `LIMIT 1001`。
- **截窗**（`_apply_cube_window`）：结果返回后按 `(offset, limit)` 在 `rows` 上截取（语义等值
  MySQL 的 `LIMIT n OFFSET m`），同步 `row_count`、置 `truncated`、附注记说明「行数是平台截的、
  wren 侧这两个参数会导致语法错才剥离」。只在结果确实是 wren 的
  `{columns, rows, row_count, truncated}` 形态时动手，**任何异常都 fail-open 返回原结果**
  ——最坏情况是 limit 没生效、多给几行，绝不篡改/丢失数据。
- **窗口覆盖全部返回行时不加任何噪音**（`off == 0 and limit >= len(rows)` 直接原样返回）。
- **复算侧同源**：`plan_cube_sql` 用同一个 `strip_cube_window`，并把窗口以
  `window_limit` / `window_offset` 随 plan 回传 → 报告的物理 SQL 与**实际下发的那条**逐字一致
  （`dup_limit=False`、只有一条 LIMIT），物理节注记同时说明窗口是平台在结果集上截的、
  窗口大于 1000 行时最多只取 1000 行。
- `build_sql_approval_middleware` 挂载判据从「有 run_sql 工具」放宽为「有 run_sql **或** query_cube 工具」。

### 回归

- `D:\tmp\test_sql_readonly.py` **56/56**（+19 例）：剥离后 args、截窗（offset/limit/仅其一）、
  `row_count`/`truncated`/注记、窗口覆盖全部行零改动、`sql_only=True` 不剥、
  6 种非 JSON/错误/空 rows 形态原样返回、run_sql 尾部 LIMIT 归一未被截胡、异步路径同断言。
- `D:\tmp\test_wren_plan_unit.py` **54/54**：`_build_cube_query` 收到 `(None, None)`、
  剥离后 `dup_limit=False`、`window_*` 回传、无窗口时无 LIMIT、`strip_cube_window` 纯函数/非法值/非 dict。
- `D:\tmp\test_wren_plan_real_api.py` **58/58**（真 wren API，零 DB 连接）：带窗口的规格
  `cube_sql` 与「不带窗口版」**逐字相同**、物理 SQL 只有一条 LIMIT、`window_*` 回显、
  `limit_appended == 1001`、**CLI（不传 `--limit`）与剥离后的 cube_sql 逐字节相同**。
  留档基线里带窗口的两条改为断言「等价于不带窗口版」（基线是旧路径产物，不再逐字可比），
  PART 2 的 CLI 对拍也统一**不传 `--limit`**（那才是线上形态）。

既有套件回归：`test_wren_plan_check_writeback` 25/25、`test_report_physical_sql` 21/21、
`test_cube_full_table` 27/27、`test_report_cube_section` 14/14、`test_check_progress_fix` ALL PASS、
`test_wren_ctx_resolve` 14/14、`test_cube_last_success` 40/40。

> `D:\tmp\query_tools_io.py` 是排查用的 Langfuse trace dump 脚本（非回归套件），
> 依赖 Langfuse v4 write mode 的 observations v2 API，本机 404，与本次改动无关。

### 发版后 E2E 追加判据

（a）Cube 通道问一个**模型会带 limit** 的问题（如 Top-N），确认不再出现
`1064 … near 'LIMIT N'`，且数据表行数 = 模型要的 N；
（b）该报告物理节注记含「平台已改为在结果集上截取」；
（c）子 agent 中途试过滤失败的问题，确认报告物理节仍在（锚在最近一次成功调用），
注记含「以上取的是最近一次成功的 Cube 调用」。

