# SQL-of-Thought Pipeline Flow

## Complete Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        USER INPUT                                    │
│              Natural Language Question (Q)                            │
└─────────────────────┬───────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    PHASE 1: PRE-PIPELINE                            │
│              Schema Knowledge Preparation                            │
│                                                                      │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────┐             │
│  │ ingest_     │──→│ compile_     │──→│ wren context show  │             │
│  │ (verify)    │
│  └─────────────┘   └──────────────┘   └──────────────┘             │
│                                                                      │
│  Uses: wren context show-compiler MCP server                                  │
│  Purpose: Build MDL from database schema                             │
└─────────────────────┬───────────────────────────────────────────────┘
                      │ (if MDL already built, skip)
                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  PHASE 2: SEQUENTIAL PIPELINE                       │
│                                                                      │
│  Step 1: Schema Linking Agent                                        │
│  ┌─────────────────────────────────────────────────┐                │
│  │ Input: Q + db_id                                │                │
│  │ Process: wren context show → wren context show               │                │
│  │         → CoT schema linking reasoning           │                │
│  │ Output: Cropped Schema (S)                       │                │
│  └────────────────────┬────────────────────────────┘                │
│                       │                                              │
│                       ▼                                              │
│  Step 2: Subproblem Agent                                            │
│  ┌─────────────────────────────────────────────────┐                │
│  │ Input: Q + S                                    │                │
│  │ Process: Intent analysis → clause mapping       │                │
│  │ Output: JSON Subproblems (C)                     │                │
│  │   {WHERE, GROUP BY, JOIN, HAVING, ...}          │                │
│  └────────────────────┬────────────────────────────┘                │
│                       │                                              │
│                       ▼                                              │
│  Step 3: Query Plan Agent (CRITICAL: NO SQL)                        │
│  ┌─────────────────────────────────────────────────┐                │
│  │ Input: Q + S + C                                │                │
│  │ Process: CoT reasoning → procedural steps       │                │
│  │ Output: Procedural Plan (P) — NO SQL HERE       │                │
│  └────────────────────┬────────────────────────────┘                │
│                       │                                              │
│                       ▼                                              │
│  Step 4: SQL Generation Agent                                        │
│  ┌─────────────────────────────────────────────────┐                │
│  │ Input: Q + P + S                                │                │
│  │ Process: Plan→SQL mapping → post-processing     │                │
│  │ Output: Executable SQL (Y)                       │                │
│  └────────────────────┬────────────────────────────┘                │
│                       │                                              │
│                       ▼                                              │
│  Step 5: DB Execution                                                │
│  ┌─────────────────────────────────────────────────┐                │
│  │ Process: Execute SQL against SQLite database     │                │
│  │ Decision: SUCCESS → END                          │                │
│  │           ERROR → Phase 3                        │                │
│  └────────────────────┬────────────────────────────┘                │
│                       │                                              │
│            ┌──────────┴──────────┐                                   │
│            │                     │                                   │
│         SUCCESS               ERROR                                 │
│            │                     │                                   │
│            ▼                     ▼                                   │
│    ┌─────────────┐    ┌─────────────────────────────────┐           │
│    │   RETURN    │    │    PHASE 3: CORRECTION LOOP      │           │
│    │   RESULT    │    │                                   │           │
│    └─────────────┘    │  Step 6: Correction Plan Agent   │           │
│                       │  ┌───────────────────────────┐   │           │
│                       │  │ Input: Failed SQL + Error │   │           │
│                       │  │         + Q + S + Taxonomy│   │           │
│                       │  │ Process: Taxonomy-guided  │   │           │
│                       │  │         CoT diagnosis     │   │           │
│                       │  │ Output: Correction Plan   │   │           │
│                       │  └──────────────┬────────────┘   │           │
│                       │                 │                 │           │
│                       │                 ▼                 │           │
│                       │  Step 7: Correction SQL Agent    │           │
│                       │  ┌───────────────────────────┐   │           │
│                       │  │ Input: Correction Plan    │   │           │
│                       │  │         + Q + S + Failed  │   │           │
│                       │  │         SQL               │   │           │
│                       │  │ Process: Regenerate SQL   │   │           │
│                       │  │ Output: Corrected SQL     │   │           │
│                       │  └──────────────┬────────────┘   │           │
│                       │                 │                 │           │
│                       │                 ▼                 │           │
│                       │  Re-Execute → Check              │           │
│                       │  ┌───────────────────────────┐   │           │
│                       │  │ SUCCESS → RETURN RESULT   │   │           │
│                       │  │ ERROR → Repeat (max 3x)   │   │           │
│                       │  │ 3x FAIL → TERMINATE       │   │           │
│                       │  └───────────────────────────┘   │           │
│                       └─────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────────────┘
```

## Decision Logic

### When to Enter Correction Loop?

| Condition | Action |
|-----------|--------|
| SQL executes successfully and returns expected results | → END (return result) |
| SQL throws execution error (syntax, runtime) | → Enter correction loop |
| SQL executes but returns logically incorrect results | → Enter correction loop |
| Same error code repeated across correction attempts | → TERMINATE (stuck) |
| 3 correction attempts exhausted | → TERMINATE (max attempts reached) |

### When to Skip Steps?

| Condition | Action |
|-----------|--------|
| target/mdl.json exists and schema unchanged | → Skip Phase 1 |
| Question is trivial (single table, no joins/aggregation) | → Steps 2-3 can be simplified but NOT skipped |
| SQL succeeds on first attempt | → Skip Phase 3 entirely |

## Agent Interactions and Data Flow

```
Q ──→ Schema Linking ──→ S ──→ Subproblem ──→ C ──→ Query Plan ──→ P ──→ SQL Gen ──→ Y
                                                                    │
                                                                    ▼
                                                            DB Execute
                                                            │        │
                                                         Success   Error
                                                            │        │
                                                            ▼        ▼
                                                        Return   Correction
                                                        Result   Loop:
                                                                 │
                                                                 ▼
                                                         Y_fail + Error + T
                                                                 │
                                                                 ▼
                                                         Correction Plan ──→ Plan_fix
                                                                 │
                                                                 ▼
                                                         Correction SQL ──→ Y_corrected
                                                                 │
                                                                 ▼
                                                         DB Execute (re-check)
```

## Key Formalization

The entire pipeline is formalized as:

**Y = LLM(Q, S, C, P, T | θ)**

| Variable | Meaning | Source |
|----------|---------|--------|
| Q | Natural language question | User input |
| S | Linked schema (cropped) | Schema Linking Agent |
| C | Clause-level subproblems (JSON) | Subproblem Agent |
| P | Procedural query plan (CoT) | Query Plan Agent |
| T | Error taxonomy (31 sub-categories) | Correction Plan Agent |
| θ | LLM parameters | Model selection |
| Y | Final executable SQL output | SQL Agent / Correction SQL Agent |