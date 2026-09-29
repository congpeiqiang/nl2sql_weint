"""只读检索的入口：两腿交给后端，**融合（RRF）与结果形状留在这里唯一一份**。

## 分层

```
search()  ← RRF 融合 + 结果形状 + 文本截断（本文件）
   └── backends.lance   ← 默认：LanceDB（icu 分词，on-disk mmap）
   └── backends.jsonl   ← 降级：自写 idf-n-gram + 预缓存模长的余弦
```

两腿各自返回**已定序**的 `[{id, score, row}]`，融合只吃名次（`Σ 1/(60+rank)`），
所以两腿分数量纲不同无所谓 —— 这是 RRF 的意义，也是本层不必知道后端打分口径的原因。

## 为什么 FTS 曾经必须自写（历史，`jsonl` 后端仍靠它；`lance` 后端改用 icu 分词绕开）

- wren 0.13.0 **完全没有** `create_fts_index` / `query_type` / `rerank` 这些面；
- 自写腿当年的动机是「trigram 类引擎对两字中文词恒空」。⚠️ **2026-09-28 复测修正**：那条
  只对 **FTS5 `tokenize='trigram'`（3 是引擎硬下限）** 成立；LanceDB 的 `ngram` 之 3 是
  **配置默认值** —— `ngram_min_length=2` 时两字中文正常命中。所以现在选 icu 是**精度**
  选择（按词切），不是「唯一能用」；icu 的代价是**不切 `_`**，靠 `_index_text` 的归一化列
  补（详见 `backends/lance.py` 模块 docstring）。分词器事实由
  `scripts/verify_retrieval.py` 的「分词器事实」段实际跑出来，不靠记忆。
- 标识符必须按 `_` 切开（`customer_t` 要能被 `customer` 命中），ICU **不做**这件事，
  所以 lance 侧靠「归一化列 + 原始标识符追加」实现（同上）。

## 契约

- **读侧永不抛**：索引缺失 / 损坏 / 嵌入通道挂了 ⇒ 返回 `[]`，调用方回落全量注入（L2）。
- **`hybrid` 缺嵌入 ⇒ 自动降级 `fts`**：索引里一条向量都没有就直接不试嵌入（连那 2.5s
  超时都不花）。
- **`hybrid` 但索引与嵌入配置不是同一套 ⇒ 也只走 FTS 腿**：`_embed_cfg_matches` 拿
  索引 meta 的 `embed_model`/`embed_dim` 与当前配置对（**配置改了没重建索引**这条路径，
  `embedder` 侧的响应模型名闸抓不到，因为服务端报告的正是新名字）。
- 返回条目 `text` **截断**（默认 600 字符）：调用方要拼进 prompt，不能让一条巨型知识
  文档把预算吃光。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from agent.retrieval import backends
from agent.retrieval import embedder
from agent.retrieval import schema as S

_log = logging.getLogger(__name__)

RRF_K = 60          # RRF 平滑常数（标准取值）
TEXT_CAP = 600      # 返回文本上限

#：返回给调用方的字段（后端私有列一律不出现）
_PUBLIC_COLS = ("id", "kind", "title", "text", "db_name", "src_path")


def _rrf(legs: list[list[dict]]) -> list[tuple[str, float]]:
    """自写 RRF：`Σ 1/(K + rank)`。按 `id` 名次融合。"""
    total: dict[str, float] = {}
    for leg in legs:
        for rank, hit in enumerate(leg, start=1):
            key = str(hit.get("id") or "")
            if not key:
                continue
            total[key] = total.get(key, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(total.items(), key=lambda pair: pair[1], reverse=True)


def _hydrate(row: dict, *, score: float, legs: list[str], text_cap: int) -> dict:
    """落库行 → 调用方看到的结果（**唯一**一处，两个后端共享）。"""
    out = {key: row.get(key) for key in _PUBLIC_COLS}
    try:
        out["meta"] = json.loads(str(row.get("meta_json") or "{}") or "{}")
    except Exception:  # noqa: BLE001 —— meta 坏了不影响检索结果本身
        out["meta"] = {}
    text = str(row.get("text") or "")
    out["text"] = text[:text_cap] + ("…" if len(text) > text_cap else "")
    out["score"] = round(float(score), 6)
    out["legs"] = legs
    return out


def _embed_cfg_matches(index_dir: Path) -> bool:
    """索引 meta 记的 `embed_model`/`embed_dim` 是否仍与**当前配置**一致。

    这是 `embedder._model_matches`（看响应体报告的模型名）的**另一半**，抓的正是
    它抓不到的那条路径：**配置改了、索引没重建**。换 `.env` 的 `NL2SQL_EMBED_MODEL`
    之后，服务端如实报告的是**新**模型名 ⇒ 响应闸放行，于是新查询向量与旧索引向量
    **同维数静默混用**（不报错，只是排序变垃圾）。meta 里记的是**建索引那一刻**的真相，
    所以只有它能判「配置与索引是不是同一套」。

    不一致 ⇒ 只做 FTS（§8.1 的 L1），**不**整体拒绝索引：FTS 腿的召回完全不受影响。
    `embed_model` 为空（`--no-vectors` 建的纯 FTS 索引）⇒ 放行 —— 那种索引一条向量都
    没有，「vectors==0」那条短路自会接管，不必在这里判。
    """
    meta = S.read_meta(index_dir)          # 读不到 ⇒ {}（永不抛）
    built = str(meta.get("embed_model") or "").strip()
    if not built:
        return True
    if not embedder.same_model(built, embedder.model_name()):
        _log.warning(
            "[retrieval] 索引按 %r 建的，当前配置是 %r ⇒ 本次只用 FTS 腿；"
            "两者同维数也会把向量静默混用（排序变垃圾但不报错），"
            "要么改回配置，要么**重建索引** %s",
            built,
            embedder.model_name(),
            index_dir,
        )
        return False
    want_dim = embedder.dim()
    try:                                   # meta 被手改坏 ⇒ 当「没记」，绝不让它抛出去
        got_dim = int(meta.get("embed_dim") or 0)
    except (TypeError, ValueError):
        got_dim = 0
    if got_dim and got_dim != want_dim:
        _log.warning(
            "[retrieval] 索引向量维数 %d ≠ 当前配置 %d ⇒ 本次只用 FTS 腿（重建索引 %s）",
            got_dim,
            want_dim,
            index_dir,
        )
        return False
    return True


def search(
    index_dir: Path,
    query: str,
    *,
    limit: int = 8,
    kinds: set[str] | None = None,
    db_name: str | None = None,
    text_cap: int = TEXT_CAP,
) -> list[dict]:
    """检索。返回 `[{id, kind, title, text, db_name, src_path, score, meta, legs}]`。

    索引不存在 / 不可读 ⇒ `[]`（调用方回落全量注入）。**永不抛**。
    """
    if not S.is_enabled() or not (query or "").strip():
        return []

    index_dir = Path(index_dir)
    try:
        backend = backends.open_backend(index_dir)
        if not backend.ready():
            return []
    except Exception as e:  # noqa: BLE001 —— 后端构造失败也算「没有索引」
        _log.warning("[retrieval] 后端不可用 %s: %s", index_dir, e)
        return []

    fetch = max(limit * 3, 20)
    legs: list[list[dict]] = []
    names: list[str] = []

    fts_hits = backend.fts(query, limit=fetch, kinds=kinds, db_name=db_name)
    if fts_hits:
        legs.append(fts_hits)
        names.append("fts")
    if S.vector_enabled() and _embed_cfg_matches(index_dir):
        vec_hits = backend.vector(query, limit=fetch, kinds=kinds, db_name=db_name)
        if vec_hits:
            legs.append(vec_hits)
            names.append("vector")

    if not legs:
        return []

    fused = _rrf(legs) if len(legs) > 1 else [
        (str(hit.get("id") or ""), float(hit.get("score") or 0.0)) for hit in legs[0]
    ]
    rows = {}
    for leg in legs:
        for hit in leg:
            key = str(hit.get("id") or "")
            if key and key not in rows:
                rows[key] = hit.get("row") or {}
    membership = {name: {str(h.get("id") or "") for h in leg} for name, leg in zip(names, legs)}

    out: list[dict] = []
    for key, score in fused[:limit]:
        row = rows.get(key)
        if not row:
            continue
        out.append(_hydrate(
            row,
            score=score,
            legs=[name for name, ids in membership.items() if key in ids],
            text_cap=text_cap,
        ))
    return out


def search_project(project: Path, query: str, **kwargs) -> list[dict]:
    """按项目目录检索（省掉调用方拼 `index_dir_for`）。"""
    return search(S.index_dir_for(project), query, **kwargs)
