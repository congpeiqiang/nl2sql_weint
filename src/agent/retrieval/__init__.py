"""混合检索薄层（LanceDB + FTS，方案见 `docs/agent优化记录/混合检索方案.md`）。

分层：`schema`（开关/后端/落点/条目）→ `embedder`（向量）→ `indexer`（唯一写入口）→
`backends`（lance / jsonl 两个后端）→ `search`（只读：融合 + 结果形状）→
`adapters`（四个消费点的形状适配）。

**三条铁律**：

1. **默认关**（`NL2SQL_RETRIEVAL=off`）⇒ 全链与今天逐字一致（R2）。
2. **不 import 期依赖 LanceDB**：镜像重建前没有这个包，所有 lancedb 导入都必须在函数
   内惰性进行，否则会把整个 agent 一起带崩。唯一的探测点是 `backends.lance_available()`。
3. **后端可降级**：lance 装不出来 ⇒ `backends` 层自动退回 jsonl（读侧行为不变，
   只是内存曲线差一档）。
"""
from __future__ import annotations

from agent.retrieval.embedder import embed_one, embed_texts, health as embed_health
from agent.retrieval.schema import (
    ALL_KINDS,
    BACKEND_JSONL,
    BACKEND_LANCE,
    INDEX_DIR_NAME,
    Item,
    MODE_FTS,
    MODE_HYBRID,
    MODE_OFF,
    index_dir_for,
    is_enabled,
    item_id,
    project_rev,
    registered_projects,
    retrieval_backend,
    retrieval_mode,
    vector_enabled,
)

__all__ = [
    "ALL_KINDS",
    "BACKEND_JSONL",
    "BACKEND_LANCE",
    "INDEX_DIR_NAME",
    "Item",
    "MODE_FTS",
    "MODE_HYBRID",
    "MODE_OFF",
    "embed_health",
    "embed_one",
    "embed_texts",
    "index_dir_for",
    "is_enabled",
    "item_id",
    "project_rev",
    "registered_projects",
    "retrieval_backend",
    "retrieval_mode",
    "vector_enabled",
]
