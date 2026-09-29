"""JSONL 后端：零依赖的降级路径（lancedb 装不出来时的唯一选择）。

打分器是自写的：**idf 加权的字符 n-gram** + 子串命中加成 + 标题加成。为什么自写而不是
用引擎（FTS5 trigram / lance ngram）—— 见 `search.py` 顶部的实测记录，两条硬约束：

1. **两字中文词**（工时 / 部门 / 项目）在 **FTS5 `tokenize='trigram'`** 里匹配不到任何东西
   且不报错（3 是它的引擎硬下限）。⚠️ 2026-09-28 复测：LanceDB 的 `ngram_min_length=3`
   是**配置默认值**，调到 2 就能命中两字 —— 所以本层自写的理由只剩「零依赖降级」，
   不再有「引擎做不到」这一条；
2. **ASCII 标识符必须按 `_` 切开**（`customer_t` 要能被 `customer` 命中）。icu 与 trigram
   都不做，本层靠 `_TOKEN_RE` 不含下划线实现；lance 侧靠归一化列实现（同一条约束，两种绕法）。

⚠️ 向量腿的模长**必须缓存**（实施期实测）：原先每次查询都现算全部向量的模长，233 条
×1024 维 = **153ms/查**，其中纯余弦只占 9.6ms —— 多出来的 140ms 全是模长。修完之后
命中 numpy 走矩阵乘（233 条 0.02ms / 5000 条 0.54ms），没 numpy 则回落到预缓存模长的
纯 Python 通道。
"""
from __future__ import annotations

import logging
import math
import re
import threading
from pathlib import Path

from agent.retrieval import embedder
from agent.retrieval import store

_log = logging.getLogger(__name__)

NGRAM = 2           # n-gram 下限（**不是 3**：两字业务词要能进 idf 加权）
NGRAM_MAX = 6       # 上限：长串整词命中靠子串通道，n-gram 只做粗召回
SUBSTR_BOOST = 2.5  # 整串子串命中的加成

# 注意：**不含下划线** —— 物理表名 `customer_t` 必须被切成 `customer` + `t`，
# 否则查 `customer` 命中不了（真语料实测全空）。
_TOKEN_RE = re.compile(r"[0-9A-Za-z]+|[一-鿿]+")

# 缓存一律**按路径分桶**，绝不用单槽（本仓已有教训：拿全局变量当单槽会多库互踩）。
_GRAM_CACHE: dict[str, tuple[str, list[set[str]], dict[str, int]]] = {}
_VEC_CACHE: dict[str, tuple[str, object]] = {}
_LOCK = threading.Lock()


# ── FTS 腿 ─────────────────────────────────────────────
def _ngrams(text: str, n: int = NGRAM) -> set[str]:
    """字符 n-gram。中文与 ASCII **同一套生成**（`客户` 与 `customer` 都走二元起）。"""
    out: set[str] = set()
    for token in _TOKEN_RE.findall(text or ""):
        token = token.lower()
        if len(token) < 2:
            continue
        for size in range(n, min(len(token), NGRAM_MAX) + 1):
            for i in range(len(token) - size + 1):
                out.add(token[i : i + size])
    return out


def _idf(ngram: str, doc_count: int, freq: dict[str, int]) -> float:
    return math.log(1.0 + doc_count / (1.0 + freq.get(ngram, 0)))


def _gram_index(index_dir: Path, rows: list[dict]) -> tuple[list[set[str]], dict[str, int]]:
    """每条的 n-gram 集合 + 全局文档频率（df）。按 `(路径, 索引戳)` 缓存。

    不缓存时每次查询都要给全部条目重新产 gram，233 条实测 40~57ms/查。
    """
    key = str(index_dir)
    stamp = store.stamp(index_dir)
    with _LOCK:
        hit = _GRAM_CACHE.get(key)
        if hit and hit[0] == stamp:
            return hit[1], hit[2]

    doc_grams: list[set[str]] = []
    freq: dict[str, int] = {}
    for row in rows:
        grams = _ngrams(f"{row.get('title', '')} {row.get('text', '')}")
        doc_grams.append(grams)
        for gram in grams:
            freq[gram] = freq.get(gram, 0) + 1

    with _LOCK:
        _GRAM_CACHE[key] = (stamp, doc_grams, freq)
    return doc_grams, freq


# ── 向量腿 ─────────────────────────────────────────────
def _vec_index(index_dir: Path, rows: list[dict], vectors: list[list[float] | None]):
    """向量腿的加速结构（按索引戳缓存）：numpy 归一化矩阵，或纯 Python 的「条目下标, 归一化向量, 模长」。

    空洞条目（`None`，增量更新只嵌了新增几条时会出现）在这里就被剔除。
    """
    key = str(index_dir)
    stamp = store.stamp(index_dir)
    with _LOCK:
        hit = _VEC_CACHE.get(key)
        if hit and hit[0] == stamp:
            return hit[1]

    slots = [(idx, vec) for idx, vec in enumerate(vectors or []) if vec]
    built: object | None = None
    if slots:
        dims = {len(vec) for _idx, vec in slots}
        if len(dims) == 1:
            try:
                import numpy as np

                mat = np.asarray([vec for _idx, vec in slots], dtype="float32")
                norms = np.linalg.norm(mat, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                # 形状必须是二元组 `(mode, payload)` —— `_rank_vector` 按二元解包
                built = ("np", ([idx for idx, _ in slots], mat / norms))
            except Exception as e:  # noqa: BLE001 —— 没 numpy 就走纯 Python，不是错误
                _log.debug("[retrieval] numpy 不可用，向量腿走纯 Python: %s", e)
        if built is None:
            pairs = []
            for idx, vec in slots:
                norm = math.sqrt(sum(v * v for v in vec)) or 1.0
                pairs.append((idx, vec, norm))
            built = ("py", pairs)

    with _LOCK:
        _VEC_CACHE[key] = (stamp, built)
    return built


# ── 后端 ───────────────────────────────────────────────
class JsonlBackend:
    name = "jsonl"

    def __init__(self, index_dir: Path) -> None:
        self.dir = Path(index_dir)

    def ready(self) -> bool:
        meta = store.stats(self.dir)
        return bool(meta.get("ready"))

    def stats(self) -> dict:
        return store.stats(self.dir)

    def write(self, rows: list[dict], vectors: list[list[float] | None] | None) -> dict:
        """全量落盘（本后端不做增量：条目只有几百，全量重建 < 1s）。"""
        store.save(self.dir, rows, vectors)
        return {"ok": True, "backend": self.name, "items": len(rows),
                "vectors": sum(1 for v in (vectors or []) if v)}

    def upsert(self, rows: list[dict], vectors: list[list[float] | None] | None) -> dict:
        """按 id 合并后全量重写。**必须保留已有向量**（只嵌了新条目，不是不要旧的）。"""
        try:
            old_rows, old_vectors = store.load(self.dir)
            merged: dict[str, tuple[dict, list[float] | None]] = {}
            for pos, row in enumerate(old_rows):
                merged[str(row.get("id") or "")] = (
                    row, (old_vectors[pos] if old_vectors and pos < len(old_vectors) else None))
            for pos, row in enumerate(rows):
                key = str(row.get("id") or "")
                new_vec = vectors[pos] if vectors and pos < len(vectors) else None
                prev = merged.get(key)
                # 新条目没带向量时沿用旧的（嵌入失败不许把已有向量抹掉）
                merged[key] = (row, new_vec or (prev[1] if prev else None))
            seq = [v for v in merged.values() if str(v[0].get("id") or "")]
            store.save(self.dir, [r for r, _v in seq], [v for _r, v in seq])
            return {"ok": True, "backend": self.name, "items": len(rows),
                    "vectors": sum(1 for _r, v in seq if v)}
        except Exception as e:  # noqa: BLE001
            _log.warning("[retrieval] jsonl 增量写失败 %s: %s", self.dir, e)
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"}

    def fts(self, query: str, *, limit: int, kinds: set[str] | None = None,
            db_name: str | None = None) -> list[dict]:
        try:
            rows, _vectors = store.load(self.dir)
            if not rows:
                return []
            return self._rank(rows, query, kinds=kinds, db_name=db_name, limit=limit)
        except Exception as e:  # noqa: BLE001 —— 读侧永不抛
            _log.warning("[retrieval] jsonl FTS 腿失败 %s: %s", self.dir, e)
            return []

    def vector(self, query: str, *, limit: int, kinds: set[str] | None = None,
               db_name: str | None = None) -> list[dict]:
        try:
            rows, vectors = store.load(self.dir)
            if not rows or not vectors:
                return []
            return self._rank_vector(rows, vectors, query, kinds=kinds, db_name=db_name, limit=limit)
        except Exception as e:  # noqa: BLE001
            _log.warning("[retrieval] jsonl 向量腿失败 %s: %s", self.dir, e)
            return []

    # —— 打分 ——
    def _rank(self, rows: list[dict], query: str, *, kinds, db_name, limit) -> list[dict]:
        q_grams = _ngrams(query)
        q_lower = (query or "").strip().lower()
        if not q_grams and not q_lower:
            return []

        doc_grams, freq = _gram_index(self.dir, rows)
        doc_count = max(len(rows), 1)
        weights = {gram: _idf(gram, doc_count, freq) for gram in q_grams}

        scored: list[tuple[int, float]] = []
        for idx, row in enumerate(rows):
            if kinds and row.get("kind") not in kinds:
                continue
            if db_name and row.get("db_name") != db_name:
                continue
            score = 0.0
            for gram in q_grams & doc_grams[idx]:
                score += weights.get(gram, 0.0)
            if q_lower:
                title = str(row.get("title") or "").lower()
                text = str(row.get("text") or "").lower()
                if q_lower in title:
                    score += SUBSTR_BOOST * 2
                elif q_lower in text:
                    score += SUBSTR_BOOST
                # 长查询按词再补一刀（问句里带多个业务词时，命中越多分越高）
                for part in {p for p in _TOKEN_RE.findall(q_lower) if len(p) >= 2}:
                    if part in title or part in text:
                        score += 0.5
            if score > 0:
                scored.append((idx, score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [{"id": str(rows[idx].get("id") or ""), "score": score, "row": rows[idx]}
                for idx, score in scored[:limit]]

    def _rank_vector(self, rows: list[dict], vectors, query: str, *, kinds, db_name, limit) -> list[dict]:
        built = _vec_index(self.dir, rows, vectors)
        if not built:
            return []
        qvec = embedder.embed_one(query)
        if not qvec:
            _log.info("[retrieval] 查询嵌入不可用 ⇒ 本次只用 FTS 腿")
            return []

        mode, payload = built
        hits: list[tuple[int, float]] = []
        if mode == "np":
            import numpy as np

            slots, mat = payload
            q = np.asarray(qvec, dtype="float32")
            if q.shape[0] != mat.shape[1]:
                _log.info("[retrieval] 查询嵌入维度不符（%d≠%d）⇒ 本次只用 FTS 腿",
                          q.shape[0], mat.shape[1])
                return []
            qn = float(np.linalg.norm(q)) or 1.0
            sims = (mat @ q) / qn
            order = np.argsort(-sims)
            for slot in order[: max(limit * 3, limit)]:
                idx = slots[int(slot)]
                row = rows[idx]
                if kinds and row.get("kind") not in kinds:
                    continue
                if db_name and row.get("db_name") != db_name:
                    continue
                hits.append((idx, float(sims[int(slot)])))
                if len(hits) >= limit:
                    break
        else:
            qnorm = math.sqrt(sum(v * v for v in qvec)) or 1.0
            for idx, vec, norm in payload:
                row = rows[idx]
                if kinds and row.get("kind") not in kinds:
                    continue
                if db_name and row.get("db_name") != db_name:
                    continue
                if len(vec) != len(qvec):
                    continue
                dot = sum(a * b for a, b in zip(qvec, vec))
                hits.append((idx, dot / (qnorm * norm)))
            hits.sort(key=lambda pair: pair[1], reverse=True)
            hits = hits[:limit]

        return [{"id": str(rows[idx].get("id") or ""), "score": score, "row": rows[idx]}
                for idx, score in hits]
