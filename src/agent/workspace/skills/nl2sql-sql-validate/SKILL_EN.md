---
name: nl2sql-sql-validate
description: "TRIGGER when: SQL Generation Agent has produced SQL and it needs syntax, security, and performance validation before execution. This skill is a new step (Step 4.5) in the SQL-of-Thought pipeline, between SQL Generation and Execute. Pass → execute; fail → flag issues, optionally enter correction or auto-fix. SKIP when: user explicitly skips validation; SQL is read-only and user trusts its safety."
---

# NL2SQL SQL Validation Agent

## Overview

The SQL Validation Agent is a **new step between SQL Generation (Step 4) and Execute (Step 5)** in the SQL-of-Thought pipeline. It performs three-layer validation on generated SQL to ensure correctness, security, and performance before execution.

## Role in Pipeline

```
Input:  Generated SQL (Y) + Cropped Schema (S) + NL Question (Q)
Output: Validation result (pass / warning / blocked) + optionally corrected SQL
Next:   → pass → Execute SQL
        → warning → Execute with logged issues
        → blocked → Return to SQL Generation for fix, or auto-correct
```

## Validation Flow

### Step 1: Syntax & Schema (wren dry-plan)

Use WrenAI dry-run to validate SQL syntax and schema consistency:

```bash
wren dry-plan --sql "<generated SQL>" -d duckdb
```

| Check | Description |
|-------|-------------|
| SQL syntax errors | Auto-detected by dry-plan |
| Table not found | Table missing from MDL |
| Column not found | Column missing from table |
| Invalid JOIN | Foreign key mismatch |

> Fall back to manual schema check if WrenAI MDL is unavailable.

### Step 2: Security Validation

Check for dangerous operations:

| Rule | Detection | Level |
|------|-----------|-------|
| No `DROP TABLE/VIEW/DATABASE` | Regex `\bDROP\b` | **Block** |
| No `ALTER TABLE` | Regex `\bALTER\b` | **Block** |
| No `TRUNCATE` | Regex `\bTRUNCATE\b` | **Block** |
| `DELETE` must have `WHERE` | Check WHERE after DELETE | **Block** |
| `UPDATE` must have `WHERE` | Check WHERE after UPDATE | **Block** |
| `INSERT` / `CREATE` present | Flag if detected | **Warn** |

**Block** = Reject execution, must fix. **Warn** = Log but don't stop.

### Step 3: Performance Validation

Check common anti-patterns:

| Rule | Description | Level |
|------|-------------|-------|
| `SELECT *` | Wasteful on large tables | **Warn** |
| Large table without `LIMIT` | Artist(275) OK, Track(3503) needs LIMIT | **Info** |
| `ORDER BY` on non-indexed column | May be slow | **Info** |
| Deep subquery nesting | 3+ levels | **Warn** |
| `CROSS JOIN` | Cartesian product | **Warn** |

Large table threshold: > 1000 rows. For Chinook:
- Large: Track(3503), InvoiceLine(2240), Invoice(412), PlaylistTrack
- Small: Artist(275), Album(347), Customer(59), Employee(8), Genre(25), MediaType(5), Playlist(18)

### Step 4: Auto-Fix

Auto-correctable issues:

| Issue | Auto-Fix |
|-------|----------|
| Trailing semicolon | Remove `;` |
| `SELECT *` in specific context | Expand to all columns from schema |
| Large table SELECT without LIMIT | Add `LIMIT 100` |

## Output Template

```json
{
  "validation_result": "pass | warning | blocked",
  "checks": {
    "syntax": { "status": "pass", "errors": [] },
    "security": { "status": "pass", "issues": [] },
    "performance": { "status": "warning", "issues": ["SELECT * on large table Track"] }
  },
  "auto_fixes_applied": ["removed trailing semicolon", "added LIMIT 100"],
  "final_sql": "SELECT ArtistId, Name FROM Artist LIMIT 100",
  "original_sql": "SELECT * FROM Artist;",
  "recommendation": "execute | review | reject"
}
```

## Decision Matrix

| Syntax | Security | Performance | Decision |
|--------|----------|-------------|----------|
| pass | pass | pass | **Execute** |
| pass | pass | warn | **Execute** (log warnings only) |
| pass | block | — | **Reject**, return to SQL Generation |
| fail | — | — | **Reject**, return to SQL Generation |

## Relationship with Correction Loop

> This validation **intercepts issues before execution**. If blocked, returns to SQL Generation without entering execution. This is different from Phase 3 correction loop — which triggers **after execution failure**.

## Common Pitfalls

| Pitfall | Correction |
|---------|-----------|
| Confusing validation with correction | Validation is pre-execution; correction is post-failure |
| Over-blocking | Performance warnings should not block |
| Missing auto-fix opportunities | `SELECT *` and trailing `;` can be silently fixed |
| Skipping all checks when WrenAI absent | Still do security + performance checks without dry-plan |
