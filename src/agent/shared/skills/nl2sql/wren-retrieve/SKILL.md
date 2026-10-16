---
version: 0.2.0
name: wren-retrieve
description: "触发：wren-orchestrator 步骤(1)。一次性并行调用 结构轴 get_context + 口径轴 get_context(item_type=knowledge) + recall_queries + get_instructions + list_knowledge 五路取料，全程每路至多一次（get_context 两种入参各至多一次），产出结构化取料包供下游只读消费。禁止逐个串行试探、禁止下游重复检索。"
---

# 五路并行取料

## 概述

官方 SOP step 1–3 的落地补全版。两个事实决定五轴形态：

1. `get_instructions` **只读 `knowledge/rules/*.md`**（源码写死），glossary/metrics/caveats 的正文它带不出来；
2. 本部署检索层为四类知识提供 **top-k 正文出口**：`get_context(item_type="knowledge")`（LanceDB FTS + 向量双腿 + RRF 融合，每条带来源路径）。

⇒ **口径轴**是知识正文的主通道；`get_instructions` 降为 **rules 全文核对 / 逃生口**（全局约束兜底，见专节）。

## 取料矩阵（同一批发出，互不依赖入参）

| 轴 | 工具（签名） | 覆盖范围 | 产出块 |
|----|-------------|---------|--------|
| 结构轴 | `get_context(question, limit=8)` | MDL：model/column/cube（**Cube 段已含 measures/dimensions/time_dimensions**） | `structure` |
| 口径轴 | `get_context(question, item_type="knowledge", limit=8)` | `knowledge/{rules,glossary,metrics,caveats}/*.md` 四类**正文**并集 top-k（每条带来源路径；也可按需单传 `glossary`/`metrics`/`caveats`/`rules` 窄化） | `knowledge` |
| 范例轴 | `recall_queries(question, limit=3)` | `knowledge/sql/*.md` 历史 NL→SQL 样板 | `exemplars` |
| 规则兜底 | `get_instructions()` | **仅** `knowledge/rules/*.md` **全文**（逐字、不切块、不排序）——全局约束兜底 | `rules` |
| 知识面 | `list_knowledge()` | `metrics`/`glossary`/`caveats`/`rules`/`sql` 的**文件清单**（只给名字、**不含正文**；只作来源标注与存在性核对） | `knowledge.files` |

> 不调 `list_cubes`：full 模式下 `get_context` 已含全部 cube 定义，判断与填参就地完成。仅当检索后端为向量 top-K（可能漏召）时才作为兜底加入本批。
> 知识**正文**有三条通道：口径轴（四类 top-k，**主通道**）、`get_instructions()`（rules 全文）、`recall_queries()`（sql 范例）；`list_knowledge()` 只给**文件名**，别拿清单里的 `.md` 去 `read_file`/`grep`。
> `get_all_knowledge()`（一次读全）**从未进过任何 wrenai 发布版**，本部署工具清单里没有它 —— **别调**；**禁止对不存在的工具反复硬调**（既浪费一轮又给上下文塞一条错误消息）。
> **不要**试图用 `read_file` 按路径去读知识文件——`knowledge/**` 不在可读 VFS 通道内（见下「落盘读回」）。

## `get_instructions` 定位：rules 全文核对 / 逃生口

- **为什么保留全文**：rules 是**全局约束**（`deleted=0` 类软删过滤、剔除测试账号、缺省日期口径等），不是「与问题相关才有用」——top-k 按相关性排序漏掉一条，SQL 就错且不自知。全文是规则轴上「宁多给不可空给」的兜底。
- **核对**：sql-author 的「规则/陷阱注入」以 `rules` 块全文为逐字依据（口径必须能引用原文，不许概括改写）；口径轴命中的 rules 类片段只是**焦点提示**，不替代全文。
- **逃生口**：与结构轴的 `describe_model`/`describe_schema` 对称——先 top-k、全文兜底。口径轴零命中/覆盖明显不足、或回答需逐字引用口径时，以 `rules` 全文为准（本批已取到，无需再调）。

## 口径轴降级判别（必须看）

正常返回是 top-k 知识片段（`### 标题 [knowledge/….md#n]` + 正文）。若回来的是**表结构/完整 schema**（`strategy:"full"`，或正文全是表列定义）⇒ 检索层不可用（开关关闭/索引陈旧），此时 `item_type` 被忽略——该轴按**降级**处理：`knowledge` 正文桶置空、`metadata.degraded` 记 `"knowledge:retrieval_unavailable"`，正文退回 `rules` 全文 + `knowledge.files` 清单。
**零命中**（产物写明「知识检索零命中」）**不是降级**：`knowledge` 正文桶置空即可，不记 degraded。

## 输出

write_file 到 `/workspace/nl2sql_process_data/{thread_id}/skill_sop/wren-retrieve/retrieval.json`：

```json
{
  "question": "原始问题",
  "structure": {"strategy": "full|search", "models": [], "cubes": [{"name": "", "measures": [{"name": "", "expression": ""}], "dimensions": [], "time_dimensions": []}]},
  "knowledge": {"glossary": [{"title": "", "src_path": "", "text": ""}], "metrics": [], "caveats": [], "rules": [], "files": []},
  "exemplars": [{"nl_query": "", "sql_query": "", "similarity": 0.0}],
  "rules": [],
  "metadata": {"degraded": [], "retrieved_at": ""}
}
```

> `knowledge` 填桶：口径轴 top-k 片段按来源路径的 kind 归桶（`knowledge/glossary/…` → `glossary`，以此类推），每条保留 `src_path` 与 `text` 原文；`files` 填 `list_knowledge()` 清单。`rules` 块 = `get_instructions()` 全文（逐字）。

## 关键规则

- **唯一性铁律**：五路取料各至多一次（`get_context` 两种形态各至多一次：不带 `item_type` = 结构轴、`item_type="knowledge"` = 口径轴）；下游（clarify/metric-query/author）只读本取料包。
- **容错**：任一轴失败 → 对应块置空数组 + `metadata.degraded` 记录轴名与原因，其余轴照常交付；五轴全失败 → 输出错误 JSON 由编排器降级。
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
  - 知识类工具（`get_instructions` / `list_knowledge`）已配**免截断白名单**
    （`KNOWLEDGE_EXEMPT_MAX_CHARS`），正常情况下不再落盘；**结构轴/口径轴（`get_context` 两路）
    与范例轴（`recall_queries`）仍会落盘**，按上面规则读回。
