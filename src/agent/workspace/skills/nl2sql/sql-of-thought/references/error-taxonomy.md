# SQL-of-Thought Error Taxonomy

## Overview

This taxonomy is derived from Shen et al. and expanded by the SQL-of-Thought framework. It provides **9 categories** and **31 sub-categories** of logical errors for identification and rectification by LLMs.

**Key Design Principle**: Use **concise error codes** instead of lengthy explanations to facilitate easier identification and prevent overflowing the LLM context window.

## Category 1: Syntax (`syntax`)

| Sub-category | Code | Description |
|-------------|------|-------------|
| SQL syntax error | `syntax.sql_syntax_error` | General SQL syntax violations (malformed queries, missing keywords) |
| Invalid alias | `syntax.invalid_alias` | Incorrect or conflicting table/column alias usage |

**Diagnostic hints**: Check for missing keywords, unbalanced parentheses, misplaced commas, and alias conflicts.

## Category 2: Schema Link (`schema_link`)

| Sub-category | Code | Description |
|-------------|------|-------------|
| Table missing | `schema_link.table_missing` | Required table not included in the query |
| Column missing | `schema_link.col_missing` | Required column not referenced in the query |
| Ambiguous column | `schema_link.ambiguous_col` | Column name exists in multiple tables without qualification |
| Incorrect foreign key | `schema_link.incorrect_foreign_key` | Wrong FK column used in join condition |

**Diagnostic hints**: Cross-check every referenced table/column against the linked schema. Verify FK relationships match the database definition.

## Category 3: Join (`join`)

| Sub-category | Code | Description |
|-------------|------|-------------|
| Join missing | `join.join_missing` | Required JOIN clause omitted (query references multiple tables) |
| Join wrong type | `join.join_wrong_type` | INNER JOIN used where LEFT/RIGHT JOIN is needed, or vice versa |
| Extra table | `join.extra_table` | Unnecessary table included in the query |
| Incorrect column in join | `join.incorrect_col` | Wrong column used in the JOIN ON condition |

**Diagnostic hints**: Verify all multi-table queries have proper JOINs. Check if LEFT/RIGHT JOIN is needed for null-containing relationships. Remove tables not contributing to the result.

## Category 4: Filter (`filter`)

| Sub-category | Code | Description |
|-------------|------|-------------|
| WHERE missing | `filter.where_missing` | Required WHERE clause omitted (all rows returned instead of filtered subset) |
| Condition wrong column | `filter.condition_wrong_col` | Filter applied on wrong column (e.g., filtering on ID instead of name) |
| Condition type mismatch | `filter.condition_type_mismatch` | Type mismatch in comparison (e.g., string compared to integer, date format mismatch) |

**Diagnostic hints**: Verify WHERE conditions match the question's filtering intent. Check data types of columns used in comparisons.

## Category 5: Aggregation (`aggregation`)

| Sub-category | Code | Description |
|-------------|------|-------------|
| Aggregation without GROUP BY | `aggregation.agg_no_groupby` | Aggregate function used without GROUP BY (returns single row instead of per-group) |
| Missing GROUP BY column | `aggregation.groupby_missing_col` | GROUP BY clause missing a required grouping column |
| HAVING without GROUP BY | `aggregation.having_without_groupby` | HAVING clause present but GROUP BY omitted |
| Incorrect HAVING condition | `aggregation.having_incorrect` | HAVING condition logically wrong (wrong threshold, wrong aggregate) |
| HAVING vs WHERE confusion | `aggregation.having_vs_where` | Post-aggregation filter placed in WHERE instead of HAVING, or vice versa |

**Diagnostic hints**: Every aggregate function requires GROUP BY (unless computing a single overall aggregate). WHERE filters rows before grouping; HAVING filters groups after grouping. Non-aggregated columns in SELECT must appear in GROUP BY.

## Category 6: Value (`value`)

| Sub-category | Code | Description |
|-------------|------|-------------|
| Hard-coded value | `value.hardcoded_value` | Literal value used where a column reference or parameter should be |
| Value format wrong | `value.value_format_wrong` | Date, string, or numeric value in incorrect format (e.g., '2024-01-01' vs '01/01/2024') |

**Diagnostic hints**: Check if literal values match expected data formats. Prefer column references over hard-coded values when the question implies dynamic comparison.

## Category 7: Subquery (`subquery`)

| Sub-category | Code | Description |
|-------------|------|-------------|
| Unused subquery | `subquery.unused_subquery` | Subquery result not referenced in the outer query |
| Subquery missing | `subquery.subquery_missing` | Required nested query not implemented (question implies comparison against computed values) |
| Subquery correlation error | `subquery.subquery_correlation_error` | Correlated subquery references wrong outer column or lacks proper correlation condition |

**Diagnostic hints**: Questions like "higher than average" or "more than the maximum of..." typically require subqueries. Verify correlated subqueries reference the correct outer table columns.

## Category 8: Set Operations (`set_ops`)

| Sub-category | Code | Description |
|-------------|------|-------------|
| UNION missing | `set_ops.union_missing` | Required UNION operation not included (question implies combining multiple result sets) |
| INTERSECT missing | `set_ops.intersect_missing` | Required INTERSECT operation not included (question implies finding common elements) |
| EXCEPT missing | `set_ops.except_missing` | Required EXCEPT operation not included (question implies exclusion of certain results) |

**Diagnostic hints**: Questions mentioning "both", "common", "also in", "but not in", "except", "excluding" often require set operations. Verify column compatibility between operands (same number and type of columns).

## Category 9: Other Issues (`other`)

| Sub-category | Code | Description |
|-------------|------|-------------|
| ORDER BY missing | `other.order_by_missing` | Required sorting not applied (question implies ordering like "top", "highest", "lowest") |
| LIMIT missing | `other.limit_missing` | Required row count restriction omitted (question implies "top N", "first N") |
| Duplicate SELECT | `other.duplicate_select` | Same column selected multiple times |
| Unsupported function | `other.unsupported_function` | Function not available in SQLite (e.g., ROW_NUMBER without proper syntax) |
| Extra values selected | `other.extra_values_selected` | More columns in SELECT than the question requires |

**Diagnostic hints**: "Top 5", "highest", "lowest" imply ORDER BY + LIMIT. Check for redundant column selections. Verify all functions are SQLite-compatible.

## Usage in Correction Loop

When diagnosing a failed SQL query:

1. **Scan for each category** in order: syntax → schema_link → join → filter → aggregation → value → subquery → set_ops → other
2. **Assign concise error codes** (e.g., `aggregation.agg_no_groupby`) rather than verbose descriptions
3. **Identify root cause** — the primary error that led to the failure
4. **Note secondary errors** — additional issues that may compound the root cause
5. **Build CoT correction plan** using the identified error codes as structured guidance

## Example Diagnosis

```
Failed SQL: SELECT name AVG(salary) FROM employees WHERE dept_id > 5

Diagnosis:
- syntax.sql_syntax_error: Missing comma between SELECT columns
- aggregation.agg_no_groupby: AVG() without GROUP BY
- schema_link.incorrect_foreign_key: dept_id used as numeric filter instead of join key
- filter.condition_type_mismatch: dept_id compared to number 5 (should reference department table)

Primary root cause: aggregation.agg_no_groupby (most impactful)
Secondary: schema_link.incorrect_foreign_key, syntax.sql_syntax_error
```