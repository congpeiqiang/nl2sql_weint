---
name: nl2sql-subproblem
description: "TRIGGER when: the NL2SQL pipeline needs to decompose a natural language question into clause-level subproblems; user wants to break down a database query into structured components like WHERE, GROUP BY, JOIN, HAVING, etc. This skill is Step 2 in the SQL-of-Thought pipeline, used AFTER schema linking and BEFORE query plan generation. SKIP when: user provides pre-decomposed subproblems; user asks for direct SQL without decomposition."
---

# NL2SQL Subproblem Agent

## Overview

The Subproblem Agent is **Step 2** in the SQL-of-Thought pipeline. Given the natural language question and the schema-linked output, it decomposes the query into **clause-level subproblems** expressed as structured JSON key–value pairs.

This decomposition provides a modular representation of the query intent, enabling downstream agents (Query Plan, SQL) to reason over smaller, well-defined units rather than the entire query at once.

## Role in Pipeline

```
Input:  Natural language question (Q) + Cropped schema (S)
Output: Structured JSON object with clause-level subproblems (C)
Next:   → nl2sql-query-plan (Step 3)
```

## Supported Clause Types

Each identified clause is expressed as a key–value pair where the **key** is the SQL clause type and the **value** is a natural-language description of what that clause should accomplish:

| Clause Key | Description | Example Value |
|-----------|-------------|--------------|
| `WHERE` | Row filtering conditions | "Employees with salary > 50000" |
| `GROUP BY` | Grouping/aggregation dimension | "Group by department name" |
| `HAVING` | Post-aggregation filter | "Groups with more than 10 people" |
| `JOIN` | Table relationship requirement | "Join employees with departments via dept_id" |
| `DISTINCT` | Uniqueness requirement | "Unique department names" |
| `ORDER BY` | Sorting specification | "Order by average salary descending" |
| `LIMIT` | Result count restriction | "Top 5 highest salaries" |
| `EXCEPT` | Set difference operation | "Departments except those with no employees" |
| `UNION` | Set union operation | "Combine US and EU employee counts" |

> **Important**: Not every clause will appear in every query. Only include clauses that are actually needed based on the question analysis.

## Process

### Step 1: Analyze the Question Intent

Read the natural language question and identify which SQL clauses are implied:

```
Question: "Find the average salary of employees in departments with more than 10 people"

Intent Analysis:
- "Find the average salary" → SELECT with aggregation (AVG), needs GROUP BY
- "of employees in departments" → JOIN needed between employees and departments
- "with more than 10 people" → HAVING clause (post-aggregation filter)
- No explicit ORDER BY, LIMIT, DISTINCT, EXCEPT, or UNION mentioned
```

### Step 2: Map Intent to Clause-Level Subproblems

For each identified clause, write a **natural-language description** of what that clause should achieve. Do NOT write SQL syntax in the values — use descriptive language:

```
Subproblems:
{
  "SELECT": "Average salary per qualifying department",
  "JOIN": "Connect employees table to departments table through dept_id",
  "GROUP BY": "Group results by department",
  "HAVING": "Only include departments with employee count greater than 10"
}
```

### Step 3: Verify Against Schema

Cross-check each subproblem against the cropped schema to ensure feasibility:
- Can the mentioned columns be found in the linked tables?
- Are the join paths consistent with the identified FKs?
- Is the aggregation type supported by the available columns?

### Step 4: Format Output

## Output Template

```json
{
  "SELECT": "description of what to retrieve",
  "WHERE": "description of row-level filter conditions (if needed)",
  "JOIN": "description of which tables to connect and how (if needed)",
  "GROUP BY": "description of grouping dimension (if needed)",
  "HAVING": "description of post-aggregation filter (if needed)",
  "DISTINCT": "description of uniqueness requirement (if needed)",
  "ORDER BY": "description of sorting requirement (if needed)",
  "LIMIT": "description of result count restriction (if needed)",
  "EXCEPT": "description of set difference operation (if needed)",
  "UNION": "description of set union operation (if needed)",
  "reasoning": "Step-by-step decomposition logic..."
}
```

## Examples

### Simple Query
```
Question: "What are the names of all employees?"
→ {
  "SELECT": "Employee names from the employees table"
}
```

### Complex Aggregation Query
```
Question: "Find the department with the highest average salary among departments that have at least 5 employees, sorted by average salary descending, show only the top 3"

→ {
  "SELECT": "Department name and average salary",
  "JOIN": "Connect employees to departments via dept_id",
  "GROUP BY": "Group by department",
  "HAVING": "Departments with at least 5 employees",
  "ORDER BY": "Sort by average salary in descending order",
  "LIMIT": "Top 3 results"
}
```

### Set Operation Query
```
Question: "Show all products that are in Electronics category but not in discontinued products list"

→ {
  "SELECT": "Product names and details",
  "WHERE": "Products in Electronics category",
  "EXCEPT": "Remove products that appear in the discontinued products list"
}
```

## Best Practices

- **Use natural language in values, NOT SQL syntax**: The values should describe intent, not implementation
- **Only include relevant clauses**: If a clause is not implied by the question, omit it entirely
- **Be specific**: "departments with more than 10 people" is better than "some filter on departments"
- **Cross-reference schema**: Ensure every column/table mentioned in subproblems exists in the linked schema
- **Think about implicit clauses**: Some queries imply GROUP BY even when not explicitly stated ("average per department" → GROUP BY)

## Common Pitfalls

| Pitfall | Correction |
|---------|-----------|
| Writing SQL syntax in subproblem values | Use natural language descriptions only |
| Missing implicit GROUP BY | "Average per X" or "count per Y" always implies GROUP BY |
| Confusing WHERE vs HAVING | WHERE = row-level filter; HAVING = group-level filter |
| Omitting JOIN when multiple tables are referenced | Any reference to columns across tables requires JOIN |
| Adding unnecessary clauses | Only include clauses explicitly or implicitly required |