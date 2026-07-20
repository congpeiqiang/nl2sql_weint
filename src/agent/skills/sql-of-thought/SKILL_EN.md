---
name: sql-of-thought
description: "TRIGGER when: user asks a question that requires querying a database using SQL; user wants to convert natural language to SQL; user mentions NL2SQL, Text-to-SQL, or database query from natural language; user needs to analyze, query, or extract data from a database using a natural language question. SKIP when: user asks about writing SQL directly without NL input; user wants database administration tasks (backup, migration, etc.); user asks about NoSQL or non-SQL database operations."
---

# SQL-of-Thought: Multi-Agent NL2SQL Orchestrator

## Overview

This skill orchestrates the **SQL-of-Thought** multi-agent pipeline for converting natural language questions into executable SQL queries. The framework decomposes the NL2SQL task into specialized agent stages, following a sequential pipeline with a conditional taxonomy-guided correction loop.

Based on the research paper *"SQL-of-Thought: Multi-agentic Text-to-SQL with Guided Error Correction"* (Chaturvedi, Chadha, Bindschaedler, 2025), achieving **91.59% Execution Accuracy** on Spider benchmark.

## When to Use

- User asks a natural language question that requires database querying
- User wants to translate business questions into SQL
- User needs data extraction/analysis from a database using plain language
- Any scenario where NL → SQL conversion is needed

## Architecture: Y = LLM(Q, S, C, P, T | θ)

Where:
- **Q** = Natural language question
- **S** = Linked schema (relevant tables/columns)
- **C** = Clause-specific subproblems (JSON key-value pairs)
- **P** = Query plan (procedural, step-by-step)
- **T** = Error taxonomy (for CoT-informed error correction)
- **θ** = LLM parameters

## Pipeline Workflow

### Phase 1: Schema Knowledge Preparation (Pre-Pipeline)

Before the main pipeline, ensure database schema knowledge is available via the **llm-wiki-compiler** MCP server:

1. **Ingest schema documentation** — Use `ingest_source` to load database schema docs, ER diagrams, data dictionaries
2. **Compile wiki** — Use `compile_wiki` to extract structured entity/concept pages for tables and relationships
3. **Verify status** — Use `wiki_status` to confirm all schema entities are compiled and no orphans exist

> If schema wiki is already compiled, skip this phase.

### Phase 2: Sequential Pipeline (Main Flow)

Execute the following steps in strict sequence, loading each agent skill as needed:

```
Step 1 → load_skill("nl2sql-schema-linking")
         Schema Linking Agent: NL question → cropped schema
         Uses llmwiki MCP: search_pages, query_wiki for table/column discovery

Step 2 → load_skill("nl2sql-subproblem")
         Subproblem Agent: question + schema → JSON subproblems
         (WHERE, GROUP BY, JOIN, DISTINCT, ORDER BY, HAVING, EXCEPT, LIMIT, UNION)

Step 3 → load_skill("nl2sql-query-plan")
         Query Plan Agent: question + schema + subproblems → procedural plan (NO SQL)
         Chain-of-Thought reasoning required

Step 4 → load_skill("nl2sql-sql-generation")
         SQL Agent: question + query plan → executable SQL
         Post-process: remove trailing semicolons, NL fragments

Step 5 → Execute SQL against database
         If SUCCESS → return result, pipeline ends
         If ERROR → enter Phase 3
```

### Phase 3: Guided Correction Loop (Conditional)

Only invoked when SQL execution fails. Loop until success or max attempts reached:

```
Step 6 → load_skill("nl2sql-correction")
         Correction Plan Agent: failed SQL + error + taxonomy → CoT correction plan
         Correction SQL Agent: correction plan → regenerated SQL

Step 7 → Re-execute corrected SQL
         If SUCCESS → return result
         If ERROR → repeat Step 6 (max 3 attempts)
```

## Critical Design Principles

1. **Staged reasoning is mandatory**: Always generate Query Plan before SQL — never skip
2. **Query Plan Agent MUST NOT generate SQL**: It produces only a procedural plan
3. **Temperature = 0**: All LLM calls use temperature 0 for deterministic, reliable outputs
4. **No shared history across correction attempts**: Each correction starts fresh — no scratchpad
5. **Single correction pipeline**: Never use multiple per-error-type agents (causes conflicting edits)
6. **Concise error codes over verbose descriptions**: Use taxonomy codes (e.g., `join_missing`) not lengthy text
7. **Guided correction > unguided**: Taxonomy + CoT > raw execution feedback alone

## Failed Ablations (AVOID These Patterns)

- ❌ Free-form taxonomy → direct to SQL agent (LLMs struggle with unguided debugging)
- ❌ Temperature > 0 (reduces plan faithfulness, more invalid joins)
- ❌ Clause-specific prompt rules for JOIN/LIMIT (inflates context, distracts model)
- ❌ Multiple repair agents per error type → aggregation (conflicting edits, incoherent SQL)
- ❌ Shared scratchpad carrying history (schema drift, repetition, increased cost)

## Hybrid Model Strategy (Cost Optimization)

| Agent Type | Recommended Model Class | Reason |
|------------|------------------------|--------|
| Schema Linking | Reasoning model (e.g., Claude Opus, GPT-5) | Requires deep schema comprehension |
| Query Plan | Reasoning model | Requires CoT reasoning for plan generation |
| Correction Plan | Reasoning model | Requires taxonomy-guided diagnostic reasoning |
| Subproblem | Non-reasoning model (e.g., GPT-4o) | Structured JSON decomposition, less reasoning |
| SQL Generation | Non-reasoning model | Plan-to-SQL synthesis, follows explicit instructions |
| Correction SQL | Non-reasoning model | Correction plan is already structured guidance |

> This hybrid approach reduces cost ~30% while maintaining ~85% EA.

## MCP Integration: llm-wiki-compiler

The llmwiki MCP server provides schema knowledge retrieval:

| MCP Tool | Pipeline Phase | Usage |
|----------|---------------|-------|
| `ingest_source` | Phase 1 | Load schema docs, ER diagrams, data dictionaries |
| `compile_wiki` | Phase 1 | Build structured wiki from ingested schema sources |
| `search_pages` | Phase 2 (Step 1) | Find relevant tables/columns for the NL question |
| `query_wiki` | Phase 2 (Step 1-2) | Ask grounded questions about schema relationships |
| `read_page` | Phase 2 (Step 1) | Read specific table entity pages with full column definitions |
| `wiki_status` | Phase 1 | Check compilation status and completeness |
| `lint_wiki` | Phase 1 | Validate schema documentation quality |

## Output Format

Final output should include:
- **Generated SQL query** (post-processed, no trailing semicolons)
- **Execution result** (if database is available)
- **Pipeline trace** (which steps were executed, any corrections applied)
- **Error diagnosis** (if corrections were needed, which taxonomy categories were identified)

## Quick Start Example

```
User: "Find the average salary of employees in departments with more than 10 people"

→ Phase 1: Ensure schema wiki is compiled for the HR database
→ Step 1: Schema Linking → identifies `employees` and `departments` tables, `salary` column, `dept_id` FK
→ Step 2: Subproblem → {"GROUP BY": "department", "HAVING": "COUNT(*) > 10", "SELECT": "AVG(salary)"}
→ Step 3: Query Plan → "1. Join employees with departments on dept_id. 2. Group by department. 3. Filter groups with count > 10. 4. Calculate average salary per qualifying group."
→ Step 4: SQL → SELECT d.dept_name, AVG(e.salary) FROM employees e JOIN departments d ON e.dept_id = d.dept_id GROUP BY d.dept_name HAVING COUNT(*) > 10
→ Step 5: Execute → SUCCESS ✓
```

## References

- [Error Taxonomy](references/error-taxonomy.md) — Full 9-category, 31-subcategory taxonomy
- [Pipeline Flow](references/pipeline-flow.md) — Detailed flow diagram with decision logic
- [Design Principles](references/design-principles.md) — Key principles and failed ablation lessons
- [Hybrid Model Strategy](references/hybrid-model-strategy.md) — Cost-performance optimization guide