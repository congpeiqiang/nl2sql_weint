# sql_approval.py 代码解读

> 文件路径：`src/agent/middlewares/sql_approval.py`（435 行）
> 解读日期：2026-09-18

## 一句话概括

`SqlReadOnlyMiddleware`：三合一职责——① run_sql 类工具的 **SQL 只读硬拦截**（写/DDL 直接拒绝执行）；② Wren 语义层 run_sql 的**双重 LIMIT 防御性归一**；③ Cube 通道 limit/offset 的**剥离 + 平台侧结果截窗**。

## 历史沿革

v1 曾用 langchain 1.x `HumanInTheLoopMiddleware`（after_model interrupt）做写/DDL **人工审批**（前端 ToolApprovalInterrupt + `POST /api/threads/{tid}/sql-approval` 恢复）。2026-08-28 产品要求「执行的 SQL 只运行查询操作，其余一律禁止」→ 升级为**硬拦截**，写/DDL 无审批通道；前端「SQL 审批」开关（`configurable.sql_approval_policy`）对写/DDL 不再生效。`classify_sql` 保留（eval 复用 + 前端只读展示），API 端点 `api/sql_approval.py` 不再被触发但保留不动（避免破坏旧协议）。

## ① 只读硬拦截 `classify_sql`

分类流程：
1. `_strip_sql`：剥块注释、行注释、字符串字面量（替换成 `''`）——防关键词在字面量里误判；
2. 按顶层分号拆多语句，**取最严结论**（write > full_dump > read）；
3. 每语句取首关键词：
   - 在 `_WRITE_LEADING` 黑名单（INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE/GRANT/SET/USE/COPY/VACUUM...）→ `("write", kw)` 拒绝；
   - `WITH` 开头 → 再查 CTE 后是否跟 INSERT/UPDATE/DELETE/REPLACE/MERGE（**WITH...INSERT INTO 形态**），有则 write；否则按 SELECT 规则检查；
   - 不在 `_READ_LEADING`（SELECT/WITH/SHOW/DESCRIBE/EXPLAIN/PRAGMA/LIST/HELP）也非 SELECT → **无法识别一律按写处理（保守拦截）**；
   - SELECT 且无 WHERE/LIMIT/聚合/GROUP BY → 标 `full_dump` 但**仍放行**（只是分类信息，含全表拉取）。

拒绝返回 `status="error"` ToolMessage：「系统为只读查询系统，仅允许执行 SELECT…请改用 SELECT 查询，或向用户说明无法执行数据修改」。

## ② Wren run_sql 双重 LIMIT 归一 `normalize_semantic_limit`

**病灶**：Wren 服务端 run_sql **无条件**追加 `LIMIT {limit+1}`（_query_with_limit_probe 多取一行探测截断），模型 SQL 自带尾部 LIMIT 会变成 `…LIMIT n\nLIMIT n+1` → MySQL 1064。而子 agent 各 skill 仍残留直连时代「始终加 LIMIT」旧指令，模型常自带。

**处置**（仅 `wrenai_*_run_sql`，dbmcp 直连自身幂等无需处理）：
- `_strip_trailing_noise` 循环剥尾部分号/注释（注释起点前须为空白或串首，防误伤字符串里的 `--`）；
- 匹配**最外层尾部**整数 LIMIT → 剥掉，`limit = min(SQL自带n, 显式limit)`（无显式则取 n，保 top-N 语义，绝不违背二者任一意图）；
- offset 形态（`LIMIT o,c` / `LIMIT n OFFSET m`）无法用单值 limit 表达 → **跳过不剥**（零改动返回）。

## ③ Cube limit/offset 剥离 + 平台侧截窗

**病灶不同**：`query_cube` 把 limit/offset **编译进 Cube SQL**，连接器随后又无条件追加 LIMIT：
- `limit=200` → `…LIMIT 200` + `\nLIMIT 201` → MySQL 1064（生产 thread 01a0a850 实测 near 'LIMIT 201'）；
- `offset=5` → `…OFFSET 5` + `\nLIMIT 1001` → LIMIT 必须排在 OFFSET 前 → 必错。
即这两个参数在 wren 侧**不可用**。

**处置**：
- `_normalize_wrenai_cube`：调用边界**就地改写 args** 剥离 limit/offset（复用 `agent.utils.wren_plan.strip_cube_window`，与复算侧同源）；`sql_only=True`（只看生成 SQL 不执行）不动参数；
- `_apply_cube_window`：结果返回后在结果集上按 `(offset, limit)` 切片补齐语义（等值 MySQL `LIMIT n OFFSET m`），更新 row_count/truncated 并附注记「平台已截窗，wren 会把参数编译进语句导致语法错」。守卫：只在标准 `{columns,rows,...}` JSON 形态动手；窗口覆盖全部行 → 零改动不加噪音；**fail-open——宁可不截也绝不篡改/丢失数据**。

## 工厂与挂载

`build_sql_approval_middleware(tools)`：扫描工具列表，无 run_sql 也无 query_cube 类工具 → 返回 None 跳过挂载；有则挂 `SqlReadOnlyMiddleware` 并 log 闸门清单。位于 QueryGate **内层**（最内硬闸）。

## 关联文件

- `query_gate.py`：外层通道/顺序闸门
- `agent/utils/wren_plan.py`：strip_cube_window 同源剥离规则
- `langfuse_span.py`：deny 的 `Error:` 前缀被识别为 exec 失败
