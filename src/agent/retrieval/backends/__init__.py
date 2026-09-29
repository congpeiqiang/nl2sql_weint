"""后端选择与协议：LanceDB（默认）与 JSONL（降级路径）。

## 协议（只有三个方法，两侧都必须满足）

每条腿返回**已定序**的 `[{"id", "score", "row"}]`：

- `id`   稳定条目 id（`schema.item_id`）；
- `score` **越大越靠前**（两腿分数量纲不同没关系 —— 融合只取名次）；
- `row`  落库行的原样 dict（含 `meta_json`；**不含** `text_idx` / `vector` 等后端私有列）。

把「定序」留给后端、把「融合 + 结果形状 + 文本截断」留在 `search.py` 一处，是为了
避免同一语义出现两份判据（本仓的老毛病）。RRF 只吃名次，所以两腿的 score 不需要可比。

## 为什么 lancedb 的 import 必须在这里惰性发生

生产镜像在重建之前**没有** lancedb（镜像用 `uv sync` 装 lockfile，而 lancedb 是按
`langchain-qwq` 那条先例单独 `uv pip install` 的）。任何 import 期 import 都会把
整个 agent 拖死 —— 所以 `lance_available()` 是**唯一**的真实 import 探测点，且带缓存。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from agent.retrieval import schema as S

_log = logging.getLogger(__name__)

_lance_ok: bool | None = None


class Backend(Protocol):
    """一个索引后端。**所有方法都不许抛**（读侧抛 = 拖垮调用方）。"""

    name: str

    def ready(self) -> bool: ...
    def stats(self) -> dict: ...
    def write(self, rows: list[dict], vectors: list[list[float] | None] | None) -> dict: ...
    def upsert(self, rows: list[dict], vectors: list[list[float] | None] | None) -> dict: ...
    def fts(self, query: str, *, limit: int, kinds: set[str] | None, db_name: str | None) -> list[dict]: ...
    def vector(self, query: str, *, limit: int, kinds: set[str] | None, db_name: str | None) -> list[dict]: ...


def lance_available() -> bool:
    """lancedb 是否装得出来。**唯一的真实 import 探测点**，结果缓存。"""
    global _lance_ok
    if _lance_ok is None:
        try:
            import lancedb  # noqa: F401

            _lance_ok = True
        except Exception as e:  # noqa: BLE001 —— 缺包/装坏都算不可用
            _log.warning("[retrieval] lancedb 不可用 ⇒ 退回 jsonl 后端（检索能力降级，行为不变）: %s", e)
            _lance_ok = False
    return _lance_ok


def backend_name() -> str:
    """生效的后端名（env 指定 lance 但装不出来 ⇒ 退回 jsonl）。"""
    want = S.retrieval_backend()
    if want == S.BACKEND_LANCE and not lance_available():
        return S.BACKEND_JSONL
    return want


def open_backend(index_dir: Path) -> Backend:
    """按生效后端构造（每次调用新建，后端内部自己管缓存）。"""
    index_dir = Path(index_dir)
    if backend_name() == S.BACKEND_LANCE:
        from agent.retrieval.backends import lance

        return lance.LanceBackend(index_dir)
    from agent.retrieval.backends import jsonl

    return jsonl.JsonlBackend(index_dir)


__all__ = ["Backend", "backend_name", "lance_available", "open_backend"]
