"""检索结果 → 现有工具面的形状适配（方案 §6 的 P1-2 层）。

接线的全部难度都在**形状**与**回落**两件事上，所以它们都集中在这里，`_wren_fast_path`
里只留三行调用。

## 统一契约（调用方依赖它，改动即破坏 R2）

1. **`None` = 检索不可用** —— `off` / 索引缺失 / 索引陈旧 / 后端不可用 / 空问题 / 任何异常
   ⇒ 调用方走**今天**那条分支，逐字一致。
2. **有返回值 = 检索跑过了**，调用方必须直接返回它，**哪怕它是空集**：
   `matches_artifact` 零命中返回 `{"matches": []}`。若这时再回落 MCP，就是白花 120s
   （MCP 侧 `get_context` 会构造 `MemoryStore`、空表探针）。
   （`context_artifact` 是例外：**结构类**零命中返回 `None` ⇒ 回落完整 schema ——
   宁多给不可空给；**知识类** item_type 零命中返回说明性产物，不回落，见其 docstring。）
3. **本模块永不抛**：每个函数自己 try/except + log。调用方**仍要再包一层** —— 现有四处
   最外层 `except: return None` 的语义是「回落 MCP 子进程」，那正是本层要绕开的 120s。

## 刻意的差异（不修，写在这里免得后人当 bug）

- `score`：wren 的 `GrepIndex` 给 int（token 重叠计数），我们给 float（RRF / 名次分）。
  **键名逐字一致、量纲不同** —— 消费方是模型，没有人按类型解析。不 round 成 0 假装一样。
- 不做 `datasource` 过滤：生产 wren 的 `recall_queries(question, limit=3)` **根本没有这个
  入参**，加一层过滤就是行为变更（且索引目录本身已按项目分桶）。

## 索引新鲜度（`_index_fresh`，别省）

本层**没有**自动重建触发器（mdl 更新后索引会旧），而陈旧索引会给出**已改过名的旧表名**
—— 比「没有检索」危险得多。所以 rev 不符一律当「检索不可用」回落全量。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from agent.retrieval import backends
from agent.retrieval import schema as S
from agent.retrieval import search

_log = logging.getLogger(__name__)

#：新鲜度检查的缓存时长。mdl 哈希本身 <5ms，缓存只为省掉每次工具调用的重复哈希。
FRESH_TTL = 30.0
_fresh_cache: dict[str, tuple[float, bool]] = {}   # 按 index_dir 分桶（项目数很少）

#：`item_type` 缺省时搜哪些 kind —— 只要「结构」那几类（知识料另有知识分支，见下）。
_SCHEMA_KINDS = frozenset({
    S.KIND_SCHEMA_TABLE,
    S.KIND_SCHEMA_COLUMN,
    S.KIND_RELATIONSHIP,
    S.KIND_VIEW,
    S.KIND_CUBE,
    S.KIND_MEASURE,
    S.KIND_DIMENSION,
})

# ── 知识出口（get_context 的第三条通道）──────────────────────────────
#：知识类 kind 全集。**判「这是知识请求」的唯一判据 = 解析出的 kinds ⊆ 本集合**。
_KNOWLEDGE_KINDS_ALL = frozenset({
    S.KIND_KNOWLEDGE_RULE,
    S.KIND_KNOWLEDGE_GLOSSARY,
    S.KIND_KNOWLEDGE_METRIC,
    S.KIND_KNOWLEDGE_CAVEAT,
})

#：知识类 item_type → kind。**刻意不复用 indexer._ITEM_TYPE_TO_KIND**：那份是 str→str
# 一对一（结构类），而知识请求要能表达「四者并集」（item_type="knowledge"）。
_ITEM_TYPE_TO_KNOWLEDGE_KINDS = {
    "knowledge": _KNOWLEDGE_KINDS_ALL,
    "rules": frozenset({S.KIND_KNOWLEDGE_RULE}),
    "rule": frozenset({S.KIND_KNOWLEDGE_RULE}),
    "glossary": frozenset({S.KIND_KNOWLEDGE_GLOSSARY}),
    "metrics": frozenset({S.KIND_KNOWLEDGE_METRIC}),
    "metric": frozenset({S.KIND_KNOWLEDGE_METRIC}),
    "caveats": frozenset({S.KIND_KNOWLEDGE_CAVEAT}),
    "caveat": frozenset({S.KIND_KNOWLEDGE_CAVEAT}),
}

#：知识片段的 text 上限。索引按 `## ` 切块、单块 ≤ CHUNK_CHARS(1200)＋重叠余量，
# 默认 TEXT_CAP=600 会把块腰斩（`_hydrate` 截断）⇒ 知识分支必须显式放大。
KNOWLEDGE_TEXT_CAP = 4000

_CONTEXT_NOTE = (
    "策略 retrieval：只返回与问题最相关的 top-k 片段（不是完整 schema），"
    "末尾附全部表名清单。需要某张表的完整定义/全部列请调 describe_model(name)，"
    "需要全量 schema 请调 describe_schema() 或 get_mdl()。"
)

_KNOWLEDGE_NOTE = (
    "策略 retrieval（知识）：返回与问题最相关的业务口径片段。item_type 取值："
    "glossary=术语/枚举、metrics=计算口径、caveats=数据陷阱、rules=规则速查、"
    "knowledge=四者并集。片段是 top-k 节选，需要某领域完整口径请调 get_instructions()。"
)

_KNOWLEDGE_NOTE_EMPTY = (
    "知识检索零命中：没有与该问题相关的业务口径条目（这不是失败，是检索跑过了没找到）。"
    "可换 item_type（glossary/metrics/caveats/rules/knowledge）或换问题关键词重试；"
    "需要全文口径请调 get_instructions()。"
)


def forget_freshness(project: Path) -> None:
    """丢掉某项目的「新鲜度」判定缓存（**刚重建/刚写过索引**后必须调）。**永不抛**。

    为什么必须有：`_index_fresh` 有 30s TTL。索引重建后不丢缓存，进程内会继续沿用
    「陈旧 ⇒ 回落全量」的旧判定 —— 表现为「重建成功了，但 30s 内检索还是不走」，
    属于最难查的那种「看起来没生效」。**失败只影响缓存，不影响索引本身**。
    """
    try:
        _fresh_cache.pop(str(S.index_dir_for(Path(project))), None)
    except Exception as e:  # noqa: BLE001
        _log.warning("[retrieval] 清理新鲜度缓存失败（无害，最多多陈旧 %ss）: %s", FRESH_TTL, e)


def _index_fresh(project: Path, *, ttl: float = FRESH_TTL) -> bool:
    """索引存在、后端就绪、且 rev 与当前 `target/mdl.json` 一致。**永不抛**。"""
    try:
        index_dir = S.index_dir_for(Path(project))
    except Exception as e:  # noqa: BLE001
        _log.warning("[retrieval] 解析索引目录失败，按不可用处理: %s", e)
        return False

    key = str(index_dir)
    now = time.monotonic()
    cached = _fresh_cache.get(key)
    if cached is not None and now - cached[0] < ttl:
        return cached[1]

    ok = False
    try:
        rev = S.project_rev(project)
        meta_rev = str(S.read_meta(index_dir).get("rev") or "")
        ok = bool(rev) and rev == meta_rev and backends.open_backend(index_dir).ready()
        if not ok:
            _log.info(
                "[retrieval] 索引不可用/已陈旧（%s：meta rev=%s ≠ mdl rev=%s），回落全量",
                index_dir, meta_rev or "-", rev or "-",
            )
    except Exception as e:  # noqa: BLE001 —— 检查失败一律按不可用（回落方向 = 今天的全量注入）
        _log.warning("[retrieval] 新鲜度检查失败，按不可用处理: %s", e)
        ok = False

    _fresh_cache[key] = (now, ok)
    return ok


def db_name_for(tool: object, project: Path) -> str:
    """工具的库名：先看注入的 `_wren_db_name`，再查注册表按**项目路径**精确匹配。

    **绝不按目录名拼**（R8）：生产上 `db_config.json` 的 `name` 是 `witops`，而项目目录叫
    `witops_wrenai`，同级还躺着 `witops_wrenai.备份-<ts>`。解析不出返回 `""` —— 调用方
    据此不传库过滤（fail-open：索引目录本身已按项目分桶）。
    """
    name = str(getattr(tool, "_wren_db_name", "") or "").strip()
    if name:
        return name
    try:
        target = os.path.normpath(str(project))
        for entry in S.registered_projects():
            if os.path.normpath(str(entry.get("project") or "")) == target:
                return str(entry.get("db_name") or "")
    except Exception as e:  # noqa: BLE001
        _log.warning("[retrieval] 查注册表取 db_name 失败: %s", e)
    return ""


def _kinds_for_item_type(item_type: str | None) -> set[str]:
    """wren 的 `item_type` 入参 → 本层 kind。认不出按「结构类全搜」（不窄化）。

    先查知识映射（glossary/metrics/caveats/rules/knowledge），再落回结构类映射 ——
    未知 item_type 的行为与改动前**逐字一致**（返回 `_SCHEMA_KINDS`）。
    """
    it = str(item_type or "").strip().lower()
    if it in _ITEM_TYPE_TO_KNOWLEDGE_KINDS:
        return set(_ITEM_TYPE_TO_KNOWLEDGE_KINDS[it])
    if it:
        try:
            from agent.retrieval.indexer import _ITEM_TYPE_TO_KIND   # 同一份映射，不抄第二份

            kinds = [k for t, k in _ITEM_TYPE_TO_KIND.items() if t == it]
            if kinds:
                return set(kinds)
        except Exception as e:  # noqa: BLE001
            _log.debug("[retrieval] item_type 映射不可用: %s", e)
    return set(_SCHEMA_KINDS)


def _is_knowledge_request(kinds: set[str]) -> bool:
    """解析出的 kinds 全是知识类 ⇒ 走知识分支（大 text_cap、零命中不回落 schema）。"""
    return bool(kinds) and kinds <= set(_KNOWLEDGE_KINDS_ALL)


def _render_fragments(hits: list[dict]) -> str:
    """命中 → 可追溯的纯文本片段（每条带 `src_path`，便于模型/人核对来源）。"""
    blocks: list[str] = []
    for hit in hits:
        title = str(hit.get("title") or "").strip()
        src = str(hit.get("src_path") or "").strip()
        head = " ".join(part for part in (title, f"[{src}]" if src else "") if part)
        text = str(hit.get("text") or "").strip()
        blocks.append(f"### {head}\n{text}" if head else text)
    return "\n\n".join(block for block in blocks if block)


def _schema_skeleton(project: Path) -> str:
    """全部表 / 关系 / Cube 的**名字清单**（不展开列）。

    原因：模型看不到完整 schema 时最危险的失败是「**不知道某张表存在**」（于是去查错表或
    直接答不出来）。一份 ~1KB 的名字清单就能堵住这个洞，且与全量注入同源（同一个
    `build_json`，R12）。
    """
    try:
        from wren.context import build_json

        manifest = build_json(Path(project))
    except Exception as e:  # noqa: BLE001 —— 清单是增益，取不到就不附
        _log.debug("[retrieval] 取 schema 清单失败: %s", e)
        return ""
    if not isinstance(manifest, dict):
        return ""

    def names(key: str) -> list[str]:
        return [
            str(x.get("name"))
            for x in (manifest.get(key) or [])
            if isinstance(x, dict) and x.get("name")
        ]

    lines = []
    for label, key in (("表", "models"), ("关系", "relationships"), ("Cube", "cubes"), ("视图", "views")):
        got = names(key)
        if got:
            lines.append(f"{label}: " + "、".join(got))
    return "[全部对象（仅名字）] " + "；".join(lines) if lines else ""


def context_artifact(
    project: Path,
    question: str,
    *,
    limit: int = 5,
    item_type: str | None = None,
    model_name: str | None = None,
    db_name: str | None = None,
    stats_out: dict | None = None,
) -> dict | None:
    """`get_context` 的检索版产物；`None` ⇒ 调用方回落完整 schema（逐字一致）。

    键集与今天的 `full` 产物**完全相同**（`strategy` / `schema` / `note`），下游只读
    `schema`，所以换值不换键。

    `stats_out`（可选 dict）透传给 `search.search`，回填本次检索的运行画像
    （legs/attempted/fused/hits，见 `search._fill_stats`）——**产物本身不加字段**
    （形状是模型消费契约），画像走这条带外通道给 FAST-PATH 日志用。

    **知识分支**（item_type ∈ glossary/metrics/caveats/rules/knowledge）与结构分支的差异：
      - `text_cap` 放大到 `KNOWLEDGE_TEXT_CAP`（默认 600 会把按 `##` 切的知识块腰斩）；
      - **不按 `model_name` 过滤**：知识条目 meta 只有 `{"file","chunk"}`，没有 `model`
        字段，沿用结构类的过滤会把知识命中全灭；
      - **零命中不返回 `None`**：返回说明性产物。「宁多给不可空给」是 *schema* 的约定，
        对知识检索，回落整份 schema 是错误答案（问口径给表结构）；
      - 不追加 `_schema_skeleton`（表名清单对口径问答是纯噪声）。
    """
    query = str(question or "").strip()
    if not query or not _index_fresh(project):
        return None
    if not S.is_enabled():
        # off 模式：search() 会因 is_enabled()=False 返回 []，若不在此拦下，知识分支
        # 会把「开关关了」误判成「跑过零命中」而返回说明性产物 —— 破坏契约 #1
        # （off ⇒ None ⇒ 调用方走今天那条分支，逐字一致）。
        return None

    kinds = _kinds_for_item_type(item_type)
    knowledge = _is_knowledge_request(kinds)
    want = str(model_name or "").strip()
    try:
        hits = search.search_project(
            Path(project),
            query,
            limit=max(1, int(limit or 5)),
            kinds=kinds,
            db_name=(db_name or None),
            stats_out=stats_out,
            **({"text_cap": KNOWLEDGE_TEXT_CAP} if knowledge else {}),
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("[retrieval] get_context 检索失败，回落 full: %s", e)
        return None

    if knowledge:
        if not hits:
            return {
                "strategy": "retrieval",
                "schema": "未检索到与该问题相关的业务口径条目。",
                "note": _KNOWLEDGE_NOTE_EMPTY,
            }
        return {
            "strategy": "retrieval",
            "schema": _render_fragments(hits),
            "note": _KNOWLEDGE_NOTE,
        }

    if want:
        hits = [h for h in hits if str((h.get("meta") or {}).get("model") or "") == want]
    if not hits:
        return None   # 零命中 ⇒ 回落完整 schema（宁多给，不可空给）

    schema = _render_fragments(hits)
    skeleton = _schema_skeleton(project)
    if skeleton:
        schema = f"{schema}\n\n{skeleton}"
    return {"strategy": "retrieval", "schema": schema, "note": _CONTEXT_NOTE}


def matches_artifact(
    project: Path,
    question: str,
    *,
    limit: int = 3,
    db_name: str | None = None,
    stats_out: dict | None = None,
) -> dict | None:
    """`recall_queries` 的检索版产物（`{"matches": [...]}`）；`None` ⇒ 回落 MCP。

    每条**键名与取值都对齐 wren 的 `GrepIndex._pair_to_result`**（`nl_query` / `sql_query`
    / `datasource` / `tags` / `path` / `score`）：消费方是模型，形状漂移会静默改变行为。
    `nl`/`sql` 一律取自 `meta`（原文，不经 `text` 的 600 字截断）。
    零命中返回 `{"matches": []}`（= 检索跑过了），**不是** `None`。
    `stats_out`：同 `context_artifact` —— 运行画像走带外通道，matches 形状一个键不加
    （腿数判据本来只有 score 量纲一条，零命中时连它都没有，画像是唯一观测点）。
    """
    query = str(question or "").strip()
    if not query or not _index_fresh(project):
        return None

    cap = max(1, int(limit or 3))
    try:
        hits = search.search_project(
            Path(project),
            query,
            limit=max(cap * 3, cap),                       # 多取几条，去重后再截
            kinds={S.KIND_EXAMPLE_SQL},
            db_name=(db_name or None),
            stats_out=stats_out,
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("[retrieval] recall_queries 检索失败，回落 MCP: %s", e)
        return None

    matches: list[dict] = []
    seen: set[str] = set()
    for hit in hits:
        meta = hit.get("meta") or {}
        nl, sql = str(meta.get("nl") or ""), str(meta.get("sql") or "")
        if not nl or not sql:
            continue          # `*.sql` 之类没有 nl 的条目不构成范例对
        path = str(meta.get("path") or hit.get("src_path") or "")
        if path in seen:
            continue
        seen.add(path)
        tags = meta.get("tags")
        if isinstance(tags, (list, tuple, set)):
            tags = ",".join(str(t) for t in tags)
        matches.append({
            "nl_query": nl,
            "sql_query": sql,
            "datasource": str(meta.get("datasource") or ""),
            "tags": str(tags or ""),
            "path": path,
            "score": float(hit.get("score") or 0.0),
        })
        if len(matches) >= cap:
            break
    return {"matches": matches}


def refresh_examples(project: Path, db_name: str, md_path: Path) -> dict:
    """`store_query` 写完 markdown 后**显式**刷新索引（方案 §7）。**永不抛**。

    为什么要显式：`upsert_items` 不改 `rev`（rev 只看 mdl），而 `build_index` 的幂等判断
    按 rev 走 —— 不显式推一把，新范例在下一次全量构建时会被「rev 未变」跳过。

    惰性正确：本工具当前在 nl2sql 通道**不可达**（不在 `nl2sql.yaml` 白名单、提示词禁会话内
    回写、FeedbackStore 桥接在 `src/` 下无实现）⇒ 接了也不产生线上效果，只为写路径就位。
    """
    project, md_path = Path(project), Path(md_path)
    if not S.is_enabled():
        return {"ok": True, "skipped": True, "reason": "检索关闭"}
    if not db_name:
        return {"ok": False, "reason": "解析不出 db_name（不落与全量构建 id 不一致的条目）"}
    try:
        from agent.retrieval import indexer

        item = indexer.example_item(project, db_name, md_path)
        if item is None:
            return {"ok": False, "reason": "不是可索引的范例对（frontmatter 缺 nl/sql）"}
        result = indexer.upsert_items(project, db_name, [item])
        if result.get("need_rebuild"):
            result = {**indexer.build_index(project, db_name), "rebuilt": True}
        forget_freshness(project)   # 刚写过 ⇒ 下次重新判新鲜度
        return result
    except Exception as e:  # noqa: BLE001
        _log.warning("[retrieval] 刷新索引失败（忽略，不影响写 markdown）: %s", e)
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
