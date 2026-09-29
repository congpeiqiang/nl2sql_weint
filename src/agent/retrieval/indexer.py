"""语料抽取 + 索引构建（**唯一写入口**）。

## 语料来源（方案 §5.3：kind → 来源）

- `target/mdl.json` → 表 / 列 / 度量 / 视图 / 关系（中文 displayName 与 description 是
  检索命中的主力，必须进 `text`）；
- `knowledge/**` → 知识文件；`*.sql` 归 `example_sql`，其余按子目录名归规则/词表/指标/口径；
- **输入项目只许来自注册表**（`schema.registered_projects`）。扫目录会把
  `<name>.备份-<ts>` 当活库 —— 生产上就有一个 151 models 的备份躺在同级。

## 契约

- **不做增量**：条目数只有几百，全量重建 < 1s。省掉增量带来的「半新半旧」状态。
- **嵌入是纯增益**：`embed_texts` 返回 None 时照样落盘（`vectors.bin` 不写），
  索引照样可用（只有 FTS 腿）—— 这就是 §8.1 的 L1，不是异常路径。
- **空 mdl 不建索引**：`target/mdl.json` 读不到 ⇒ 拒绝构建（否则会写出一个「零条目」
  的假索引，读侧会以为一切正常而永远返回空）。
- 失败的构建**不落盘**、不覆盖旧索引（先收集完再写）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from agent.retrieval import backends
from agent.retrieval import embedder
from agent.retrieval import schema as S

_log = logging.getLogger(__name__)

MAX_FILE_BYTES = 512 * 1024   # 单文件上限：超过就不索引（防一条巨型文档淹没索引）
CHUNK_CHARS = 1200            # markdown 按二级标题切；切完仍超长则按长度硬切
EMBED_BATCH = 32


# ── 中文业务名补充 ──────────────────────────────────────
#：wren 的 item_type → 本层 kind（键必须与 `extract_schema_items` 逐字一致）
_ITEM_TYPE_TO_KIND = {
    "model": S.KIND_SCHEMA_TABLE,
    "column": S.KIND_SCHEMA_COLUMN,
    "relationship": S.KIND_RELATIONSHIP,
    "view": S.KIND_VIEW,
    "cube": S.KIND_CUBE,
    "measure": S.KIND_MEASURE,
    "cube_dimension": S.KIND_DIMENSION,
    "time_dimension": S.KIND_DIMENSION,
}

#：业务名可能挂在这些键上（wren 各版本命名不一，按优先级取第一个非空的）
_ALIAS_KEYS = ("displayName", "display_name", "synonyms", "synonym", "alias", "aliases")


def _aliases_of(obj: dict) -> str:
    """取一个 mdl 对象的中文业务名/同义词，用 `、` 连接（空的返回 ""）。"""
    props = obj.get("properties") if isinstance(obj.get("properties"), dict) else {}
    out: list[str] = []
    for key in _ALIAS_KEYS:
        value = props.get(key)
        if value in (None, "", [], {}):
            value = obj.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple, set)):
            out.extend(str(v) for v in value if str(v).strip())
        else:
            out.append(str(value))
    return "、".join(dict.fromkeys(part.strip() for part in out if part.strip()))


def _table_ref(model: dict) -> str:
    """模型的物理表全名（`catalog.schema.table`）。"""
    ref = model.get("tableReference")
    if isinstance(ref, dict):
        return ".".join(str(ref.get(k)) for k in ("catalog", "schema", "table") if ref.get(k))
    return str(ref or "")


def _extra_map(manifest: dict) -> dict[tuple[str, str, str], dict]:
    """`(item_type, model_name, item_name) → {"alias": 中文名, "lines": [附加检索行]}`。

    ⚠️ 键必须与 `extract_schema_items` 的取法**逐字对齐**，其中两处不直观：
    relationship 的 `model_name` 取的是**左表名**（`models[0]`），cube 的取 `baseObject`。

    为什么要补「物理表」：wren 的 model 文本里**不含 `tableReference`**，而问数时人和模型
    都常直接说物理表名（`t_workhour` / `algorithm_script`）—— 不补就检索不到。
    """
    table: dict[tuple[str, str, str], dict] = {}

    def put(item_type: str, model_name: str, item_name: str, obj: dict, ref: str = "") -> None:
        if not item_name:
            return
        alias = _aliases_of(obj)
        lines = []
        if alias:
            lines.append(f"业务名/同义词: {alias}")
        if ref:
            lines.append(f"物理表: {ref}")
        if alias or ref:
            table[(item_type, model_name, item_name)] = {"alias": alias, "lines": lines}

    for model in manifest.get("models") or []:
        if not isinstance(model, dict):
            continue
        name = str(model.get("name") or "")
        ref = _table_ref(model)
        put("model", name, name, model, ref)
        for col in model.get("columns") or []:
            if isinstance(col, dict):
                put("column", name, str(col.get("name") or ""), col, ref)

    for rel in manifest.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        models = rel.get("models") or []
        left = str(models[0]) if models else "?"
        put("relationship", left, str(rel.get("name") or ""), rel)

    for view in manifest.get("views") or []:
        if isinstance(view, dict):
            put("view", "", str(view.get("name") or ""), view)

    for cube in manifest.get("cubes") or []:
        if not isinstance(cube, dict):
            continue
        cube_name = str(cube.get("name") or "")
        put("cube", str(cube.get("baseObject") or "?"), cube_name, cube)
        for key, item_type in (
            ("measures", "measure"),
            ("dimensions", "cube_dimension"),
            ("timeDimensions", "time_dimension"),
        ):
            for child in cube.get(key) or []:
                if isinstance(child, dict):
                    put(item_type, cube_name, str(child.get("name") or ""), child)
    return table


def _items_from_mdl(project: Path, db_name: str, rev: str) -> list[S.Item]:
    """mdl.json → 条目。**复用 wren 自己的 `extract_schema_items`**（八类全覆盖），
    再补一列中文业务名。

    为什么要复用：手写一遍等于**猜** mdl 形状 —— 实施期用真语料对过，真实 mdl 的
    `models[].properties` / `columns[].properties` 常常**整个不存在**，元数据在
    `cubes[].measures` / `relationships` 里；wren 那个抽取器是同一版本的发信源，
    且与全量注入路径（`describe_schema`）同源 ⇒ 检索语料**定义上**是全量注入的子集。

    那为什么还要补：`_prop_description` 只读 `description`，**丢掉 `displayName`
    （中文业务名）与同义词**，而中文问数的召回几乎全靠它们。
    """
    mdl = Path(project) / "target" / "mdl.json"
    try:
        manifest = json.loads(mdl.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        _log.warning("[retrieval] 读 mdl.json 失败 %s: %s", mdl, e)
        return []
    if not isinstance(manifest, dict):
        return []

    try:
        from wren.memory.schema_indexer import extract_schema_items
    except Exception as e:  # noqa: BLE001 —— wren 版本变了也不能让索引构建炸掉调用方
        _log.warning("[retrieval] wren.memory.schema_indexer 不可用: %s", e)
        return []

    extras = _extra_map(manifest)
    items: list[S.Item] = []
    for record in extract_schema_items(manifest):
        item_type = str(record.get("item_type") or "")
        kind = _ITEM_TYPE_TO_KIND.get(item_type)
        if kind is None:
            continue
        model_name = str(record.get("model_name") or "")
        item_name = str(record.get("item_name") or "")
        if not item_name:
            continue
        extra = extras.get((item_type, model_name, item_name)) or {}
        alias = str(extra.get("alias") or "")
        text = "\n".join([*(extra.get("lines") or []), str(record.get("text") or "")]).strip()
        src = (
            f"mdl:{item_type}:{model_name}.{item_name}"
            if model_name
            else f"mdl:{item_type}:{item_name}"
        )
        items.append(
            S.Item(
                id=S.item_id(db_name, kind, src),
                kind=kind,
                title=alias.split("、")[0] if alias else item_name,
                text=text,
                db_name=db_name,
                src_path=src,
                rev=rev,
                meta={
                    "item_type": item_type,
                    "model": model_name,
                    "name": item_name,
                    "expression": str(record.get("expression") or ""),
                },
            )
        )
    return items


def _chunks(text: str) -> list[str]:
    """按二级标题切；切完仍超长按长度硬切（保留重叠，避免切断关键词）。"""
    blocks, current = [], []
    for line in text.splitlines():
        if line.startswith("## ") and current:
            blocks.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())

    out: list[str] = []
    for block in blocks:
        if not block:
            continue
        if len(block) <= CHUNK_CHARS:
            out.append(block)
            continue
        step = CHUNK_CHARS - 100
        out.extend(block[i : i + CHUNK_CHARS] for i in range(0, len(block), step))
    return out


# ── 范例对（knowledge/sql/*.md）──────────────────────────
#：为什么必须单独抽出来：`recall_queries` 的 matches 要求 `nl_query`/`sql_query` 是**原样**
#：文本，而读侧返回的 `text` 会按 `TEXT_CAP`(600) 截断 —— 多行 SQL 必被砍。故 nl/sql/
#：datasource/tags/path 全部落进 `meta`（meta 不截断），`text` 只留作检索面。


def _example_meta(project: Path, md_path: Path) -> dict | None:
    """`knowledge/sql/<name>.md` → 范例对字段；不是范例对 ⇒ None。

    **必须用 wren 自己的解析器**（与 `GrepIndex` 的 `load_query_pairs` 同一函数）：自己写
    一遍 frontmatter 解析等于猜格式，而这里每个字段都要与 GrepIndex 的 matches 逐字对应。

    `path` 用 `str(md_path.relative_to(project))`（**平台原生分隔符**）—— 逐字照抄 wren
    `load_query_pairs` 的 `str(md.relative_to(project_path))`：换成 `as_posix` 或砍掉
    `sql/` 那一段，`matches` 里的 `path` 就与 GrepIndex 对不上了（`verify_retrieval_wiring`
    ③ 段直接拿两边结果做集合比对）。
    """
    project, md_path = Path(project), Path(md_path)
    try:
        from wren.memory.markdown import parse_query_markdown

        fm = parse_query_markdown(md_path)
    except Exception as e:  # noqa: BLE001 —— 解析器缺失/文件不可读：回落旧行为，绝不抛
        _log.debug("[retrieval] 解析范例对失败 %s: %s", md_path, e)
        return None
    nl, sql = fm.get("nl"), fm.get("sql")
    if not nl or not sql:
        return None
    tags = fm.get("tags")
    if isinstance(tags, (list, tuple, set)):
        tag_list = [str(t) for t in tags if str(t).strip()]
    else:
        tag_list = [str(tags)] if tags else []
    try:
        # 与 `load_query_pairs` 的 `str(md.relative_to(project_path))` 逐字同构
        path = str(md_path.relative_to(project))
    except ValueError:  # 不在项目下（调用方已挡，双保险）
        path = md_path.name
    return {
        "nl": str(nl),
        "sql": str(sql),
        "datasource": str(fm.get("datasource") or ""),
        "tags": tag_list,
        "path": path,
        "source": str(fm.get("source") or "user"),
    }


def example_item(project: Path, db_name: str, md_path: Path) -> S.Item | None:
    """一个 `knowledge/sql/<name>.md` → 一条 `example_sql` 条目（**id 稳定**）。

    **唯一一份「md → Item」的构造**：`collect_items`（全量建索引）与 `store_query` 的增量
    刷新共用它 —— 两边各写一遍的话算出的 id 会不一样，`merge_insert on id` 会插出重复
    条目（同一范例在 RRF 里出现两次）。
    """
    project, md_path = Path(project), Path(md_path)
    try:
        rel = md_path.relative_to(project / "knowledge")
    except ValueError:
        return None
    if len(rel.parts) != 2 or rel.parts[0] != "sql" or md_path.suffix.lower() != ".md":
        return None
    meta = _example_meta(project, md_path)
    if meta is None:
        return None
    src = f"knowledge/{rel}"  # 与 _items_from_knowledge 的拼法逐字同构
    return S.Item(
        id=S.item_id(db_name, S.KIND_EXAMPLE_SQL, src),
        kind=S.KIND_EXAMPLE_SQL,
        title=meta["nl"][:120],  # NL 问句才是召回主信号（旧行为用的是文件 stem）
        text=f"{meta['nl']}\n{meta['sql']}",
        db_name=db_name,
        src_path=src,
        rev=S.project_rev(project),
        meta={**meta, "file": str(rel), "chunk": 0},
    )


def _items_from_knowledge(project: Path, db_name: str, rev: str) -> list[S.Item]:
    """knowledge/ 下的文件 → 知识条目（范例对一文件一条；其余按二级标题分块）。"""
    root = Path(project) / "knowledge"
    if not root.is_dir():
        return []

    items: list[S.Item] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                _log.info("[retrieval] 跳过超限文件 %s", path)
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            _log.warning("[retrieval] 读知识文件失败 %s: %s", path, e)
            continue

        rel = path.relative_to(root)
        sub = rel.parts[0] if len(rel.parts) > 1 else ""

        # ── 范例对优先：knowledge/sql/<name>.md（`store_query` 与 goodcase 回写的落点）──
        # 这些文件过去落到 else 分支被 knowledge_kind("sql") 判成 knowledge_rule **并按 ##
        # 切块**，于是 kind=example_sql 成了死值、`recall_queries` 的 nl/sql 也无法原样还原。
        # 一文件一条、nl/sql 原文进 meta（text 会被读侧 600 字截断）。解析不出 ⇒ 走下面的
        # 旧分支（加法，不静默丢文件）。
        if sub == "sql" and path.suffix.lower() == ".md":
            item = example_item(project, db_name, path)
            if item is not None:
                items.append(item)
                continue

        if path.suffix.lower() == ".sql":
            kind, chunks = S.KIND_EXAMPLE_SQL, [text.strip()]
        else:
            kind, chunks = S.knowledge_kind(sub), _chunks(text)

        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            src = f"knowledge/{rel}" + (f"#{i}" if len(chunks) > 1 else "")
            items.append(
                S.Item(
                    id=S.item_id(db_name, kind, src),
                    kind=kind,
                    title=rel.stem + (f" ({i})" if len(chunks) > 1 else ""),
                    text=chunk,
                    db_name=db_name,
                    src_path=src,
                    rev=rev,
                    meta={"file": str(rel), "chunk": i},
                )
            )
    return items


def collect_items(project: Path, db_name: str) -> list[S.Item]:
    """一个项目的全部语料条目。**只读**：不碰磁盘上已有的索引。"""
    rev = S.project_rev(project)
    return _items_from_mdl(project, db_name, rev) + _items_from_knowledge(project, db_name, rev)


# ── 构建 ────────────────────────────────────────────────
def _embed(items: list[S.Item], *, verbose: bool = False) -> int:
    """给条目填 vector（就地）。返回成功条数；失败 0 条 = 纯 FTS 索引，不是错误。"""
    filled = 0
    for start in range(0, len(items), EMBED_BATCH):
        batch = items[start : start + EMBED_BATCH]
        payloads = [f"{it.title}\n{it.text}".strip()[:2000] for it in batch]
        vectors = embedder.embed_texts(payloads)
        if vectors is None:
            if verbose:
                print(f"  [embed] 第 {start // EMBED_BATCH + 1} 批失败 ⇒ 该批无向量")
            continue
        for it, vec in zip(batch, vectors):
            it.vector = vec
        filled += len(batch)
    return filled


def build_index(
    project: Path,
    db_name: str,
    *,
    force: bool = False,
    with_vectors: bool | None = None,
    verbose: bool = False,
) -> dict:
    """重建一个项目的索引。返回统计 dict，**永不抛**（失败用 `ok=False` 表达）。"""
    project = Path(project)
    rev = S.project_rev(project)
    if not rev:
        return {"ok": False, "reason": "读不到 target/mdl.json（先跑 wren context build）", "project": str(project)}

    index_dir = S.index_dir_for(project)
    backend = backends.open_backend(index_dir)
    meta = S.read_meta(index_dir)
    if not force and meta.get("rev") == rev and backend.ready():
        return {"ok": True, "skipped": True, "reason": "rev 未变", "index_dir": str(index_dir)}

    items = collect_items(project, db_name)
    if not items:
        return {"ok": False, "reason": "语料为空（不落盘，避免写出零条目假索引）", "project": str(project)}

    want_vectors = S.vector_enabled() if with_vectors is None else with_vectors
    filled = _embed(items, verbose=verbose) if want_vectors else 0
    if verbose and filled and filled < len(items):
        print(f"  [embed] 局部成功 {filled}/{len(items)}（其余条目只有 FTS 腿）")

    rows = [it.row() for it in items]
    # 逐条可为空：嵌入挂了也要能落盘（纯 FTS 索引），局部失败不许拖垮全库
    written = backend.write(rows, [it.vector for it in items])
    if not written.get("ok"):
        return {"ok": False, "reason": f"后端写入失败：{written.get('reason')}", "project": str(project)}

    kinds: dict[str, int] = {}
    for it in items:
        kinds[it.kind] = kinds.get(it.kind, 0) + 1
    new_meta = {
        "rev": rev,
        "db_name": db_name,
        "project": str(project),
        "project_name": project.name,
        "backend": backend.name,
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "items": len(items),
        "vectors": filled,
        "embed_model": embedder.model_name() if filled else "",
        # 维数**总是**记（向量为 0 条时也要）：lance 的向量列是定长的，重建时要按它配 schema
        "embed_dim": embedder.dim(),
        "mode": S.retrieval_mode(),
        "kinds": kinds,
    }
    S.write_meta(index_dir, new_meta)
    if verbose:
        print(f"  [build] {project.name}: {len(items)} 条，向量 {new_meta['vectors']}/{len(items)}"
              f"，后端 {backend.name}")
    return {"ok": True, "skipped": False, "index_dir": str(index_dir), **new_meta}


def upsert_items(project: Path, db_name: str, items: list[S.Item], *, verbose: bool = False) -> dict:
    """增量写入（`store_query` 新增示例 SQL / goodcase 回写用）。

    lance 侧走 `merge_insert on id`（**新增行会被既有 FTS 索引自动覆盖**，实测），
    jsonl 侧退化为「读全量 → 合并 → 全量重写」（条目只有几百，代价可忽略）。
    **不改 `rev`**：语料是增量长起来的，mdl 没变 —— 幂等判断在 `build_index` 里按 rev 走，
    这里不参与。
    """
    if not items:
        return {"ok": True, "items": 0, "reason": "无新条目"}
    index_dir = S.index_dir_for(project)
    backend = backends.open_backend(index_dir)
    if not backend.ready():
        return {"ok": False, "reason": "索引不存在 ⇒ 先全量构建", "need_rebuild": True}
    rows = [it.row() for it in items]
    result = backend.upsert(rows, [it.vector for it in items])
    if verbose:
        print(f"  [upsert] {project.name}: {len(items)} 条 → {result}")
    if result.get("ok"):
        meta = S.read_meta(index_dir)
        meta["items"] = int(meta.get("items") or 0) + len(items)
        meta["vectors"] = int(meta.get("vectors") or 0) + int(result.get("vectors") or 0)
        meta["upserted_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        S.write_meta(index_dir, meta)
    return result


def build_all(*, force: bool = False, with_vectors: bool | None = None, verbose: bool = False) -> list[dict]:
    """按注册表重建全部库（**索引输入只许来自注册表**）。"""
    out = []
    for entry in S.registered_projects():
        result = build_index(
            entry["project"], entry["db_name"], force=force, with_vectors=with_vectors, verbose=verbose
        )
        out.append({"db_name": entry["db_name"], **result})
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI：`python -m agent.retrieval.indexer [--all | <库名>] [--force] [--no-vectors]`"""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in args
    with_vectors = None if "--no-vectors" not in args else False
    targets = [a for a in args if not a.startswith("-")]

    entries = S.registered_projects()
    if "--all" in args or not targets:
        picked = entries
    else:
        wanted = {t.lower() for t in targets}
        picked = [
            e
            for e in entries
            if e["db_name"].lower() in wanted or e["project_name"].lower() in wanted
        ]
    if not picked:
        print("没有匹配的库。注册表里有：")
        for e in entries:
            print(f"  - db_name={e['db_name']!r}  dir={e['project_name']!r}")
        return 1

    print(f"模式 = {S.retrieval_mode()}（向量腿 {'开' if with_vectors is not False and S.vector_enabled() else '关'}）")
    bad = 0
    for e in picked:
        result = build_index(
            e["project"], e["db_name"], force=force, with_vectors=with_vectors, verbose=True
        )
        print(f"  {e['db_name']}: ok={result.get('ok')} {result.get('reason', '')}")
        bad += 0 if result.get("ok") else 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
