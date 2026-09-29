"""LanceDB 后端（默认）。**本模块是唯一 import lancedb 的地方，且全部惰性。**

## 为什么是它（2026-09-28 用户拍板：接受改 Dockerfile + 重建镜像）

实测 5000 条中文语料：自写腿稳态 18.4ms / gram 倒排 37MB / 向量（Python list）156MB；
lance 走 **on-disk mmap**，进程不物化语料 —— 优势在**内存与预建索引**，不在 FTS 延迟
（自写腿 18.4ms vs lance-icu 26.8ms，自写腿反而更快）。切它买的是「20k 条时不会
776MB/进程」这条曲线。

## 实施期实测踩出来的四条（**照抄，别按常识改**）

1. **`ngram` 的「3 字符」是配置默认值，不是引擎下限**（2026-09-28 复测推翻旧结论）。
   旧判「ngram 对中文静默零命中、必须用 icu」来自**废弃 API**
   `create_fts_index(tokenizer_name="ngram")` 的默认 `min=3`：两字查询产不出 ≥3 的 gram，
   于是恒返回 0 条且不报错。新 API `create_index(config=FTS(...))` 下的真相：把
   `ngram_min_length` 调到 **2**，两字中文（工时/部门）正常命中，`numpy` 与 `icu` 同题同解
   ⇒ 引擎侧没有硬下限。选 `icu` 的理由因此**只剩精度**（按词切，而不是字符重叠）——
   而它有一个代价，见下条。配置若换，`scripts/verify_retrieval.py` 的「分词器事实」段会报出来。
2. **icu 不把 `_` 当分隔符**（这是选 icu 的代价；ngram 靠字符重叠反而能命中）。实测
   `customer` 命中不了 `customer_t`（`invoice` ← `invoice_t` 同样），正是自写腿踩过的同一个
   坑。所以索引列 `text_idx` = **归一化文本（`_`→空格）＋ 原文里的下划线标识符原样追加**：
   归一化部分让 `customer` 命中，追加的原始 token 让 `customer_t` 精确命中（实测两条查询
   都只回正确那一条，无噪声）。查询串**原样传**，不做归一化。
3. **缓存的 Table 对象在 overwrite 之后仍返回旧数据**（实测：旧对象 count_rows=3、
   查新数据 0 条；新对象 1 条）⇒ 缓存必须以 `_versions/latest_version_hint.json`
   的 `{"version":N}` 为失效键，变了就重开。`open_table` 实测 **7.3ms**，不缓存则
   每次查询白付。
4. **`create_index` 是同步的**（41ms 返回后立刻可搜）；**新增行会被既有 FTS 索引自动
   覆盖**（`merge_insert` 后立刻可搜到新行），不需要重建索引。
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import timedelta
from pathlib import Path

from agent.retrieval import embedder
from agent.retrieval import schema as S

_log = logging.getLogger(__name__)

#：落库列（`text_idx` / `vector` 是后端私有列，不进返回结果）
COLS = ("id", "kind", "title", "text", "db_name", "src_path", "meta_json", "rev")
_IDENT_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_]*_[0-9A-Za-z_]+")
_HINT_REL = ("_versions", "latest_version_hint.json")

#：Table 缓存：{index_dir_str: (版本提示, table)} —— 见模块 docstring 第 3 条
_TABLE_CACHE: dict[str, tuple[str, object]] = {}
_META_CACHE: dict[str, tuple[str, dict]] = {}
_LOCK = threading.Lock()


def _index_text(title: str, text: str) -> str:
    """FTS 索引列：归一化文本 ＋ 原文里的下划线标识符（见模块 docstring 第 2 条）。"""
    body = f"{title or ''}\n{text or ''}"
    norm = body.replace("_", " ")
    raws = " ".join(dict.fromkeys(_IDENT_RE.findall(body)))
    return f"{norm}\n{raws}".strip()


def _quote(value: str) -> str:
    """SQL 字面量转义（单引号双写；顺手去掉换行/NUL —— db_name 来自运行期入参）。"""
    cleaned = str(value).replace("\x00", "").replace("\n", " ").replace("\r", " ")
    return "'" + cleaned.replace("'", "''") + "'"


def _norm_vec(vec: list[float] | None) -> list[float] | None:
    """单位化：归一化后 L2 距离与余弦**同序**，于是不必依赖 `distance_type`。"""
    if not vec:
        return None
    norm = sum(float(v) * float(v) for v in vec) ** 0.5
    if not norm:
        return None
    return [float(v) / norm for v in vec]


class LanceBackend:
    name = "lance"

    def __init__(self, index_dir: Path) -> None:
        self.dir = Path(index_dir)

    # ── 读侧 ─────────────────────────────────────────────
    def _meta(self) -> dict:
        """元数据（按版本提示缓存；元数据由 `indexer` 在写完之后落盘）。"""
        hint = self._hint()
        with _LOCK:
            hit = _META_CACHE.get(str(self.dir))
            if hit and hit[0] == hint:
                return hit[1]
        meta = S.read_meta(self.dir)
        with _LOCK:
            _META_CACHE[str(self.dir)] = (hint, meta)
        return meta

    def _hint(self) -> str:
        """版本提示（13 字节的小文件读取）—— 跨进程新鲜的唯一廉价判据。"""
        try:
            return S.table_dir(self.dir).joinpath(*_HINT_REL).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _table(self):
        """取 Table（按版本提示缓存）。表不存在 / lancedb 不可用 ⇒ `None`。"""
        import lancedb

        hint = self._hint()
        with _LOCK:
            hit = _TABLE_CACHE.get(str(self.dir))
            if hit and hit[0] == hint and hint:
                return hit[1]
        try:
            table = lancedb.connect(str(self.dir)).open_table(S.TABLE_NAME)
        except Exception as e:  # noqa: BLE001 —— 表不存在/损坏 ⇒ 读侧降级
            _log.info("[retrieval] lance 表不可用 %s: %s", self.dir, e)
            return None
        with _LOCK:
            _TABLE_CACHE[str(self.dir)] = (hint, table)
        return table

    def ready(self) -> bool:
        """索引是否可用：元数据声明的后端是 lance，且数据集目录在。"""
        meta = self._meta()
        if str(meta.get("backend") or "") not in ("", self.name):
            return False
        return bool(meta) and S.table_dir(self.dir).is_dir()

    def stats(self) -> dict:
        meta = self._meta()
        size = 0
        for f in self.dir.rglob("*"):
            try:
                if f.is_file():
                    size += f.stat().st_size
            except OSError:
                pass
        return {
            "index_dir": str(self.dir),
            "backend": str(meta.get("backend") or self.name),
            "ready": self.ready(),
            "bytes": size,
            "rev": meta.get("rev", ""),
            "items": meta.get("items", 0),
            "vectors": meta.get("vectors", 0),
            "built_at": meta.get("built_at", ""),
            "kinds": meta.get("kinds", {}),
        }

    def _where(self, kinds: set[str] | None, db_name: str | None, *extra: str) -> str:
        parts = [p for p in extra if p]
        if kinds:
            parts.append("kind IN (" + ", ".join(_quote(k) for k in sorted(kinds)) + ")")
        if db_name:
            parts.append(f"db_name = {_quote(db_name)}")
        return " AND ".join(parts)

    def fts(self, query: str, *, limit: int, kinds: set[str] | None = None,
            db_name: str | None = None) -> list[dict]:
        if not (query or "").strip():
            return []
        try:
            table = self._table()
            if table is None:
                return []
            # `_score` 必须**显式**列进 select：lancedb 0.37.1 现在会自动投影它（每次都打
            # Deprecation warning），官方明说将来不再投影 ⇒ 不显式要，`r.get("_score")` 会变
            # None ⇒ 上报分数静默全 0（召回与排序不受影响：腿内次序由 LanceDB 给、融合只吃名次）。
            q = table.search(query, query_type="fts").select(list(COLS) + ["_score"])
            where = self._where(kinds, db_name)
            if where:
                q = q.where(where)
            rows = q.limit(limit).to_list()
        except Exception as e:  # noqa: BLE001 —— 读侧永不抛（无索引检索会抛 ValueError）
            _log.warning("[retrieval] lance FTS 腿失败 %s: %s", self.dir, e)
            return []
        return [{"id": str(r.get("id") or ""), "score": float(r.get("_score") or 0.0), "row": r}
                for r in rows]

    def vector(self, query: str, *, limit: int, kinds: set[str] | None = None,
               db_name: str | None = None) -> list[dict]:
        # 索引里一条向量都没有 ⇒ 连嵌入超时都不花（与 jsonl 后端同一条优化）
        if not int(self._meta().get("vectors") or 0):
            return []
        qvec = _norm_vec(embedder.embed_one(query))
        if not qvec:
            _log.info("[retrieval] 查询嵌入不可用 ⇒ 本次只用 FTS 腿")
            return []
        try:
            table = self._table()
            if table is None:
                return []
            where = self._where(kinds, db_name, "vector IS NOT NULL")
            # 同 fts 腿：`_distance` 靠自动投影只是**暂时**的（见上面 fts 腿的注释）
            rows = (table.search(qvec).where(where)
                    .select(list(COLS) + ["_distance"]).limit(limit).to_list())
        except Exception as e:  # noqa: BLE001
            _log.warning("[retrieval] lance 向量腿失败 %s: %s", self.dir, e)
            return []
        return [{"id": str(r.get("id") or ""), "score": -float(r.get("_distance") or 0.0), "row": r}
                for r in rows]

    # ── 写侧 ─────────────────────────────────────────────
    def write(self, rows: list[dict], vectors: list[list[float] | None] | None) -> dict:
        """全量重建（overwrite + 建 FTS 索引）。失败返回 `{"ok": False, ...}`，**永不抛**。"""
        if not rows:
            return {"ok": False, "reason": "空语料不落盘（避免写出零条目假索引）"}
        try:
            table = self._create(rows, vectors)
        except Exception as e:  # noqa: BLE001
            _log.warning("[retrieval] lance 建索引失败 %s: %s", self.dir, e)
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        if table is None:  # 维数未知 ⇒ 建不出定长向量列，宁可不落盘也不写半截索引
            return {"ok": False, "reason": "嵌入维数未知（NL2SQL_EMBED_DIM 未配）⇒ 拒绝落盘"}
        self._drop_other_backend_files()
        return {"ok": True, "backend": self.name, "items": len(rows),
                "vectors": sum(1 for v in (vectors or []) if v),
                "lance_version": self._hint()}

    def upsert(self, rows: list[dict], vectors: list[list[float] | None] | None) -> dict:
        """增量（`merge_insert on id`）：给 `store_query` 这类「只新增几条」的路径用。

        维数变了 ⇒ 报 `need_rebuild`（调用方回退全量重建），不许静默丢向量。
        """
        if not rows:
            return {"ok": True, "backend": self.name, "items": 0, "vectors": 0}
        try:
            import lancedb

            table = self._table()
            if table is None:
                return {"ok": False, "reason": "表不存在 ⇒ 需要全量重建", "need_rebuild": True}
            data = self._records(rows, vectors)
            table.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(
                data, on_bad_vectors="null"
            )
            self._optimize(table)
            self._invalidate()
            return {"ok": True, "backend": self.name, "items": len(rows),
                    "vectors": sum(1 for v in (vectors or []) if v),
                    "lance_version": self._hint()}
        except Exception as e:  # noqa: BLE001
            _log.warning("[retrieval] lance 增量写失败 %s: %s", self.dir, e)
            return {"ok": False, "reason": f"{type(e).__name__}: {e}", "need_rebuild": True}

    # ── 内部 ─────────────────────────────────────────────
    def _dim(self, vectors: list[list[float] | None] | None) -> int:
        for vec in vectors or []:
            if vec:
                return len(vec)
        return int(embedder.dim() or 0)

    def _records(self, rows: list[dict], vectors) -> list[dict]:
        out = []
        for pos, row in enumerate(rows):
            vec = _norm_vec(vectors[pos]) if vectors and pos < len(vectors) else None
            rec = {k: str(row.get(k) or "") for k in COLS}
            rec["text_idx"] = _index_text(row.get("title", ""), row.get("text", ""))
            # 逐条可为空（嵌入局部失败/增量只嵌新条目）—— 空向量走 null，检索侧用
            # `vector IS NOT NULL` 过滤；写入必须带 on_bad_vectors="null"（否则 lance
            # 会把 null 当「变长向量」直接抛 ValueError，实测）。
            rec["vector"] = vec
            out.append(rec)
        return out

    def _create(self, rows: list[dict], vectors):
        import lancedb
        import pyarrow as pa
        from lancedb.index import FTS

        dim = self._dim(vectors)
        if dim <= 0:
            return None
        fields = [pa.field("id", pa.string()), pa.field("kind", pa.string()),
                  pa.field("title", pa.string()), pa.field("text", pa.string()),
                  pa.field("text_idx", pa.string()), pa.field("db_name", pa.string()),
                  pa.field("src_path", pa.string()), pa.field("meta_json", pa.string()),
                  pa.field("rev", pa.string()), pa.field("vector", pa.list_(pa.float32(), dim))]
        db = lancedb.connect(str(self.dir))
        table = db.create_table(S.TABLE_NAME, schema=pa.schema(fields),
                                data=self._records(rows, vectors),
                                mode="overwrite", on_bad_vectors="null")
        # base_tokenizer 用 icu 是**精度选择**（按词切）而非「唯一能用」（见 docstring 第 1 条）；
        # stem/停用词/ascii_folding 对中文都只有害处。
        table.create_index("text_idx", config=FTS(
            base_tokenizer="icu", lower_case=True, stem=False,
            remove_stop_words=False, ascii_folding=False))
        self._optimize(table)
        self._invalidate()
        return table

    def _optimize(self, table) -> None:
        """压实 + 清理旧版本（每次写都留版本，不清会一直涨）。

        只清 1 小时前的版本：更近的版本可能正被另一个进程持有的 Table 对象引用。
        """
        try:
            table.optimize(cleanup_older_than=timedelta(hours=1))
        except Exception as e:  # noqa: BLE001 —— 清理失败不影响索引可用
            _log.debug("[retrieval] lance optimize 跳过: %s", e)

    def _drop_other_backend_files(self) -> None:
        """清掉另一个后端的残留（同一目录只留一份索引，避免「哪个才是活的」歧义）。"""
        for name in ("items.jsonl", "vectors.bin", "vectors.idx.json"):
            try:
                (self.dir / name).unlink()
            except OSError:
                pass

    def _invalidate(self) -> None:
        """写完之后必须失效缓存（旧 Table 对象在 overwrite 之后仍返回旧数据，实测）。

        清空即可，不必等提示文件刷新：下一次 `_hint()` 会读到新版本号，缓存自然落空重开
        （写入是同步的，实测 merge_insert 返回后 hint 立刻变为新版本）。
        """
        with _LOCK:
            _TABLE_CACHE.pop(str(self.dir), None)
            _META_CACHE.pop(str(self.dir), None)
