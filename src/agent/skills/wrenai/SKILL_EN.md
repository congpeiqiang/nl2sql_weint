---
name: wrenai
description: "TRIGGER when: the user wants to enhance the NL2SQL pipeline with the WrenAI semantic layer; wants to generate MDL semantic models from a database schema; wants to add business context to schema (enum meanings, units, metric definitions); wants to validate generated SQL via dry-plan; mentions WrenAI, semantic layer, MDL, governed SQL, or business context. This skill serves as a pre-processor (Phase 0) and validation layer (Phase 2.5) for the SQL-of-Thought pipeline. SKIP when: the user already has a complete semantic layer solution; the user only needs pure schema mapping without business context; the database is unsupported by WrenAI."
---

# WrenAI Semantic Layer Integration

## Overview

WrenAI is an open-source **GenBI engine** that provides a semantic SQL layer, enabling AI agents to understand business semantics beyond raw schemas. It uses **MDL (Model Definition Language)** to define business entities, relationships, metrics, and context, making NL2SQL-generated SQL more accurate and trustworthy.

## Role in the NL2SQL Pipeline

```
                    Phase 0: WrenAI Pre-processing (optional)
                    ┌─────────────────────────────┐
                    │ wren generate-mdl           │ ← Generate MDL from database
                    │ wren enrich-context         │ ← Add business context
                    │ wren context build/validate │ ← Build & validate MDL
                    └──────────────┬──────────────┘
                                   │
                                   ▼
                    Phase 2: Standard SQL-of-Thought Pipeline
                    Use MDL semantic context to enhance Schema Linking
                                   │
                                   ▼
                    Phase 2.5: WrenAI Validation (optional)
                    ┌─────────────────────────────┐
                    │ wren dry-plan --sql '...'   │ ← Dry-run SQL validation
                    │ wren memory recall          │ ← Retrieve similar queries
                    └─────────────────────────────┘
```

## WrenAI vs. Pure Schema Approach

| Dimension | Pure Schema (Current) | + WrenAI Semantic Layer |
|-----------|----------------------|------------------------|
| Table/column identification | Fuzzy NL matching | Explicit business entities in MDL |
| Business meaning | LLM inference only | Explicit enums, units in MDL |
| Metric definitions | Re-derived each run | Pre-defined metrics/cubes in MDL |
| SQL validation | Post-execution error check | `dry-plan` pre-execution validation |
| History reuse | None | `memory recall` for similar queries |

## Installation & Setup

```bash
pip install wrenai
```

## Workflow Guides

WrenAI's workflow guides are built into the CLI:

```bash
wren skills list                        # List all available workflows
wren skills get onboarding              # End-to-end setup
wren skills get generate-mdl            # Generate MDL from database schema
wren skills get dlt-connector           # Connect SaaS data sources
wren skills get enrich-context          # Add business context
wren skills get genbi                   # Build & deploy GenBI dashboards
```

### Phase 0: Pre-processing — Build the Semantic Layer

**Step 1: Configure Database Connection**

```bash
wren profile add --name <project_name> --type <db_type> \
  --host <host> --port <port> --database <db_name> \
  --user <user> --password <password>
wren profile list
```

**Step 2: Generate MDL from Schema**

```bash
wren generate-mdl --profile <project_name>
```

This analyzes the database schema and auto-generates an MDL project containing:
- `models/` — Semantic models for each table (names, column definitions, relationships)
- `project.yaml` — Project configuration

**Step 3: Enrich with Business Context**

```bash
wren enrich-context --profile <project_name>
```

Adds business semantics to MDL:
- Enum value meanings (e.g., `status = 'A'` means "Active")
- Column units (e.g., `amount` is in CNY)
- Pre-defined metric cubes (e.g., ARR, DAU, churn rate)

**Step 4: Build & Validate**

```bash
wren context build --profile <project_name>
wren context validate
```

### Phase 2: Enhanced Schema Linking

During the NL2SQL Schema Linking phase, in addition to using `search_pages` for raw schema knowledge, combine with WrenAI MDL semantic context:

1. Read MDL model files for **business meanings** of tables/columns
2. Use `wren context show` to view the complete semantic model
3. Include MDL business definitions as additional context during Schema Linking reasoning

### Phase 2.5: SQL Validation

After SQL generation and before database execution, use WrenAI for dry-run validation:

```bash
wren dry-plan --sql 'SELECT ...'
```

`dry-plan` performs SQL parsing and semantic validation **without accessing the database**:
- Validates SQL syntax correctness
- Checks that referenced tables/columns exist in MDL
- Verifies JOIN relationships are valid
- Catches semantic issues (e.g., incorrect enum value references)

### Semantic Memory

WrenAI includes semantic memory for retrieving similar historical queries:

```bash
wren memory recall --question "Find average salary by department"
# Returns: similar query examples, correct SQL, execution results
```

During NL2SQL error correction, use memory retrieval to avoid repeating mistakes.

## Integration with SQL-of-Thought Pipeline

### Full Flow

```
Input: NL Question + db_id
    │
    ▼
[Phase 0: WrenAI]
  wren generate-mdl            ← Skip if MDL exists
  wren enrich-context          ← Skip if context exists
    │
    ▼
[Phase 2: SQL-of-Thought]
  Step 1: Schema Linking       ← Enhanced with MDL semantic context
    search_pages + wren context show
  Step 2: Subproblem Decomp
  Step 3: Query Plan           ← Reference MDL metric definitions
  Step 4: SQL Generation
    │
    ▼
[Phase 2.5: WrenAI Validation]
  wren dry-plan --sql '...'    ← Validate SQL
    │                    │
  Pass                 Fail
    │                    │
    ▼                    ▼
[DB Execute]       [Fix SQL]
    │                    │
    ▼                    ▼
  Return Result     [Phase 3: Correction]
                    May use wren memory recall
```

### Recommended Strategy

| Scenario | WrenAI Usage |
|----------|-------------|
| First-time database use | Full Phase 0: generate-mdl → enrich-context → context build |
| Daily queries | Phase 2.5 only: dry-plan validation + memory recall |
| Complex metric queries | Phase 2: MDL metric definitions instead of manual derivation |
| SQL correction | Phase 3: memory recall for similar correct SQL |

## Common Pitfalls

| Pitfall | Correction |
|---------|-----------|
| MDL out of sync with schema | Run `wren generate-mdl` after every schema change |
| Ignoring business context | Periodically `wren enrich-context` even if schema unchanged |
| dry-plan passes but execution fails | dry-plan validates semantic layer only, not data existence |
| Over-relying on memory | memory recall provides reference, not a substitute for precise schema reasoning |
| MDL conflicts with llmwiki | MDL focuses on business semantics, llmwiki on schema documentation — complementary, not conflicting |

## Supported Databases

WrenAI supports 22+ data sources: PostgreSQL, MySQL, BigQuery, Snowflake, Spark SQL, DuckDB, SQLite, Trino, Databricks, MS SQL Server, Oracle, ClickHouse, and more.

```bash
wren docs connection-info postgres    # View connection params for a data source
wren docs connection-info sqlite
```
