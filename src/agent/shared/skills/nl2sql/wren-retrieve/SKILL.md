---
version: 0.1.0
name: wren-retrieve
description: "触发：wren-orchestrator 步骤(1)。一次性并行调用 get_context + recall_queries + get_instructions + list_knowledge 四路取料，全程各至多一次，产出结构化取料包供下游只读消费。禁止逐个串行试探、禁止下游重复检索。"
---

# 四路并行取料

## 概述

官方 SOP step 1–3 的落地补全版：`get_instructions` **只读 `knowledge/rules/*.md`**（源码写死），业务术语/指标定义/陷阱不会自动带出，故必须扩为**四轴并行**，一次拿齐。

## 取料矩阵（同一批发出，互不依赖入参）

| 轴 | 工具（签名） | 覆盖范围 | 产出块 |
|----|-------------|---------|--------|
| 结构轴 | `get_context(question, limit=8)` | MDL：model/column/cube（**Cube 段已含 measures/dimensions/time_dimensions**） | `structure` |
| 范例轴 | `recall_queries(question, limit=3)` | `knowledge/sql/*.md` 历史 NL→SQL 样板 | `exemplars` |
| 规则 | `get_instructions()` | **仅** `knowledge/rules/*.md` | `rules` |
| 知识面 | `list_knowledge()` | `metrics`/`glossary`/`caveats`/`rules`/`sql` 的**文件清单**（只给名字、**不含正文**；只作来源标注与存在性核对） | `knowledge` |

> 不调 `list_cubes`：full 模式下 `get_context` 已含全部 cube 定义，判断与填参就地完成。仅当检索后端为向量 top-K（可能漏召）时才作为兜底加入本批。
> 知识**正文**只有 `get_instructions()`（`rules/*.md`）与 `recall_queries()`（`sql/*.md`）两条通道；`list_knowledge()` 只给**文件名**，别拿清单里的 `.md` 去 `read_file`/`grep`。
> `metrics`/`glossary`/`caveats` 的正文**上游 wren 0.15.0 及以前都没有读取工具**（本部署同样没有）。`get_all_knowledge()`（一次读全）**从未进过任何 wrenai 发布版**，本部署工具清单里没有它 —— **别调**；只有清单里确实出现时才用它替代 `list_knowledge()`。**禁止对不存在的工具反复硬调**（既浪费一轮又给上下文塞一条错误消息）；
> **不要**试图用 `read_file` 按路径去读知识文件——`knowledge/**` 不在可读 VFS 通道内（见下「落盘读回」）。

## 输出

write_file 到 `/workspace/nl2sql_process_data/{thread_id}/skill_sop/wren-retrieve/retrieval.json`：

```json
{
  "question": "原始问题"
  "structure": {"strategy": "full|search", "models": [], "cubes": [{"name": "", "measures": [{"name": "", "expression": ""}], "dimensions": [], "time_dimensions": []}]},
  "exemplars": [{"nl_query": "", "sql_query": "", "similarity": 0.0}],
  "rules": [],
  "knowledge": {"glossary": [], "metrics": [], "caveats": []},
  "metadata": {"degraded": [], "retrieved_at": ""}
}
```

## 关键规则

- **唯一性铁律**：四工具各至多一次；下游（clarify/metric-query/author）只读本取料包。
- **容错**：任一轴失败 → 对应块置空数组 + `metadata.degraded` 记录轴名与原因，其余轴照常交付；四轴全失败 → 输出错误 JSON 由编排器降级。
- **裁剪**：`structure`/`knowledge` 按问题关键词过滤，只留相关条目，避免全量塞上下文。
- **落盘读回（重要）**：任一轴结果超过 8000 字符会被 MessageSlimmer 落盘，工具结果里只剩
  「头 5 行 + `...[N lines truncated]...` + 尾 5 行」预览 + 路径指针
  `/workspace/large_tool_results/<tool_call_id>`。
  - ✅ 要全文 → `read_file` **那个指针路径**（可带 `offset`/`limit` 分段读），它是真实的 VFS 路径。
  - ⚠️ 正文里出现的 `xxx.md`（如头 5 行里那句「工时专项见 `报工与工时.md`」）是**来源标注**，
    表示该文件内容**已在同一份返回里**（往往就在被砍掉的中段）——不是让你去打开的文件。
  - 🚫 **禁止**用 `read_file`/`grep`/`glob`/`ls` 去找 `knowledge/**`：那五个分类目录只经 MCP 工具
    投递，不在子 agent 的可读通道内（真路径还含一段不可推导的语义库目录名），去找只会拿到
    `permission denied`；**拿到 denied 不要换路径重试**，回到落盘路径或降级记录 `metadata.degraded`。
  - 知识类工具（`get_instructions` / `get_all_knowledge` / `list_knowledge`）已配**免截断白名单**
    （`KNOWLEDGE_EXEMPT_MAX_CHARS`），正常情况下不再落盘；**其它两轴（`get_context` / `recall_queries`）
    仍会落盘**，按上面规则读回。
