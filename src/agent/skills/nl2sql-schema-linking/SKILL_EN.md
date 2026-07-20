---
name: nl2sql-schema-linking
description: "TRIGGER when: the NL2SQL pipeline needs to identify relevant database tables, columns, primary keys, foreign keys, and join relationships for a natural language question; user wants schema context extraction for a query; user asks which tables/columns are needed for a given question. This skill is Step 1 in the SQL-of-Thought pipeline and should be used BEFORE subproblem decomposition and query planning. SKIP when: user provides a complete schema context already; user asks SQL questions without needing schema discovery."
---

# NL2SQL Schema Linking Agent

## Overview

The Schema Linking Agent is the **first step** in the SQL-of-Thought pipeline. It parses the natural language question in conjunction with the database schema to identify the relevant tables, columns, primary keys, foreign keys, and join relationships needed to answer the query.

This step constrains SQL generation to schema-relevant entities, reducing hallucinations and ensuring structural correctness in downstream stages.

## Role in Pipeline

```
Input:  Natural language question (Q) + Database identifier (db_id)
Output: Cropped schema (S) — relevant tables, columns, PKs, FKs, join relationships
Next:   → nl2sql-subproblem (Step 2)
```

## Process

### Step 1: Retrieve Schema Knowledge via llmwiki MCP

Use the llm-wiki-compiler MCP tools to discover relevant schema information:

1. **`search_pages`** — Search for tables/columns relevant to the question
   ```
   search_pages({ question: "Which tables contain customer order information and their payment status?" })
   ```

2. **`query_wiki`** — Ask grounded questions about schema relationships with citations
   ```
   query_wiki({ question: "What foreign key connects orders to customers?", save: true })
   ```

3. **Use `search_pages` results directly** — `search_pages` already returns the full page body
   for each match. No need for a separate `read_page` call. Access detailed column definitions
   via `result.pages[].body`.
   ```
   result = search_pages({ question: "..." })
   // result.pages[i].body contains the complete markdown with all column definitions
   ```

4. **If wiki not yet compiled**, use `ingest_source` + `compile_wiki` first (see orchestrator Phase 1)

### Step 2: Schema Linking Reasoning (Chain-of-Thought)

Perform structured reasoning to link the question to schema entities:

```
Given question: "Find the average salary of employees in departments with more than 10 people"

Schema Linking Reasoning:
1. The question mentions "employees" → likely maps to `employees` table
2. "salary" → likely maps to `salary` column in `employees` table
3. "departments" → likely maps to `departments` table
4. "more than 10 people" → implies counting employees per department → needs `dept_id` column
5. Join relationship: `employees.dept_id` → `departments.dept_id` (foreign key)

Cropped Schema Output:
- Tables: employees, departments
- Columns: employees.salary, employees.dept_id, departments.dept_name, departments.dept_id
- Primary Keys: departments.dept_id
- Foreign Keys: employees.dept_id → departments.dept_id
- Join: employees JOIN departments ON employees.dept_id = departments.dept_id
```

### Step 3: Format Output

The cropped schema output must include:
- **Table names** — Only tables relevant to the question
- **Column names** — Only columns needed (not all columns in the table)
- **Primary keys** — For relevant tables
- **Foreign keys** — For join relationships
- **Join paths** — How relevant tables connect

## Best Practices

- **Be conservative**: Include only directly relevant tables/columns. Extra entities distract downstream agents.
- **Trace join paths**: If the question involves multiple tables, explicitly identify the join path through foreign keys.
- **Handle ambiguity**: When a term could map to multiple columns (e.g., "name" → first_name vs last_name), list both as candidates and note the ambiguity.
- **Use llmwiki citations**: Reference specific wiki page slugs for each identified table/column for provenance.

## Common Pitfalls

| Pitfall | Correction |
|---------|-----------|
| Missing a required table for JOIN | Check llmwiki for indirect relationships; follow FK chains |
| Including all columns instead of relevant ones | Only include columns explicitly mentioned or structurally required |
| Ignoring composite foreign keys | Read full table entity pages from llmwiki for multi-column FKs |
| Mapping NL terms to wrong columns | Use `query_wiki` to verify semantic meaning of columns |

## Output Template

```json
{
  "tables": ["table1", "table2"],
  "columns": {
    "table1": ["col_a", "col_b", "col_id"],
    "table2": ["col_c", "col_d", "table1_id"]
  },
  "primary_keys": {
    "table1": "col_id",
    "table2": "col_d"
  },
  "foreign_keys": {
    "table2.table1_id": "table1.col_id"
  },
  "join_paths": [
    "table2 JOIN table1 ON table2.table1_id = table1.col_id"
  ],
  "reasoning": "Step-by-step schema linking logic..."
}
```