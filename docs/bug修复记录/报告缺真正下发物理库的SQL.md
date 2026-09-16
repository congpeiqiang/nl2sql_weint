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
| 双 LIMIT（cube 自带 limit） | 按连接器行为原样复现 + `dup_limit` + 注记说明；该场景下调用本身在 MySQL 也会语法错，正常路径不触发 |
| 引擎构建耗时 | 每进程一次 ~0.9 s（之后 63~367 ms），发生在子任务完成后的 check 里，不占子 agent 等待时间 |
| check 结果膨胀 | 只增指针 + `cube_sql` + 注记 ≈ 1 KB，远低于 8000 字符截断阈值；全量 SQL 只在磁盘与报告文件里 |
| 落盘/读盘失败 | 写失败 → 靠内联兜底（≤3000 字符）；读失败 → 该节不出现并如实降级，**不编造 SQL** |
| Windows GBK | 全程不调 `wren.context.build_json`；读文件一律显式 `encoding="utf-8"` |
