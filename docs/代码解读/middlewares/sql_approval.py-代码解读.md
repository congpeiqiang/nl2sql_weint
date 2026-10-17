# sql_approval.py — SqlReadOnlyMiddleware 代码解读

> 源文件：`src/agent/middlewares/sql_approval.py`
> 类别：**安全护栏**
> 挂载点：`wrap_tool_call` / `awrap_tool_call`
> 行数：~435 行

## 1. 解决的问题

三合一职责：SQL 只读硬拦截、Wren LIMIT 归一、Cube limit/offset 剥离。

## 2. 职责一：SQL 只读硬拦截

### 2.1 分类流程

`classify_sql(sql_text)` 确定性分类：

1. **剥注释和字符串字面量**：去掉 `--` 行注释、`/* */` 块注释、单引号/双引号字符串
2. **按顶层分号拆多语句**：取最严结论（任一语句为写操作 → 整体为写）
3. **首关键词判定**：首关键词在黑名单（INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE/SET/USE/COPY/GRANT/REVOKE/REPLACE/MERGE/LOAD）→ 写操作
4. **WITH...INSERT 形态**：`WITH` 开头再查 CTE 后是否跟 INSERT/UPDATE
5. **无法识别**：一律按写处理（保守 fail-close）

### 2.2 分类结果

| 类别 | 行为 |
|---|---|
| `read_only` | 放行执行 |
| `full_dump`（SELECT 无 WHERE/LIMIT/聚合/GROUP BY） | 放行，但标记为全表扫描 |
| `write` / `ddl` | 直接拒绝，不执行不审批 |
| `unknown` | 按写处理，拒绝 |

### 2.3 历史演变

曾用 `HumanInTheLoopMiddleware` 人工审批，2026-08-28 产品要求升级为**硬拦截**——前端"SQL 审批"开关对写/DDL 不再生效。

## 3. 职责二：Wren run_sql 双重 LIMIT 归一

### 3.1 问题

Wren 服务端无条件追加 `LIMIT {limit+1}` 探测截断。如果 SQL 自带尾部 LIMIT，变成双 LIMIT → MySQL 1064 语法错误。

### 3.2 归一化逻辑

`normalize_semantic_limit(sql, limit_param)`：

1. 循环剥掉**最外层尾部**整数 LIMIT（先循环剥尾部分号/注释再匹配）
2. 折算进 limit 参数：`min(sql_limit, param_limit)`（保 top-N 语义）
3. offset 形态无法表达则跳过归一

```sql
-- 输入
SELECT * FROM users LIMIT 100
-- 参数 limit=50
-- 归一后
SELECT * FROM users
-- limit=50（取 min(100, 50)）
```

### 3.3 作用范围

仅作用于 `wrenai_*_run_sql`。dbmcp 通道自身幂等无需处理。

## 4. 职责三：Cube limit/offset 剥离 + 平台侧截窗

### 4.1 问题

`query_cube` 的 limit/offset 参数会被编译进 Cube SQL，连接器又追加 LIMIT → 语法错误（生产 thread 01a0a850 实测）。

### 4.2 机制

1. **调用边界剥离**：从 `query_cube` 参数中就地剥离 limit/offset
2. **结果侧补切**：`_apply_cube_window(result, offset, limit)` 在结果集上按 `(offset, limit)` 切片
3. 只在 `{columns, rows, ...}` 标准形态动手
4. 窗口覆盖全部行 → 零改动不加噪音
5. fail-open：宁可不截也不篡改数据

## 5. 工厂函数

`build_sql_approval_middleware(tools)` 工厂：

- 工具列表中有 `run_sql` 或 `query_cube` → 返回中间件实例
- 无相关工具 → 返回 None，跳过挂载

## 6. 关键设计决策

### 6.1 为什么硬拦截而非审批

- 生产环境不允许 agent 执行写操作（数据安全第一）
- 审批流程增加延迟和用户操作成本
- 写操作应由 DBA 手动执行，不在 agent 职责范围

### 6.2 为什么 LIMIT 归一在中间件层

- Wren server 行为不可控（服务端总是追加 LIMIT）
- 模型可能不知道 Wren 的 LIMIT 行为，写出冲突 SQL
- 中间件层统一处理，模型无需感知底层差异

### 6.3 Cube 窗口为什么在结果侧补切

- Cube SQL 的 LIMIT/OFFSET 编译行为不可控
- 平台侧切片确保语义正确
- 对模型透明——模型只管传 limit/offset 参数
