---
name: nl2sql-correction
description: "TRIGGER when: the NL2SQL pipeline's generated SQL query fails execution; user needs to diagnose and fix a failing SQL query using taxonomy-guided error correction; user wants CoT-based error analysis for SQL queries. This skill covers BOTH Step 6 (Correction Plan) and Step 7 (Correction SQL) in the SQL-of-Thought pipeline. It is only invoked when initial SQL execution produces an error or incorrect result. SKIP when: SQL execution succeeds; user wants to generate new SQL from scratch; user asks about error taxonomy without a failing query."
---

# NL2SQL Correction Agent (Plan + SQL)

## Overview

This skill covers the **Guided Correction Loop** — Steps 6 and 7 in the SQL-of-Thought pipeline. It is only invoked when the initial SQL query fails execution. The loop combines two sub-agents:

1. **Correction Plan Agent** — Diagnoses *why* the query failed using the error taxonomy + CoT reasoning → produces a Correction Plan
2. **Correction SQL Agent** — Regenerates SQL guided by the Correction Plan → produces corrected SQL

The key innovation: instead of relying solely on raw execution feedback (as in DIN-SQL and DAIL-SQL), correction is **informed by a structured error taxonomy** that categorizes *why* a query failed, not just *what* failed.

## Role in Pipeline

```
Input:  Failed SQL + Execution error + NL question (Q) + Cropped schema (S)
Output: Corrected SQL query (with Correction Plan as intermediate)
Loop:   Re-execute corrected SQL → SUCCESS (end) or ERROR (repeat, max 3 attempts)
```

## Error Taxonomy Reference

The correction loop is guided by a **9-category, 31-subcategory** error taxonomy. Load the full taxonomy from [references/error-taxonomy.md](references/error-taxonomy.md).

### Quick Reference: Taxonomy Categories

| Category | Code | Sub-categories |
|----------|------|---------------|
| **Syntax** | `syntax` | `sql_syntax_error`, `invalid_alias` |
| **Schema Link** | `schema_link` | `table_missing`, `col_missing`, `ambiguous_col`, `incorrect_foreign_key` |
| **Join** | `join` | `join_missing`, `join_wrong_type`, `extra_table`, `incorrect_col` |
| **Filter** | `filter` | `where_missing`, `condition_wrong_col`, `condition_type_mismatch` |
| **Aggregation** | `aggregation` | `agg_no_groupby`, `groupby_missing_col`, `having_without_groupby`, `having_incorrect`, `having_vs_where` |
| **Value** | `value` | `hardcoded_value`, `value_format_wrong` |
| **Subquery** | `subquery` | `unused_subquery`, `subquery_missing`, `subquery_correlation_error` |
| **Set Operations** | `set_ops` | `union_missing`, `intersect_missing`, `except_missing` |
| **Other Issues** | `other` | `order_by_missing`, `limit_missing`, `duplicate_select`, `unsupported_function`, `extra_values_selected` |

## Process

### Part A: Correction Plan Agent (Diagnosis)

#### Step A1: Collect Error Context

Gather all available information about the failure:
- The **failed SQL query** (exact text)
- The **execution error message** (from DB engine)
- The **original natural language question** (Q)
- The **cropped schema** (S)
- The **error taxonomy** (T — use concise codes)

#### Step A2: Taxonomy-Guided Error Diagnosis

Analyze the failed SQL against the taxonomy. Identify which error categories and sub-categories apply. Use **concise error codes** (not verbose descriptions) to prevent context window overflow.

```
Diagnosis Example:
Failed SQL: SELECT name, AVG(salary) FROM employees WHERE dept_id > 5
Error: Execution returns wrong results (logically incorrect)

Taxonomy Analysis:
- `aggregation.agg_no_groupby` — AVG(salary) is used without GROUP BY
- `filter.condition_type_mismatch` — dept_id > 5 is numeric comparison, but dept_id is likely a foreign key, should compare to department identifier
- `schema_link.col_missing` — dept_name is not selected, but question asks about department names

Root Cause: The query lacks GROUP BY and misuses dept_id as a numeric filter instead of joining with departments table.
```

#### Step A3: Chain-of-Thought Correction Plan

Produce a structured CoT correction plan that:
1. Identifies the **root cause** (aligned with taxonomy)
2. Explains **why** the error occurred (not just what)
3. Proposes a **concrete repair strategy**
4. Maps the repair to specific SQL modifications

```
Correction Plan Example:

Root Cause: `aggregation.agg_no_groupby` + `join.join_missing`

Why: The query computes AVG(salary) without grouping, returning a single aggregate across all employees instead of per-department. Also, dept_id is incorrectly used as a numeric filter rather than joining with departments to get department names.

Repair Strategy:
1. Add GROUP BY clause — group by department identifier (dept_name after joining)
2. Replace WHERE condition with proper JOIN — employees JOIN departments ON e.dept_id = d.id
3. Add department name to SELECT — SELECT d.dept_name, AVG(e.salary)
4. If "more than 10 people" is required → add HAVING COUNT(*) > 10

SQL Modification Points:
- Add: JOIN departments d ON e.dept_id = d.id
- Add: GROUP BY d.dept_name
- Replace: WHERE dept_id > 5 → HAVING COUNT(*) > 10 (if applicable)
- Add: d.dept_name to SELECT clause
```

#### Step A4: Format Correction Plan Output

```json
{
  "error_codes": ["aggregation.agg_no_groupby", "join.join_missing"],
  "root_cause": "description of primary failure reason",
  "cot_reasoning": "step-by-step diagnostic reasoning...",
  "repair_strategy": [
    "1. Add JOIN with departments table via dept_id FK",
    "2. Add GROUP BY d.dept_name",
    "3. Add d.dept_name to SELECT",
    "4. Replace numeric filter with proper HAVING condition"
  ],
  "sql_modification_points": [
    "Add: JOIN departments d ON e.dept_id = d.id",
    "Add: GROUP BY d.dept_name",
    "Add: d.dept_name to SELECT",
    "Replace: WHERE dept_id > 5 → HAVING COUNT(*) > 10"
  ]
}
```

### Part B: Correction SQL Agent (Regeneration)

#### Step B1: Apply Correction Plan

Use the Correction Plan as **structured guidance** to regenerate the SQL. The Correction SQL Agent:
- Takes the correction plan + question + schema + incorrect SQL as input
- Regenerates SQL while **avoiding the previous errors** identified in the plan
- Does NOT carry history from previous correction attempts (no shared scratchpad)

#### Step B2: Generate Corrected SQL

```
Original Failed SQL:
SELECT name, AVG(salary) FROM employees WHERE dept_id > 5

Correction Plan Applied:
- Add JOIN → FROM employees e JOIN departments d ON e.dept_id = d.id
- Add GROUP BY → GROUP BY d.dept_name
- Replace WHERE → Remove WHERE dept_id > 5
- Add HAVING → HAVING COUNT(*) > 10 (if "more than 10 people" required)
- Fix SELECT → SELECT d.dept_name, AVG(e.salary) AS avg_salary

Corrected SQL:
SELECT d.dept_name, AVG(e.salary) AS avg_salary
FROM employees e
JOIN departments d ON e.dept_id = d.id
GROUP BY d.dept_name
HAVING COUNT(*) > 10
```

#### Step B3: Post-Process and Validate

Apply the same post-processing rules as the SQL Generation Agent:
- Remove trailing semicolons
- Remove NL fragments
- Normalize whitespace
- Validate against schema

#### Step B4: Re-Execute

Execute the corrected SQL against the database:
- **If SUCCESS** → Pipeline ends, return result
- **If ERROR** → Re-enter correction loop (Part A again), max **3 attempts** total

## Correction Loop Rules

1. **Max 3 correction attempts**: Do not loop indefinitely
2. **No shared history**: Each correction attempt starts fresh — do NOT carry scratchpad from previous attempts
3. **No repetition of same error codes**: If the same error codes appear in consecutive attempts, flag as "stuck" and terminate
4. **Single correction pipeline**: Use one Correction Plan → one Correction SQL per attempt. Never use multiple per-error-type agents
5. **Temperature = 0**: All correction calls use temperature 0
6. **Use concise error codes**: Reference taxonomy sub-categories by code (e.g., `join_missing`) not verbose descriptions

## Failed Ablations (AVOID)

| ❌ Failed Approach | Why It Failed |
|-------------------|---------------|
| Free-form taxonomy → direct to SQL agent | LLMs struggle with unguided debugging; need structured reasoning step first |
| Multiple repair agents per error type → aggregation agent | Independent edits conflict; merge produces incoherent SQL |
| Shared scratchpad carrying history across attempts | Expands context, increases latency/cost, amplifies repetition and schema drift |
| Unguided reasoning without taxonomy | Same correction steps repeated across attempts without progress |

## Examples

### Syntax Error Correction
```
Failed SQL: SELECT AVG(salary FROM employees
Error: sql_syntax_error — missing closing parenthesis

Correction Plan:
- Error code: syntax.sql_syntax_error
- Root cause: Mismatched parenthesis in AVG() function call
- Repair: Add closing parenthesis after "salary"

Corrected SQL: SELECT AVG(salary) FROM employees
```

### Schema Link Error Correction
```
Failed SQL: SELECT e.name, d.dept_name FROM employees e JOIN departments d ON e.dept_name = d.dept_name
Error: schema_link.incorrect_foreign_key — dept_name is not the join column, dept_id is the FK

Correction Plan:
- Error code: schema_link.incorrect_foreign_key
- Root cause: Used dept_name for join condition instead of the actual FK dept_id
- Repair: Replace e.dept_name = d.dept_name with e.dept_id = d.id

Corrected SQL: SELECT e.name, d.dept_name FROM employees e JOIN departments d ON e.dept_id = d.id
```

### Aggregation Error Correction
```
Failed SQL: SELECT dept_id, AVG(salary) FROM employees WHERE COUNT(*) > 10
Error: aggregation.having_vs_where + aggregation.having_without_groupby — COUNT(*) used in WHERE instead of HAVING, no GROUP BY

Correction Plan:
- Error codes: aggregation.having_vs_where, aggregation.having_without_groupby
- Root cause: (1) Aggregate function COUNT(*) cannot appear in WHERE clause, must use HAVING; (2) GROUP BY is required for aggregation with HAVING
- Repair: (1) Move COUNT(*) > 10 from WHERE to HAVING; (2) Add GROUP BY dept_id

Corrected SQL: SELECT dept_id, AVG(salary) FROM employees GROUP BY dept_id HAVING COUNT(*) > 10
```