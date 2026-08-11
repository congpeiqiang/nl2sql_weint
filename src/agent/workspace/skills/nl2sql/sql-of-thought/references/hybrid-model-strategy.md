# Hybrid Model Strategy Guide

## Overview

The SQL-of-Thought framework supports **hybrid model deployment** — using reasoning models (e.g., Claude Opus 3) for reasoning-intensive agents and cheaper non-reasoning models (e.g., GPT-4o) for generation-oriented agents.

This strategy reduces total cost by approximately **30%** while maintaining **85% Execution Accuracy** on a 100-sample ablation (vs. 95% for full Opus 3).

## Agent-Model Mapping

### Reasoning-Intensive Agents (Use Reasoning Models)

| Agent | Why Reasoning Model? | Suitable Models |
|-------|---------------------|-----------------|
| **Schema Linking** | Requires deep schema comprehension, FK inference, semantic column mapping | Claude Opus 3, GPT-5, Claude Sonnet 4 |
| **Query Plan** | Requires CoT reasoning for procedural plan generation, logical decomposition | Claude Opus 3, GPT-5 |
| **Correction Plan** | Requires taxonomy-guided diagnostic reasoning, root cause analysis | Claude Opus 3, GPT-5 |

### Generation-Oriented Agents (Use Non-Reasoning Models)

| Agent | Why Non-Reasoning is OK? | Suitable Models |
|-------|--------------------------|-----------------|
| **Subproblem** | Structured JSON decomposition following clear patterns; less creative reasoning | GPT-4o, Claude Haiku |
| **SQL Generation** | Plan-to-SQL mapping; follows explicit instructions from the procedural plan | GPT-4o, Claude Haiku |
| **Correction SQL** | Follows the structured correction plan; guided regeneration, not open-ended reasoning | GPT-4o, Claude Haiku |

## Cost Analysis

### Full Spider Dataset Run (~1034 samples)

| Configuration | Total Cost | Est. Runtime | EA (100-sample) |
|--------------|-----------|-------------|:---:|
| Claude Opus 3 only | $42.58 | ~5 hours | 95% |
| GPT-5 only | ~$44.20 | ~5 hours | 89% |
| **Hybrid: Opus (reasoning) + GPT-4o (gen)** | **~$30** | ~5 hours | 85% |
| GPT-4o-mini only | Lower | ~4 hours | 87% |
| GPT-3.5 only | Lowest | ~3 hours | 67% |

### Per-Sample Cost Breakdown (Hybrid)

```
Reasoning model calls (3 agents × ~0.03/MTok input + ~0.15/MTok output):
  - Schema Linking: ~$0.04
  - Query Plan: ~$0.06
  - Correction Plan (if needed): ~$0.05

Non-reasoning model calls (3 agents × ~0.005/MTok input + ~0.015/MTok output):
  - Subproblem: ~$0.008
  - SQL Generation: ~$0.01
  - Correction SQL (if needed): ~$0.01

Per-sample (success on first try): ~$0.11
Per-sample (with 1 correction): ~$0.17
```

## Model Selection Guidelines

### For Maximum Accuracy
```
All agents → Claude Opus 3 ($15/MTok)
Expected EA: ~95% (100-sample) → ~91.59% (full Spider)
```

### For Best Cost-Performance Balance
```
Schema Linking → Claude Opus 3
Query Plan → Claude Opus 3
Subproblem → GPT-4o ($2.5/MTok)
SQL Generation → GPT-4o
Correction Plan → Claude Opus 3
Correction SQL → GPT-4o

Expected EA: ~85% (100-sample)
```

### For Budget-Constrained Deployment
```
All agents → GPT-4o-mini ($0.15/MTok)
Expected EA: ~87% (100-sample)

⚠️ Note: Non-reasoning models produce "valid but logically wrong SQL" more often
  - Confusing columns across tables
  - Forgetting GROUP BY
  - Selecting wrong/extraneous columns
```

### ❌ NOT Recommended
```
All agents → GPT-3.5 ($0.0015/MTok)
Expected EA: ~67% — too low for production

All agents → Open-source (Llama 3.1 8B, Qwen 2.5 1.5B)
Expected EA: ~45.3% — hallucination issues, high latency (3x slower), missing columns
```

## Temperature and Hyperparameters

| Parameter | Value | Reason |
|-----------|-------|--------|
| Temperature | **0** | Deterministic outputs; higher temps reduce plan faithfulness |
| Top_p | Default | No override needed with temperature 0 |
| Other hyperparams | Default | Keep simple — no complex tuning needed |

## When to Switch Models

| Signal | Action |
|--------|--------|
| Budget is unlimited | → Use Claude Opus 3 for all agents |
| Budget is moderate (~$30/run) | → Use hybrid (Opus for reasoning, GPT-4o for gen) |
| Budget is tight | → Use GPT-4o-mini for all (accept 87% EA) |
| Accuracy must be >90% | → Must use reasoning model for at least Schema Linking + Query Plan |
| Accuracy must be >85% | → Hybrid deployment is sufficient |
| Real-time latency critical | → Avoid open-source models (3x slower) |

## Practical Implementation Tips

1. **Start with hybrid**: Most cost-effective for production use
2. **Upgrade reasoning agents first**: If accuracy is low, upgrade Schema Linking and Query Plan to reasoning models before upgrading generation agents
3. **Monitor error patterns**: If generation agents produce many syntax errors → upgrade to better model. If they produce logically wrong SQL → upgrade the Query Plan agent
4. **缓存 MDL**: 通过 `wren context build` 构建后跨查询复用 — the Schema Linking cost drops significantly
5. **Track correction frequency**: If >50% of queries enter the correction loop → upgrade the Query Plan agent (better initial plans reduce correction needs)