"""JSONL 持久层：条目 + float32 向量二进制（**`jsonl` 后端的存储原语**）。

## 它在体系里的位置（2026-09-28 起）

默认后端是 **LanceDB**（`backends/lance.py`，用户拍板接受改 Dockerfile + 重建镜像），
理由是**内存曲线**：实测 5000 条中文语料下自写方案的进程占用 = gram 倒排 37MB +
向量（Python list，是 float32 的 8 倍）156MB，20k 条时到 **776MB/进程**；lance 走
on-disk mmap，进程不物化语料。FTS 延迟上自写腿其实更快（18.4ms vs 26.8ms），
所以这不是「谁更快」的选择，是「20k 条时谁不会把内存吃穿」的选择。

本模块保留为 **lancedb 装不出来时的降级路径**（`backends/jsonl.py` 调用它）。
读侧两条路径的返回形状完全一致 —— 形状只在 `search.py` 一处。

## 契约

- **落盘三件**（都在 `<workspace>/.retrieval/<项目名>/`）：
  `items.jsonl`（一行一条，`vector` 字段除外）、`vectors.bin`（按行序的 float32）、
  `index.json`（元数据，不装任何依赖也能读）。
- **写是原子的**：先写 `.tmp` 再 `replace`，避免半截索引被读成「完整索引」。
- **缓存按路径分桶**，绝不用「单槽」——本仓已有教训：拿全局变量当单槽会在多库之间
  互相踩（见 memory `shared-slot-dir-audit`）。
- **读侧永不抛**：索引缺失/损坏一律返回空，调用方回落今天的全量注入（§8.1 的 L2）。
"""
from __future__ import annotations

import json
import logging
import threading
from array import array
from pathlib import Path

from agent.retrieval.schema import META_FILENAME

_log = logging.getLogger(__name__)

ITEMS_FILENAME = "items.jsonl"
VECTORS_FILENAME = "vectors.bin"
VEC_POSITIONS_FILENAME = "vectors.idx.json"   # 有向量的条目在 items.jsonl 里的下标（可稀疏）

# 缓存：{index_dir_str: (items_stamp, items, vectors)}。按路径分桶，不是单槽。
_CACHE: dict[str, tuple[str, list[dict], list[list[float] | None] | None]] = {}
_LOCK = threading.Lock()


def items_path(index_dir: Path) -> Path:
    return Path(index_dir) / ITEMS_FILENAME


def vectors_path(index_dir: Path) -> Path:
    return Path(index_dir) / VECTORS_FILENAME


def _vec_positions_path(index_dir: Path) -> Path:
    return Path(index_dir) / VEC_POSITIONS_FILENAME


def ready(index_dir: Path) -> bool:
    """索引文件是否齐（只判存在，不判新鲜度 —— 新鲜度看 `index.json` 的 rev）。"""
    return items_path(index_dir).is_file()


def stamp(index_dir: Path) -> str:
    """索引文件的版本戳（mtime_ns + size）；读不到返回 ""。

    缓存失效的公共依据 —— `search` 的 n-gram 倒排也按它判新旧（同一次构建只会建一次）。
    """
    try:
        st = items_path(index_dir).stat()
        return f"{int(st.st_mtime_ns)}:{st.st_size}"
    except OSError:
        return ""


def save(
    index_dir: Path,
    rows: list[dict],
    vectors: list[list[float] | None] | None,
) -> None:
    """原子落盘。`rows` 里不带 `vector`（向量单独进 vectors.bin，避免 JSON 体积爆炸）。

    `vectors` 允许**逐条为空**（长度须与 `rows` 相等）：`store_query` 这类增量更新只
    嵌了新增的几条，不能因此把全库已有向量丢掉 —— 空洞位置记在 `vectors.idx.json`。
    """
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    if vectors is not None and len(vectors) != len(rows):
        _log.warning("[retrieval] 向量条数与条目不符（%d/%d）⇒ 丢弃向量", len(vectors), len(rows))
        vectors = None

    payload = []
    for row in rows:
        clean = {k: v for k, v in row.items() if k != "vector"}
        payload.append(json.dumps(clean, ensure_ascii=False))

    tmp = items_path(index_dir).with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(payload) + ("\n" if payload else ""), encoding="utf-8")
    tmp.replace(items_path(index_dir))

    flat = array("f")
    positions: list[int] = []
    for pos, vec in enumerate(vectors or []):
        if vec:
            flat.extend(vec)
            positions.append(pos)

    if positions:
        vtmp = vectors_path(index_dir).with_suffix(".bin.tmp")
        with open(vtmp, "wb") as fh:
            flat.tofile(fh)
        vtmp.replace(vectors_path(index_dir))
        tmp_idx = _vec_positions_path(index_dir).with_suffix(".json.tmp")
        tmp_idx.write_text(json.dumps(positions), encoding="utf-8")
        tmp_idx.replace(_vec_positions_path(index_dir))
    else:
        for path in (vectors_path(index_dir), _vec_positions_path(index_dir)):
            try:
                path.unlink()
            except OSError:
                pass

    with _LOCK:
        _CACHE.pop(str(index_dir), None)


def load(index_dir: Path) -> tuple[list[dict], list[list[float] | None] | None]:
    """读条目（＋可选向量，逐条可为 `None`）。任何异常都返回 `([], None)`。"""
    index_dir = Path(index_dir)
    stamp_value = stamp(index_dir)
    if not stamp_value:
        return [], None

    with _LOCK:
        cached = _CACHE.get(str(index_dir))
        if cached and cached[0] == stamp_value:
            return cached[1], cached[2]

    try:
        rows = [
            json.loads(line)
            for line in items_path(index_dir).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception as e:  # noqa: BLE001
        _log.warning("[retrieval] 读索引失败 %s: %s", index_dir, e)
        return [], None

    vectors: list[list[float] | None] | None = None
    vpath = vectors_path(index_dir)
    if vpath.is_file() and rows:
        try:
            flat = array("f")
            with open(vpath, "rb") as fh:
                flat.fromfile(fh, vpath.stat().st_size // flat.itemsize)
            try:
                positions = json.loads(_vec_positions_path(index_dir).read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 —— 无侧车文件 ⇒ 按「全部都有」的旧格式读
                positions = list(range(len(rows)))
            count = len(positions)
            if count and len(flat) % count == 0:
                dim = len(flat) // count
                vectors = [None] * len(rows)
                for slot, pos in enumerate(positions):
                    if 0 <= pos < len(rows):
                        vectors[pos] = list(flat[slot * dim : (slot + 1) * dim])
        except Exception as e:  # noqa: BLE001
            _log.warning("[retrieval] 读向量失败 %s: %s", index_dir, e)
            vectors = None

    if vectors is not None and len(vectors) != len(rows):
        _log.warning("[retrieval] 向量条数与条目不符 ⇒ 放弃向量腿")
        vectors = None

    with _LOCK:
        _CACHE[str(index_dir)] = (stamp_value, rows, vectors)
    return rows, vectors


def stats(index_dir: Path) -> dict:
    """给 verify/运维看的一眼状态（不读全量索引）。"""
    index_dir = Path(index_dir)
    meta = {}
    try:
        meta = json.loads((index_dir / META_FILENAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    size = 0
    for name in (ITEMS_FILENAME, VECTORS_FILENAME):
        try:
            size += (index_dir / name).stat().st_size
        except OSError:
            pass
    return {
        "index_dir": str(index_dir),
        "ready": ready(index_dir),
        "bytes": size,
        "rev": meta.get("rev", ""),
        "items": meta.get("items", 0),
        "vectors": meta.get("vectors", 0),
        "built_at": meta.get("built_at", ""),
        "kinds": meta.get("kinds", {}),
    }
