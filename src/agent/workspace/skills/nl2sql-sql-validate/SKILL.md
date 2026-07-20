---
name: nl2sql-sql-validate
description: "触发条件：SQL Generation Agent已生成SQL，执行前需要语法、安全性和性能校验。本技能是SQL-of-Thought流水线的新增步骤（Step 4.5），介于SQL Generation之后、Execute之前。校验通过→执行；校验失败→标记问题后可选择进入纠错或直接修正。跳过条件：用户明确要求跳过校验；SQL为只读查询且用户信任其安全性。"
---

# NL2SQL SQL校验智能体

## 概述

SQL校验智能体是SQL-of-Thought流水线中**SQL Generation（Step 4）和 Execute（Step 5）之间的新增步骤**。它对生成的SQL进行三层校验，确保执行前SQL的正确性、安全性和性能。

## 在流水线中的角色

```
输入：  生成的SQL (Y) + 裁剪后的Schema (S) + 自然语言问题 (Q)
输出：  校验结果（通过 / 警告 / 阻断）+ 必要时修正后的SQL
下一步：→ 通过 → 执行SQL
        → 警告 → 记录问题后执行
        → 阻断 → 返回SQL Generation修正 或 自动修正
```

## 校验流程

### 第1步：语法与Schema校验（wren dry-plan）

使用 WrenAI 干运行验证SQL，检查语法和Schema一致性：

```bash
wren dry-plan --sql "<生成的SQL>" -d duckdb
```

| 检查项 | 说明 |
|--------|------|
| SQL语法错误 | `dry-plan` 自动检测语法问题 |
| 表不存在 | MDL模型中无此表 |
| 列不存在 | 表中无此列 |
| JOIN关系不合法 | 外键关系不匹配 |

> 如果 WrenAI MDL 不可用，回退到人工对照Schema检查。

### 第2步：安全性校验

逐条检查SQL是否包含危险操作：

| 规则 | 检查方式 | 级别 |
|------|---------|------|
| 禁止 `DROP TABLE/VIEW/DATABASE` | 正则匹配 `DROP` | **阻断** |
| 禁止 `ALTER TABLE` | 正则匹配 `ALTER` | **阻断** |
| 禁止 `TRUNCATE` | 正则匹配 `TRUNCATE` | **阻断** |
| `DELETE` 必须有 `WHERE` | 检查DELETE后是否有WHERE子句 | **阻断** |
| `UPDATE` 必须有 `WHERE` | 检查UPDATE后是否有WHERE子句 | **阻断** |
| 包含 `INSERT` / `CREATE` | 检测到则标记 | **警告** |

**阻断** = 拒绝执行，必须修正。**警告** = 记录但不阻止。

### 第3步：性能校验

检查常见性能反模式：

| 规则 | 说明 | 级别 |
|------|------|------|
| `SELECT *` | 在大表上使用 `SELECT *` 浪费IO | **警告** |
| 大表无 `LIMIT` | Artist(275行)无关紧要，Track(3503行)建议加LIMIT | **提示** |
| `ORDER BY` 无索引列 | 非主键排序可能慢 | **提示** |
| 多次嵌套子查询 | 3层以上子查询 | **警告** |
| `CROSS JOIN` | 笛卡尔积 | **警告** |

大表判断标准：行数 > 1000 的视为大表。基于Chinook数据库：
- 大表：Track(3503), InvoiceLine(2240), Invoice(412), PlaylistTrack
- 小表：Artist(275), Album(347), Customer(59), Employee(8), Genre(25), MediaType(5), Playlist(18)

### 第4步：自动修正

对于可自动修正的问题，直接修正：

| 问题 | 自动修正 |
|------|---------|
| SQL尾部有分号 | 移除 `;` |
| SELECT * 在具体查询中 | 替换为 Schema 中该表的所有列名 |
| 大表SELECT无LIMIT | 添加 `LIMIT 100` |

## 输出模板

```json
{
  "validation_result": "pass" | "warning" | "blocked",
  "checks": {
    "syntax": { "status": "pass", "errors": [] },
    "security": { "status": "pass", "issues": [] },
    "performance": { "status": "warning", "issues": ["SELECT * on large table Track"] }
  },
  "auto_fixes_applied": ["removed trailing semicolon", "added LIMIT 100"],
  "final_sql": "SELECT ArtistId, Name FROM Artist LIMIT 100",
  "original_sql": "SELECT * FROM Artist;",
  "recommendation": "execute" | "review" | "reject"
}
```

## 校验决策矩阵

| 语法 | 安全 | 性能 | 决策 |
|------|------|------|------|
| ✅ | ✅ | ✅ | **执行** |
| ✅ | ✅ | ⚠️ | **执行**（性能警告仅记录） |
| ✅ | 🚫 | — | **拒绝**，返回SQL Generation修正 |
| ❌ | — | — | **拒绝**，返回SQL Generation修正 |

## 与纠错循环的关系

> 本校验在**执行前**拦截问题。如果校验阻断，不进入执行阶段，直接返回SQL Generation修正。
> 这与Phase 3纠错循环不同——纠错循环是**执行失败后**才触发。

## 常见陷阱

| 陷阱 | 纠正 |
|------|------|
| 将校验结果误当纠错 | 校验是执行前检查，纠错是执行后修复 |
| 过度阻断 | 性能警告不应阻断，只记录 |
| 忽略自动修正机会 | `SELECT *` + `;` 等可以静默修正 |
| 无WrenAI时跳过全部校验 | `dry-plan`不可用时仍做安全+性能校验 |
