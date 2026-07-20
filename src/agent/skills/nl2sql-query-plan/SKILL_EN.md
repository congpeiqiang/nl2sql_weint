---
name: nl2sql-query-plan
description: "TRIGGER when: the NL2SQL pipeline needs to generate a procedural step-by-step query execution plan from a natural language question, schema, and subproblems; user wants a Chain-of-Thought reasoning plan before SQL generation. This skill is Step 3 in the SQL-of-Thought pipeline, used AFTER subproblem decomposition and BEFORE SQL generation. CRITICAL: This agent MUST NOT generate SQL — only procedural plans. SKIP when: user requests direct SQL without planning; user already has a query plan ready."
---

# NL2SQL Query Plan Agent

## Overview

The Query Plan Agent is **Step 3** in the SQL-of-Thought pipeline. It generates a **step-by-step procedural execution plan** that maps the user's intent to the schema and subproblems, using **Chain-of-Thought (CoT) reasoning**.

This is a critical intermediate reasoning step that reduces hallucinations and improves alignment between natural language intent and the final SQL output. The agent is **explicitly restricted from generating executable SQL** at this stage.

## Role in Pipeline

```
Input:  Natural language question (Q) + Cropped schema (S) + Clause-level subproblems (C)
Output: Procedural query plan (P) — step-by-step reasoning, NO SQL
Next:   → nl2sql-sql-generation (Step 4)
```

## Critical Constraint

> ⚠️ **THE QUERY PLAN AGENT MUST NOT GENERATE SQL**
> 
> This agent produces **only a procedural plan** — a step-by-step description of how to solve the query. SQL generation is the exclusive responsibility of the SQL Agent (Step 4).
> 
> Violating this constraint causes:
> - Increased hallucination rates
> - ~5% accuracy drop (per ablation study)
> - Misalignment between NL intent and SQL output

## Process

### Step 1: Review All Inputs

Gather and review:
- **Question (Q)**: The original natural language question
- **Schema (S)**: The cropped schema with relevant tables, columns, PKs, FKs
- **Subproblems (C)**: The JSON key–value pairs from the Subproblem Agent

### Step 2: Chain-of-Thought Reasoning

For each step, explicitly explain the reasoning behind the decision. Use the following CoT structure:

```
Question: "Find the average salary of employees in departments with more than 10 people"
Schema: employees(id, name, salary, dept_id), departments(id, dept_name)
Subproblems: { SELECT: "average salary per dept", JOIN: "employees↔departments via dept_id", GROUP BY: "by department", HAVING: "count > 10" }

Reasoning Step 1: The question asks about employees and departments together. 
→ I need to JOIN the employees and departments tables.
→ The foreign key employees.dept_id references departments.id, so the join condition is: employees.dept_id = departments.id

Reasoning Step 2: The question mentions "average salary" per department.
→ This requires grouping by department, so I group by departments.dept_name.
→ The aggregation function is AVG applied to employees.salary.

Reasoning Step 3: The question specifies "more than 10 people" per department.
→ This is a post-aggregation condition, so it belongs in the HAVING clause.
→ I need to count employees per department (COUNT(*)) and filter for > 10.

Reasoning Step 4: Putting it all together:
→ 1. Start from employees table
→ 2. Join with departments on dept_id
→ 3. Group by dept_name
→ 4. Calculate AVG(salary) and COUNT(*) for each group
→ 5. Filter groups where COUNT(*) > 10
→ 6. Return dept_name and average salary for qualifying departments
```

### Step 3: Synthesize Procedural Plan

Convert the reasoning into a numbered, procedural plan:

```
Query Plan:
1. Join the employees table with the departments table using the foreign key relationship: employees.dept_id = departments.id
2. Group the joined results by department name (departments.dept_name)
3. For each group, compute two aggregates:
   a. Average salary: AVG(employees.salary)
   b. Employee count: COUNT(*)
4. Apply a post-aggregation filter: only keep groups where the employee count (COUNT(*)) exceeds 10
5. Return two columns for qualifying departments: department name and average salary
6. No explicit sorting or limit is specified in the question
```

### Step 4: Verify Plan Against Schema

Final check:
- Does the plan reference only tables/columns that exist in the cropped schema?
- Does every subproblem (C) have a corresponding step in the plan?
- Are join conditions using valid foreign keys?
- Are aggregation functions paired with appropriate GROUP BY?

## Output Template

```json
{
  "question": "original natural language question",
  "schema_used": {
    "tables": ["..."],
    "key_columns": ["..."],
    "join_conditions": ["..."]
  },
  "subproblems_addressed": {
    "SELECT": "step X covers this",
    "JOIN": "step Y covers this",
    "GROUP BY": "step Z covers this",
    "HAVING": "step W covers this"
  },
  "cot_reasoning": [
    "Step-by-step reasoning for each decision..."
  ],
  "procedural_plan": [
    "1. Procedural step one...",
    "2. Procedural step two...",
    "3. Procedural step three...",
    "..."
  ],
  "plan_verification": {
    "all_subproblems_covered": true,
    "schema_references_valid": true,
    "joins_use_valid_fks": true
  }
}
```

## Examples

### Simple Selection Plan
```
Question: "What are the names of all employees?"
Plan:
1. Read all rows from the employees table
2. Extract only the name column (employees.name)
3. Return all employee names without filtering or grouping
```

### Complex Aggregation Plan
```
Question: "Find the top 3 departments by average salary among departments with at least 5 employees"

Plan:
1. Join employees with departments via employees.dept_id = departments.id
2. Group the results by departments.dept_name
3. For each group, compute AVG(employees.salary) and COUNT(*)
4. Filter groups where COUNT(*) >= 5 (HAVING condition)
5. Order qualifying groups by average salary in descending order
6. Limit results to top 3 departments
7. Return: dept_name, avg_salary
```

### Nested Subquery Plan
```
Question: "Find employees whose salary is higher than the average salary of their department"

Plan:
1. First, compute the average salary per department as a subquery:
   a. Group employees by dept_id
   b. Calculate AVG(salary) for each department
2. Then, for each employee, compare their salary against the average of their department from step 1
3. Select employees where salary > department average (from subquery)
4. Correlate the subquery with the outer query using dept_id
5. Return: employee name, salary, department name, department average
```

## Best Practices

- **Always use CoT reasoning**: Explain WHY each decision is made, not just WHAT
- **Number every step**: Procedural plans must be numbered and sequential
- **Cross-reference subproblems**: Every key in (C) should map to at least one plan step
- **Be explicit about join conditions**: Always specify the exact FK column pairs used for joins
- **Handle subqueries procedurally**: Describe nested logic as numbered steps within steps
- **Keep plan concise**: Avoid unnecessary verbosity — focus on execution logic

## Common Pitfalls

| Pitfall | Correction |
|---------|-----------|
| Generating SQL in the plan | This is the #1 violation — write procedural steps only |
| Skipping CoT reasoning | Always explain reasoning before synthesizing steps |
| Missing subproblem coverage | Every clause in (C) must appear in the plan |
| Vague join descriptions | Specify exact column pairs (e.g., `e.dept_id = d.id`) |
| Ignoring implicit requirements | "average per X" implies GROUP BY even if not explicit |