# SQL 性能优化规则集

> 本规则集基于 sqlglot 30.12.0 和 sqlfluff 4.2.2 的实测能力整理，供 LLM 在性能优化环节逐条对照检查 SQL。

## 规则总览

| # | 规则 | 严重度 | 检测方式 | 优化建议 |
|---|------|:---:|---------|---------|
| 1 | SELECT * | high | 检测 SELECT 列表含 `*` | 明确列出所需列 |
| 2 | SELECT 无 LIMIT | high | 检测 SELECT 无 LIMIT 子句 | 添加 LIMIT |
| 3 | JOIN 无 ON 条件 | high | 检测 JOIN 无 ON 子句 | 添加 ON 条件 |
| 4 | 子查询 | medium | 检测 FROM/WHERE 中的子查询 | 改写为 JOIN |
| 5 | 函数包裹列 | medium | 检测 WHERE 中函数包裹列 | 避免索引失效 |
| 6 | NOT IN | medium | 检测 NOT IN 子查询 | 改写为 NOT EXISTS |
| 7 | DISTINCT | low | 检测 SELECT DISTINCT | 评估是否必要 |
| 8 | LIKE 前缀通配符 | medium | 检测 LIKE 以 `%`/`_` 开头 | 避免索引失效 |
| 9 | OR 条件 | low | 检测 WHERE 中 OR 条件 | 考虑 UNION ALL |
| 10 | LIMIT 无 ORDER BY | medium | 检测 LIMIT 但无 ORDER BY | 添加 ORDER BY |

---

## 规则详解

### 规则 1：SELECT *（严重度：high）

**问题**：`SELECT *` 返回所有列，可能返回大量不必要的数据，增加网络传输和内存开销。

**检测**：SELECT 列表包含 `*` 通配符。

**示例**：
```sql
-- 问题
SELECT * FROM users WHERE age > 30
-- 优化
SELECT id, name, age FROM users WHERE age > 30
```

**注意**：`COUNT(*)` 是合法的聚合用法，不算问题。

---

### 规则 2：SELECT 无 LIMIT（严重度：high）

**问题**：无 LIMIT 的 SELECT 可能返回全表数据，导致内存溢出或响应缓慢。

**检测**：SELECT 语句无 LIMIT 子句。

**示例**：
```sql
-- 问题
SELECT name FROM users
-- 优化
SELECT name FROM users LIMIT 100
```

**注意**：聚合查询（GROUP BY）通常不需要 LIMIT，需结合业务判断。

---

### 规则 3：JOIN 无 ON 条件（严重度：high）

**问题**：JOIN 无 ON 条件会产生笛卡尔积，数据量爆炸。

**检测**：JOIN 子句无 ON 条件。

**示例**：
```sql
-- 问题（笛卡尔积）
SELECT * FROM users u JOIN orders o
-- 优化
SELECT * FROM users u JOIN orders o ON u.id = o.user_id
```

---

### 规则 4：子查询（严重度：medium）

**问题**：FROM 或 WHERE 中的子查询可能无法有效利用索引，且可读性差。

**检测**：FROM 子句中的子查询（派生表）、WHERE 中的子查询。

**示例**：
```sql
-- 问题
SELECT * FROM (SELECT * FROM users WHERE age > 30) t WHERE t.id = 1
-- 优化（消除冗余子查询）
SELECT * FROM users WHERE age > 30 AND id = 1
```

**注意**：相关子查询（correlated subquery）改写为 JOIN 时需谨慎，确保语义一致。

---

### 规则 5：函数包裹列（严重度：medium）

**问题**：WHERE 中对列应用函数（如 `YEAR(created_at)`）会导致索引失效，全表扫描。

**检测**：WHERE 条件中函数包裹列。

**示例**：
```sql
-- 问题（索引失效）
SELECT * FROM users WHERE YEAR(created_at) = 2024
-- 优化（范围查询，可用索引）
SELECT * FROM users WHERE created_at >= '2024-01-01' AND created_at < '2025-01-01'
```

**常见函数**：YEAR()、MONTH()、DATE()、LOWER()、UPPER()、SUBSTR() 等。

---

### 规则 6：NOT IN（严重度：medium）

**问题**：`NOT IN` 子查询在子查询结果含 NULL 时返回空集，且性能较差。

**检测**：WHERE 中 `NOT IN` 子查询。

**示例**：
```sql
-- 问题
SELECT * FROM users WHERE id NOT IN (SELECT user_id FROM orders)
-- 优化（NOT EXISTS 更安全高效）
SELECT * FROM users u WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id)
```

---

### 规则 7：DISTINCT（严重度：low）

**问题**：`SELECT DISTINCT` 需要排序去重，开销大。若结果本身唯一则多余。

**检测**：SELECT 含 DISTINCT。

**示例**：
```sql
-- 问题（若 user_id 本身唯一）
SELECT DISTINCT user_id FROM orders
-- 优化
SELECT user_id FROM orders
```

**注意**：若确实需要去重，保留 DISTINCT。

---

### 规则 8：LIKE 前缀通配符（严重度：medium）

**问题**：`LIKE '%xxx'` 或 `LIKE '_xxx'` 前缀通配符导致索引失效。

**检测**：LIKE 模式以 `%` 或 `_` 开头。

**示例**：
```sql
-- 问题（索引失效）
SELECT * FROM users WHERE name LIKE '%john%'
-- 优化（若只需前缀匹配，可用索引）
SELECT * FROM users WHERE name LIKE 'john%'
```

---

### 规则 9：OR 条件（严重度：low）

**问题**：WHERE 中多个 OR 条件可能无法有效利用索引。

**检测**：WHERE 含 OR 条件。

**示例**：
```sql
-- 问题
SELECT * FROM users WHERE age > 30 OR status = 'active'
-- 优化（考虑 UNION ALL，各分支可用索引）
SELECT * FROM users WHERE age > 30
UNION ALL
SELECT * FROM users WHERE status = 'active'
```

**注意**：OR 改写为 UNION ALL 需确保无重复行问题，谨慎使用。

---

### 规则 10：LIMIT 无 ORDER BY（严重度：medium）

**问题**：`LIMIT` 无 `ORDER BY` 时结果不确定，且可能返回任意行。

**检测**：SELECT 含 LIMIT 但无 ORDER BY。

**示例**：
```sql
-- 问题（结果不确定）
SELECT name FROM users LIMIT 10
-- 优化
SELECT name FROM users ORDER BY id LIMIT 10
```

---

## 严重度分级

| 严重度 | 含义 | 处理策略 |
|:---:|------|---------|
| high | 可能导致数据量爆炸或严重性能问题 | 必须优化 |
| medium | 可能导致索引失效或性能下降 | 建议优化 |
| low | 轻微性能影响 | 视情况优化 |

## 优化原则

1. **语义不变**：优化不得改变查询结果
2. **保守优先**：不确定时保留原 SQL，仅给出建议
3. **结合 Schema**：参考 schema.json 判断列是否有索引
4. **业务判断**：LIMIT 是否必要、DISTINCT 是否多余，需结合业务语义
