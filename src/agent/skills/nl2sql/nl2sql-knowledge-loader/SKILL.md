---
version: 0.2.0
name: knowledge-loader
description: 从知识库中查询全量业务规则、知识库内容和指标定义等业务知识`K`
---

# NL2SQL 业务知识加载 Skill

## 概述

SQL-of-Thought 流水线第1步。从知识库中查询全量业务规则、知识库内容和指标定义等业务知识`K`，并输出过滤后的结构化 JSON。

**性能优先**：全部使用 MCP 工具（已按当前数据库自动路由），不使用 read_file。

## 输入
- 自然语言问题 (Q)
- 从对话历史中获取用户消息

## 输出
- 在回复末尾输出结构化 JSON（````json` 代码块），供编排器传递给下游 skill
- 仅在数据 >15KB 时 write_file 到 `/workspace/nl2sql_process_data/{thread_id}/knowledge-loader/knowledge.json` 作为 fallback

## 执行步骤

### 步骤 1: 数据收集（并行调用 MCP 工具）

同时调用以下 3 个 MCP 工具（工具已按当前数据库自动路由，无需指定项目路径）：

1. **`get_instructions()`** — 获取业务规则（返回 rules/*.md 全量内容）
2. **`recall_queries(question=用户问题, limit=5)`** — 语义搜索匹配的历史 SQL 示例
3. **`get_all_knowledge()`** — 一次读取 metrics/glossary/caveats 全部知识文件内容

### 步骤 2: 过滤与输出

根据用户问题提取关键词，从步骤 1 的结果中过滤保留相关内容：

- 只保留包含用户问题关键词的规则/指标/知识
- 只保留与用户问题领域相关的内容
- 按相关性排序，每种类型最多保留 10 条

然后在回复末尾输出以下 JSON 结构：

```json
{
  "user_intent": {
    "original_question": "原始用户问题",
    "domain": "业务领域",
    "keywords": ["关键词1", "关键词2"],
    "intent_type": "意图类型"
  },
  "entities": {
    "time_range": {"start": "", "end": ""},
    "filters": [{"field": "", "operator": "", "value": ""}],
    "fields_to_display": ["字段1", "字段2"]
  },
  "metrics": [
    {
      "name": "指标名",
      "aliases": ["别名"],
      "formula": "计算公式/SQL",
      "related_tables": ["表名"],
      "aggregation": "聚合方式"
    }
  ],
  "business_rules": [
    {
      "id": "rule_id",
      "name": "规则名",
      "condition": "过滤条件"
    }
  ],
  "field_mappings": {
    "业务术语": "物理字段"
  },
  "historical_qa_pairs": [
    {
      "id": "hqa_id",
      "original_question": "历史问题",
      "sql": "历史SQL",
      "similarity_score": 0.0
    }
  ],
  "knowledge": [
    {
      "topic": "主题",
      "content": "内容"
    }
  ],
  "context_summary": {
    "main_tables": ["主表"],
    "main_fields": ["主字段"],
    "filter_conditions": ["过滤条件"],
    "time_range": "时间范围"
  },
  "metadata": {
    "phase": "knowledge_loaded",
    "status": "success",
    "total_queried": {"rules": 0, "history": 0},
    "filtered_saved": {"rules": 0, "history": 0}
  }
}
```

## 错误处理

- 如果 MCP 工具调用全部失败，在回复末尾输出错误 JSON：
  ```json
  {"error": "业务知识加载失败", "detail": "..."}
  ```
- 部分工具失败不影响整体——用成功返回的数据继续，缺失部分标记为空数组
