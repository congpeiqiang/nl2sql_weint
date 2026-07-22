# SQL-of-Thought Design Principles

## Core Principles

### 1. Staged Reasoning is Mandatory

**Principle**: Always generate a Query Plan before SQL — never skip this step.

**Evidence**: Ablation study shows ~5% accuracy drop when bypassing the query plan step (Claude Opus 3: 95% → 90%).

**Reasoning**: LLMs are better at formulating reasoning for mathematical, programming, and planning tasks. The intermediate reasoning step:
- Explicitly organizes relevant schema elements
- Reduces hallucinations
- Improves alignment between NL intent and SQL output

**Implementation**: Query Plan Agent produces ONLY procedural steps, NEVER SQL.

### 2. Query Plan Agent Must NOT Generate SQL

**Principle**: The plan stage is strictly procedural — no executable SQL code.

**Reasoning**: Direct SQL generation during planning conflates reasoning with implementation, leading to:
- Premature commitment to specific SQL constructs
- Reduced flexibility for the SQL Agent to optimize implementation
- Higher hallucination rates

**Implementation**: Explicit prompt restriction: "Generate a step-by-step procedural plan. DO NOT generate any SQL code."

### 3. Guided Correction > Unguided Correction

**Principle**: Taxonomy + CoT reasoning > raw execution feedback alone.

**Evidence**: 95–99% of generated queries are already syntactically valid. Predominant failures are **intent mismatches** — logically incorrect but syntactically valid queries. Raw execution traces provide little guidance for correcting these.

**Reasoning**: Structured taxonomy enables:
- Categorization of *why* a query failed (not just *what* failed)
- Interpretable, linguistically grounded diagnosis
- Specific, actionable repair strategies

**Implementation**: Correction Plan Agent uses concise taxonomy codes + CoT reasoning template.

### 4. Concise Error Codes Over Verbose Descriptions

**Principle**: Use taxonomy sub-category codes (e.g., `join_missing`, `agg_no_groupby`) instead of lengthy error descriptions.

**Reasoning**: Verbose descriptions:
- Overflow the LLM context window
- Add latency and cost
- Reduce focus on the actual repair strategy

**Implementation**: 9 categories, 31 sub-categories, all with short code names.

### 5. Temperature = 0

**Principle**: All LLM calls use temperature 0 for deterministic, reliable outputs.

**Evidence**: Increasing temperature above 0 for GPT-4o added surface diversity but reduced plan faithfulness — more invalid joins and clause misuse.

**Implementation**: Set `temperature=0` for all agent interactions. Use `top_p` and other hyperparameters at defaults.

### 6. No Shared History Across Correction Attempts

**Principle**: Each correction attempt starts fresh — no scratchpad or history from previous attempts.

**Evidence**: Carrying history through a shared scratchpad:
- Expanded context window
- Increased latency and API cost
- Amplified repetition and schema drift
- Led to lower accuracy

**Implementation**: Correction loop resets context each attempt. Only the current failed SQL + error + taxonomy are provided.

### 7. Single Correction Pipeline (Not Multiple Per-Error-Type Agents)

**Principle**: Use one Correction Plan Agent → one Correction SQL Agent per attempt.

**Evidence**: An ablation design with multiple repair agents (one per error type) → aggregation agent failed because:
- Independent edits conflicted
- Merge process produced incoherent SQL
- Coordination overhead increased cost without benefit

**Implementation**: Single unified correction pipeline that handles all error types in one pass.

### 8. No Clause-Specific Hard-Coded Rules

**Principle**: Do NOT add specific prompt rules for individual clauses (JOIN, LIMIT, etc.).

**Evidence**: Adding clause-specific rules:
- Inflated the context window
- Distracted the model with irrelevant details
- Lowered accuracy overall

**Implementation**: Keep prompts generic and focused on the plan → SQL mapping logic.

### 9. Structured Reasoning Step Before SQL Regeneration

**Principle**: Correction Plan Agent must intermediate between error detection and SQL fix.

**Evidence**: A critic loop that applied the full error taxonomy in a free-form way and sent errors directly to the SQL agent underperformed. LLMs struggle with unguided debugging and benefit from at least one structured reasoning step.

**Implementation**: Error detection → Correction Plan (CoT reasoning) → Correction SQL (guided regeneration).

## Failed Ablation Designs (DO NOT REPLICATE)

| ❌ Failed Approach | Evidence | Reason |
|-------------------|----------|--------|
| Free-form taxonomy → direct to SQL agent | Underperformed vs. structured correction | LLMs struggle with unguided debugging |
| Temperature > 0 | Reduced plan faithfulness | More invalid joins and clause misuse |
| Clause-specific prompt rules (JOIN, LIMIT) | Lowered accuracy | Inflated context, distracted model |
| Multiple repair agents per error type → aggregation agent | Failed completely | Independent edits conflicted, incoherent merge |
| Shared scratchpad across correction attempts | Lowered accuracy | Repetition, schema drift, increased cost/latency |

## Hybrid Model Strategy

### Principle: Reasoning Models for Reasoning Tasks, Cheaper Models for Generation Tasks

| Agent | Reasoning Required? | Recommended Model Class |
|-------|:---:|--------|
| Schema Linking | ✅ Yes | Reasoning model (Claude Opus, GPT-5) |
| Query Plan | ✅ Yes | Reasoning model |
| Correction Plan | ✅ Yes | Reasoning model |
| Subproblem | ❌ No | Non-reasoning model (GPT-4o) |
| SQL Generation | ❌ No | Non-reasoning model |
| Correction SQL | ❌ No | Non-reasoning model |

### Cost-Performance Trade-offs

| Configuration | Cost (100-sample) | EA (100-sample) | Notes |
|--------------|-------------------|:---:|-------|
| Full Claude Opus 3 | ~$42.58 (full Spider: ~$42) | 95% | Best accuracy |
| Full GPT models | ~$44.20 (full Spider) | 89% | Good accuracy |
| Hybrid (Opus for reasoning + GPT-4o for gen) | ~$30 (full Spider) | 85% | Good cost-performance balance |
| GPT-4o-mini only | Lower | 87% | Budget option |
| GPT-3.5 only | Lowest | 67% | Not recommended |
| Open-source (Llama 3.1 8B) | ~$0 (self-hosted) | ~45% | Not suitable for NL2SQL |

### Why Hybrid Works

- Schema Linking, Query Planning, and Correction Planning require deep reasoning — reasoning models excel here
- Subproblem decomposition, SQL synthesis, and Correction SQL follow structured guidance — non-reasoning models perform adequately
- Cost savings: ~30% reduction while maintaining 85% EA