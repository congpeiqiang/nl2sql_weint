# SQL Error Taxonomy (Reference for Correction Agent)

> This is a standalone reference copy for the nl2sql-correction skill. For the orchestrator-level copy, see sql-of-thought/references/error-taxonomy.md.

## Quick Diagnosis Checklist

When diagnosing a failed SQL query, scan each category in order and check all sub-categories:

### 1. Syntax (`syntax`)
- `syntax.sql_syntax_error` — General syntax violations (malformed queries, missing keywords)
- `syntax.invalid_alias` — Incorrect or conflicting table/column alias

### 2. Schema Link (`schema_link`)
- `schema_link.table_missing` — Required table not in the query
- `schema_link.col_missing` — Required column not referenced
- `schema_link.ambiguous_col` — Column name exists in multiple tables without qualification
- `schema_link.incorrect_foreign_key` — Wrong FK column in join condition

### 3. Join (`join`)
- `join.join_missing` — Required JOIN clause omitted
- `join.join_wrong_type` — Wrong join type (INNER vs LEFT/RIGHT)
- `join.extra_table` — Unnecessary table included
- `join.incorrect_col` — Wrong column in JOIN ON condition

### 4. Filter (`filter`)
- `filter.where_missing` — Required WHERE clause omitted
- `filter.condition_wrong_col` — Filter on wrong column
- `filter.condition_type_mismatch` — Type mismatch in comparison

### 5. Aggregation (`aggregation`)
- `aggregation.agg_no_groupby` — Aggregate function without GROUP BY
- `aggregation.groupby_missing_col` — GROUP BY missing a required column
- `aggregation.having_without_groupby` — HAVING without GROUP BY
- `aggregation.having_incorrect` — HAVING condition logically wrong
- `aggregation.having_vs_where` — Post-aggregation filter in WHERE instead of HAVING

### 6. Value (`value`)
- `value.hardcoded_value` — Literal value where column reference should be
- `value.value_format_wrong` — Date/string/numeric value in wrong format

### 7. Subquery (`subquery`)
- `subquery.unused_subquery` — Subquery result not referenced in outer query
- `subquery.subquery_missing` — Required nested query not implemented
- `subquery.subquery_correlation_error` — Correlated subquery references wrong outer column

### 8. Set Operations (`set_ops`)
- `set_ops.union_missing` — Required UNION not included
- `set_ops.intersect_missing` — Required INTERSECT not included
- `set_ops.except_missing` — Required EXCEPT not included

### 9. Other Issues (`other`)
- `other.order_by_missing` — Required sorting not applied
- `other.limit_missing` — Required row count restriction omitted
- `other.duplicate_select` — Same column selected multiple times
- `other.unsupported_function` — Function not available in SQLite
- `other.extra_values_selected` — More columns in SELECT than needed

## Diagnostic Priority Order

Scan categories in this priority order:
1. **Syntax** first (blocking errors — query cannot even parse)
2. **Schema Link** (structural correctness)
3. **Join** (multi-table correctness)
4. **Aggregation** (most common logical errors)
5. **Filter** (condition correctness)
6. **Subquery** (nested logic correctness)
7. **Set Operations** (UNION/INTERSECT/EXCEPT correctness)
8. **Value** (literal/format correctness)
9. **Other** (sorting, limits, redundancy)

## Root Cause Identification

For each diagnosis:
1. **Primary root cause** — The single most impactful error category
2. **Secondary errors** — Additional issues that compound the root cause
3. **Repair strategy** — Concrete fix aligned with the identified taxonomy codes

> Remember: Use concise error codes (e.g., `aggregation.agg_no_groupby`) not verbose descriptions.