# -*- coding: utf-8 -*-
"""P1-2 接线验收：读侧三件（`get_context` / `recall_queries` / 知识料裁剪）+ `store_query` 刷新。

**为什么要这份脚本**：检索层自己的正确性已由 `verify_retrieval.py` 覆盖；这一层要证的
是**接线**——即「有没有接上」与「关掉时是否逐字等于今天」。两件事都极易假通过：

1. `off`（默认）下必须**逐字**等于旧行为。断言不能写成「差不多」——`get_context` 的
   artifact 是三个键的 dict，`recall_queries` 的短路是 `{"matches": []}`，`store_query`
   的返回值是 `{"path": …}`，任何一个键/值飘了都算回归，所以这里做**逐字相等**。
2. 「有返回值」与「返回 None」的语义不同：`matches_artifact` 零命中返回 `{"matches": []}`
   **不是** `None`（回落 MCP 就是白花 120s）；`context_artifact` 零命中返回 `None`
   （宁多给不可空给）。这两个方向反过来都是静默事故，各配负对照。
3. 索引**陈旧**（mdl 改过）必须当「不可用」——旧索引会给出已改名的旧表名，比没有检索
   危险得多。故 ④ 段专门改 `mdl.json` 制造 rev 不符。
4. 中间件裁剪是**可证明的原文子集重排**：裁出来的每一片都必须能在原文里逐字找到、
   保持原序、且不比原文长。闸门本身（重建==原文）也要有负对照。

九段，**每段带负对照**；`AGENT_DATA_ROOT` 先钉 `tempfile.mkdtemp` 再 import 业务模块
（数据落点全由它推导，钉晚了就会往真工作区写）。全程 `WREN_MEMORY_BACKEND=grep`——
那是 `_wren_fast_path` 四道门的公共前置条件，不设的话测的是空气。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_retrieval_wiring.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

import yaml

# 本脚本建真图（⑨ 的 config 探针）⇒ langchain 会尝试把 trace 发到 LangSmith（外网）。
# 离线验证脚本不许有出网副作用。
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING"] = "false"

# ⚠️ 必须在 import 业务模块之前：索引/工作区落点由 AGENT_DATA_ROOT 推导
_REPO = pathlib.Path(__file__).resolve().parents[1]
_WORK = pathlib.Path(tempfile.mkdtemp(prefix="nl2sql-verify-wiring-"))
os.environ["AGENT_DATA_ROOT"] = str(_WORK)
os.environ["WREN_MEMORY_BACKEND"] = "grep"        # fast-path 四道门的公共前置条件
os.environ["NL2SQL_EMBED_BASE_URL"] = "http://127.0.0.1:9/v1"   # 嵌入通道钉死 ⇒ 纯 FTS
sys.path.insert(0, str(_REPO / "src"))

results: list[tuple[bool, str, str]] = []
_scene = ["（未分组）"]


def section(title: str) -> None:
    _scene[0] = title
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


def check(cond, label: str, extra: str = "") -> bool:
    results.append((bool(cond), label, _scene[0]))
    tail = f"  | {extra}" if extra else ""
    print(f"  {'[OK]  ' if cond else '[FAIL]'} {label}{tail}")
    return bool(cond)


def set_mode(mode: str | None) -> None:
    """切换检索开关（`None` ⇒ 恢复默认＝完全不设该 env）。"""
    if mode is None:
        os.environ.pop("NL2SQL_RETRIEVAL", None)
    else:
        os.environ["NL2SQL_RETRIEVAL"] = mode


from agent.retrieval import adapters, indexer, schema as S, search  # noqa: E402
from agent.retrieval import backends  # noqa: E402
import agent.workspace_manager as _wm_mod  # noqa: E402
from agent.utils import path_resolver  # noqa: E402
from agent.middlewares import knowledge_trim as kt  # noqa: E402
from agent.middlewares.knowledge_trim import KnowledgeTrimMiddleware  # noqa: E402
from langchain_core.messages import HumanMessage, ToolMessage  # noqa: E402

# ══════════════════════════════════════════════════════════
# 夹具：一个真的（很小的）wren 项目
# ══════════════════════════════════════════════════════════
MDL = {
    "models": [
        {
            "name": "workhour",
            "tableReference": {"catalog": "dw", "schema": "hr", "table": "t_workhour"},
            "properties": {"displayName": "工时表", "description": "员工每月工时记录"},
            "columns": [
                {"name": "emp_id", "type": "INTEGER", "properties": {"displayName": "员工编号"}},
                {
                    "name": "work_hour",
                    "type": "DOUBLE",
                    "properties": {"displayName": "工时", "description": "当月合计工时"},
                },
            ],
            "metrics": [
                {"name": "total_hours", "description": "总工时", "expression": "SUM(work_hour)"}
            ],
        },
        {
            "name": "dept",
            "tableReference": {"catalog": "dw", "schema": "hr", "table": "t_dept"},
            "properties": {"displayName": "部门表"},
            "columns": [
                {"name": "dept_name", "type": "VARCHAR", "properties": {"displayName": "部门名称"}}
            ],
        },
    ]
    + [
        # 填充表：让「全量 schema」有足够体积，②段的「检索版更小」才是个真命题
        {
            "name": f"biz_{i:02d}",
            "tableReference": {"catalog": "dw", "schema": "biz", "table": f"t_biz_{i:02d}"},
            "properties": {"displayName": f"业务表{i:02d}", "description": f"第{i:02d}类业务明细记录"},
            "columns": [
                {"name": f"col_{j}", "type": "VARCHAR", "properties": {"displayName": f"业务字段{j}"}}
                for j in range(6)
            ],
        }
        for i in range(1, 11)
    ],
    "views": [
        {
            "name": "dept_summary",
            "statement": "SELECT dept_name, COUNT(*) FROM t_dept GROUP BY 1",
            "properties": {"displayName": "部门汇总视图"},
        }
    ],
    "relationships": [
        {
            "name": "emp_dept",
            "models": ["workhour", "dept"],
            "joinType": "many_to_one",
            "condition": "workhour.emp_id = dept.emp_id",
            "properties": {"displayName": "员工所属部门"},
        }
    ],
    "cubes": [
        {
            "name": "workhour_cube",
            "baseObject": "workhour",
            "properties": {"displayName": "工时立方"},
            "measures": [
                {"name": "total_hours", "expression": "SUM(work_hour)",
                 "properties": {"displayName": "总工时"}}
            ],
            "dimensions": [{"name": "emp_dim", "properties": {"displayName": "员工维度"}}],
            "timeDimensions": [{"name": "month_td", "properties": {"displayName": "月份"}}],
        }
    ],
}

# 三份口径各异、词表互不重叠的规则文件（免得 FTS 串门导致断言靠运气）
_RULES = {
    "报工口径.md": "## 加班工时\n\n加班工时以报工单上审批通过的加班工时为准，单独统计。\n",
    "客户口径.md": "## 客户归属\n\n客户以合同签约主体为准，渠道商不算客户。\n",
    "库存口径.md": "## 仓库盘点\n\n仓库盘点以月末台账为准，在途物资不计入。\n",
    "财务口径.md": "## 回款确认\n\n回款以银行流水到账日为准，开票不等于回款。\n",
}

# 两份范例对：B 的 SQL 故意 **>600 字符且多行** —— 用来证明 `matches.sql_query` 走的是
# `meta`（不截断）而不是 `text`（TEXT_CAP=600 必砍）。
_EXAMPLE_A = """---
nl: 查询每个员工的当月工时合计
sql: |
  SELECT emp_id, SUM(work_hour) AS total_hours
  FROM hr.t_workhour
  GROUP BY emp_id
datasource: postgres
tags:
  - 工时
  - 报工
---
"""
_EXAMPLE_B_SQL = "\n".join(
    f"  SELECT work_hour + {i} AS w{i}, '第{i}档加班工时口径说明（占位以撑过 600 字符截断线）' AS note"
    for i in range(12)
)
_EXAMPLE_B = f"""---
nl: 查询加班工时明细与分档说明
sql: |
{_EXAMPLE_B_SQL}
  FROM hr.t_workhour
datasource: postgres
tags:
  - 加班
---
"""


def _snake(key: str) -> str:
    out: list[str] = []
    for ch in key:
        out.append("_" + ch.lower() if ch.isupper() else ch)
    return "".join(out)


def _source_keys(obj):
    """camelCase 清单 → YAML 源码口径（`properties` 子树原样保留）。"""
    if isinstance(obj, dict):
        return {k: (v if k == "properties" else _source_keys(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_source_keys(x) for x in obj]
    return obj


def write_yaml_sources(root: pathlib.Path, mdl: dict) -> None:
    """把清单同时落成 **YAML 源码**。

    为什么非要这份：`describe_schema(build_json(project))` 读的是项目 **YAML 源码**
    （`models/*.yml` / `views.yml` / `relationships.yml` / `cubes/*.yml`，schema_version=1），
    **不是** `target/mdl.json`——只写 mdl.json 的话「全量 schema」是空串，②段的体积对照
    会变成 `0 → N`，看着通过其实什么都没测到。
    """
    src = _source_keys(copy.deepcopy(mdl))
    models_dir = root / "models"
    for m in src.get("models") or []:
        models_dir.mkdir(parents=True, exist_ok=True)
        (models_dir / f"{m['name']}.yml").write_text(
            yaml.safe_dump(m, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    for key, filename in (("views", "views.yml"), ("relationships", "relationships.yml")):
        rows = src.get(key) or []
        if rows:
            (root / filename).write_text(
                yaml.safe_dump({key: rows}, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
    (root / "cubes").mkdir(parents=True, exist_ok=True)
    for c in src.get("cubes") or []:
        (root / "cubes" / f"{c['name']}.yml").write_text(
            yaml.safe_dump(c, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )


def make_project(
    root: pathlib.Path,
    *,
    rules: dict[str, str] | None = None,
    examples: dict[str, str] | None = None,
    mdl: dict | None = MDL,
    tag: str = "live",
) -> pathlib.Path:
    (root / "target").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "rules").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "sql").mkdir(parents=True, exist_ok=True)
    (root / "wren_project.yml").write_text("name: demo\n", encoding="utf-8")
    if mdl is not None:
        payload = json.loads(json.dumps(mdl))
        if tag != "live":  # 备份目录塞一条独有表名，用来验「没被索引」
            payload["models"].append(
                {"name": "BACKUP_ONLY_TABLE", "properties": {"displayName": "只在备份里"},
                 "columns": []}
            )
        (root / "target" / "mdl.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        write_yaml_sources(root, payload)
    for name, text in (rules if rules is not None else _RULES).items():
        (root / "knowledge" / "rules" / name).write_text(text, encoding="utf-8")
    for name, text in (examples or {}).items():
        (root / "knowledge" / "sql" / name).write_text(text, encoding="utf-8")
    return root


def _big_rules() -> dict[str, str]:
    """4 份各 ~2.2KB 的规则（原文 >8000 字符）⇒ 默认地板（3 份 / 8000//2 字符）会生效。

    措辞刻意**不含**别的主题词，否则 FTS 会把几份一起召回、地板断言就测不出东西。
    """
    topics = {
        "报工口径.md": "加班工时",
        "客户口径.md": "客户合同",
        "库存口径.md": "仓库台账",
        "财务口径.md": "回款发票",
    }
    out: dict[str, str] = {}
    for name, topic in topics.items():
        filler = (f"本季度{topic}口径以系统台账为准，人工登记不作为依据。" * 90)
        out[name] = f"## {topic}\n\n{topic}的认定与统计见下表。\n\n{filler}\n"
    return out


class _FakeWM:
    """只提供 schema 需要的三个属性（真 manager 会顺带拉起别的东西）。"""

    def __init__(self, ws: pathlib.Path) -> None:
        self.active_workspace = ws
        self.db_config_path = ws / "db_config.json"
        self.semantic_dir = ws


def write_db_config(ws: pathlib.Path, proj: pathlib.Path) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "db_config.json").write_text(
        json.dumps(
            {"databases": [{"name": "demo", "db_type": "postgres",
                            "wren_project": str(proj), "status": "active"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def tool(name: str, proj: pathlib.Path, db: str | None = "demo") -> SimpleNamespace:
    """假 wren 工具：名字用生产的 `wrenai_<库名>_` 前缀（后缀匹配是接线前提）。"""
    obj = SimpleNamespace(name=f"wrenai_demo_{name}", _wren_project_path=str(proj))
    if db is not None:
        obj._wren_db_name = db
    return obj


def fast(t: SimpleNamespace, kwargs: dict | None = None):
    return path_resolver._wren_fast_path(t, kwargs or {})


def artifact_of(result) -> dict:
    content, art = result
    assert content == json.dumps(art, ensure_ascii=False), "content 与 artifact 不是同一份"
    return art


# ══════════════════════════════════════════════════════════
def run_r1_gate(ws: pathlib.Path, proj: pathlib.Path) -> None:
    """⓿ R1：口径**核验**绝不能被检索窄化（结构性判据，子进程真跑）。"""
    section("⓿ R1 结构性闸：核验侧不依赖检索层")
    code = (
        "import sys\n"
        "import agent.utils.caliber_evidence as ce\n"
        "assert 'agent.retrieval' not in sys.modules, '核验模块把检索层拖进来了'\n"
        "print('r1-ok')\n"
    )
    env = {**os.environ, "PYTHONPATH": str(_REPO / "src"), "AGENT_DATA_ROOT": str(_WORK)}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    check(proc.returncode == 0 and "r1-ok" in proc.stdout,
          "import 核验模块不会 import agent.retrieval（子进程真跑）",
          (proc.stderr or "").strip()[-200:])

    # 语料侧仍读**磁盘全部**文件（把项目解析钉到夹具，不碰生产路径）
    import agent.utils.caliber_evidence as ce
    import agent.utils.wren_call_extract as wce

    with mock.patch.object(wce, "resolve_wren_ctx_by_db", lambda db: (proj, None)):
        ce._CORPUS_CACHE.clear()
        files = ce.load_knowledge_corpus("demo")
        names = sorted(f.rel_path.replace("\\", "/") for f in files)
        on_disk = sorted(f"{sub}/{p.name}" for sub in ("glossary", "metrics", "rules", "sql", "caveats")
                         for p in (proj / "knowledge" / sub).glob("*.md"))
        check(names == on_disk and len(names) >= 4,
              "load_knowledge_corpus 仍返回磁盘全部文件（检索不参与核验）",
              f"{len(names)} 个：{names}")


def run_off_identical(ws: pathlib.Path, proj: pathlib.Path, bare: pathlib.Path) -> None:
    """① `off`（默认）＝旧行为逐字不变（R2），四个 fast-path 分支全覆盖。"""
    section("① off ⇒ 旧行为逐字一致（四个 fast-path 分支）")
    set_mode(None)
    check(S.retrieval_mode() == S.MODE_OFF and not S.is_enabled(), "默认（不设 env）＝ off")
    check(path_resolver._retrieval_adapters() is None, "off ⇒ 适配层连 import 都不做")

    # (1) get_context：artifact 与手算的 full 产物逐字相等
    from wren.context import build_json
    from wren.memory.schema_indexer import describe_schema

    expected = {
        "strategy": "full",
        "schema": describe_schema(build_json(proj)),
        "note": "WREN_MEMORY_BACKEND=grep: full schema returned.",
    }
    res = fast(tool("get_context", proj), {"question": "员工每月工时记录"})
    check(res is not None and artifact_of(res) == expected,
          "get_context artifact 与老代码逐字相等（含 note 文案）",
          f"{len(expected['schema'])} chars")
    check(res[0] == json.dumps(expected, ensure_ascii=False),
          "get_context content == json.dumps(artifact, ensure_ascii=False)")

    # (2) recall_queries：无范例对 ⇒ {"matches": []}；有 ⇒ None（回落 MCP）
    res = fast(tool("recall_queries", bare), {"question": "工时"})
    check(res is not None and artifact_of(res) == {"matches": []},
          "recall_queries 无 knowledge/sql ⇒ {\"matches\": []}（与老代码一致）")
    (bare / "knowledge" / "sql" / "临时.md").write_text(_EXAMPLE_A, encoding="utf-8")
    check(fast(tool("recall_queries", bare), {"question": "工时"}) is None,
          "recall_queries 有范例对 ⇒ None（交回 MCP，off 下不越权给结果）")

    # (3) list_stored_queries：有意不接检索层 ⇒ 输出仍是手工推的同一份
    from wren.memory.markdown import load_query_pairs

    pairs = load_query_pairs(bare)
    exp_queries = [
        {"nl_query": p["nl"], "sql_query": p["sql"], "datasource": p.get("datasource", ""),
         "tags": p.get("tags", ""), "source": p.get("source", "user"), "path": p.get("path")}
        for p in pairs
    ]
    art = artifact_of(fast(tool("list_stored_queries", bare), {}))
    check(art == {"queries": exp_queries},
          "list_stored_queries 输出逐字不变（枚举语义，检索不介入）",
          f"{len(exp_queries)} 条")

    # (4) store_query：返回值恒 {"path": …}，且 off 下不产生任何索引副作用
    idx_dir = S.index_dir_for(bare)
    if idx_dir.exists():
        shutil.rmtree(idx_dir)
    art = artifact_of(fast(tool("store_query", bare),
                           {"nl_query": "查询工时", "sql_query": "SELECT 1", "tags": "a,b"}))
    check(set(art) == {"path"} and pathlib.Path(art["path"]).is_file(),
          "store_query 返回 {\"path\": …}（MCP 契约形状未污染）", str(art["path"]))
    check(not idx_dir.exists(), "off ⇒ store_query 不落任何索引目录（零副作用）")


def run_fts_schema(ws: pathlib.Path, proj: pathlib.Path) -> None:
    """② `fts` 下 get_context 走检索版：键集不变、字节数下降、逃生口在。"""
    section("② fts ⇒ get_context 检索版（键集不变 / 体积下降 / 逃生口）")
    set_mode("fts")
    check(S.retrieval_mode() == S.MODE_FTS and S.is_enabled(), "开关读到 fts")

    built = indexer.build_index(proj, "demo", force=True, with_vectors=False)
    check(built.get("ok") and not built.get("skipped"),
          "建索引成功（--no-vectors 等价）", str(built)[:120])

    # 先量旧分支的体积做对照
    set_mode(None)
    full_chars = len(artifact_of(fast(tool("get_context", proj), {"question": "员工每月工时记录"}))["schema"])
    set_mode("fts")
    art = fast(tool("get_context", proj), {"question": "员工每月工时记录"})
    check(art is not None, "命中 ⇒ 走检索版（不是 None）")
    art = artifact_of(art)
    check(set(art) == {"strategy", "schema", "note"},
          "键集与 full 产物完全相同（strategy/schema/note）", str(sorted(art)))
    check(art["strategy"] == "retrieval", "strategy == retrieval")
    got = len(art["schema"])
    print(f"        full {full_chars} chars → retrieval {got} chars"
          f"（{got / max(1, full_chars):.1%}）")
    check(got < full_chars, "检索版比全量注入小（生产 278KB → 目标 KB 级）")
    check("workhour" in art["schema"] and "dept_summary" in art["schema"],
          "尾部附「全部对象（仅名字）」清单（堵「不知道某张表存在」）")
    check("describe_model" in art["note"] and "describe_schema" in art["note"],
          "note 里写死逃生口 describe_model / describe_schema")

    # 负对照：生僻问题 ⇒ None ⇒ 走 full
    check(adapters.context_artifact(proj, "qzxwvjk") is None,
          "★ 负对照：零命中 ⇒ None（宁多给不可空给）")
    fb = artifact_of(fast(tool("get_context", proj), {"question": "qzxwvjk"}))
    check(fb["strategy"] == "full", "★ 零命中时 fast-path 仍回落 full schema")

    # item_type / model_name 窄化不炸（生产工具有这两个入参）
    narrowed = adapters.context_artifact(proj, "员工每月工时记录", item_type="model", model_name="workhour")
    check(narrowed is None or narrowed["strategy"] == "retrieval",
          "item_type/model_name 入参被接受（不抛、不误伤）")


def run_matches_shape(ws: pathlib.Path, proj: pathlib.Path) -> None:
    """③ matches 键名逐字对齐 GrepIndex，且 nl/sql 走 meta 原文（不截断）。"""
    section("③ recall_queries 检索版：键名逐字 / SQL 不被 600 字砍")
    set_mode("fts")
    for name, text in (("工时示例.md", _EXAMPLE_A), ("加班示例.md", _EXAMPLE_B)):
        (proj / "knowledge" / "sql" / name).write_text(text, encoding="utf-8")
    indexer.build_index(proj, "demo", force=True, with_vectors=False)

    q = "work_hour"
    art = fast(tool("recall_queries", proj), {"question": q})
    check(art is not None, "索引新鲜 ⇒ 检索版给结果（不是 None）")
    matches = artifact_of(art)["matches"]
    check(len(matches) >= 1, f"召回 {len(matches)} 条")
    if not matches:
        return

    keys = {frozenset(m) for m in matches}
    check(keys == {frozenset({"nl_query", "sql_query", "datasource", "tags", "path", "score"})},
          "每条键集恰为 {nl_query,sql_query,datasource,tags,path,score}", str(keys))
    check(all(isinstance(m["tags"], str) for m in matches), "tags 是 str（不是 list）")
    check(all(isinstance(m["score"], (int, float)) for m in matches), "score 是数值")

    disk_b = proj / "knowledge" / "sql" / "加班示例.md"
    from wren.memory.markdown import parse_query_markdown

    want_b = str(parse_query_markdown(disk_b)["sql"])
    hit_b = next((m for m in matches if m["path"] == _pair_path(proj, "加班示例.md")), None)
    if check(hit_b is not None, "长 SQL 那条被召回（负对照才有意义）"):
        check(len(want_b) > 600 and "\n" in want_b,
              "夹具的长 SQL 确实 >600 字符且多行", f"{len(want_b)} chars")
        check(hit_b["sql_query"] == want_b and "\n" in hit_b["sql_query"],
              "★ sql_query == 原文（含换行、>600 字符）——走的是 meta 不是 text",
              f"{len(hit_b['sql_query'])} chars")
        # 同一份语料在 search 面上确实被 TEXT_CAP 砍了（证明 meta 这条腿是必要的）
        raw = search.search_project(proj, q, limit=5, kinds={S.KIND_EXAMPLE_SQL})
        capped = [h for h in raw
                  if str((h.get("meta") or {}).get("sql") or "").startswith("SELECT work_hour + 0")]
        check(bool(capped) and len(capped[0]["text"]) <= 601 and "…" in capped[0]["text"],
              "对照：search 的 text 确实按 TEXT_CAP=600 截断",
              str([len(h["text"]) for h in capped]))

    # 负对照：与 wren 自己的 GrepIndex 比（同语料、同问题）——集合必须相等
    from wren.memory.index_backend import GrepIndex

    theirs = GrepIndex(proj).search(q, limit=3)
    mine = {(m["nl_query"], m["sql_query"], m["path"]) for m in matches}
    theirs_set = {(t["nl_query"], t["sql_query"], t["path"]) for t in theirs}
    check(theirs_set and mine == theirs_set,
          "★ 与 GrepIndex.search() 的 (nl_query,sql_query,path) 集合相等",
          f"ours={len(mine)} theirs={len(theirs_set)}")

    # 负对照：off ⇒ None（一个字都不给，交回老路径）
    set_mode("off")
    check(fast(tool("recall_queries", proj), {"question": q}) is None, "★ 负对照：off ⇒ None")
    set_mode("fts")


def _pair_path(proj: pathlib.Path, name: str) -> str:
    """`load_query_pairs` 的 path 口径（平台原生分隔符）。"""
    return str((proj / "knowledge" / "sql" / name).relative_to(proj))


def run_fallback_negatives(ws: pathlib.Path, proj: pathlib.Path) -> None:
    """④ 索引不可用/陈旧的四种形态都必须回落（陈旧比没有更危险）。"""
    section("④ 回落负对照：缺索引 / 坏 meta / mdl 改过 / 空问题")
    set_mode("fts")
    idx_dir = S.index_dir_for(proj)
    q = "员工每月工时记录"

    def fresh_probe() -> bool:
        adapters._fresh_cache.clear()          # 新鲜度带 30s TTL 缓存，负对照必须清
        return adapters.context_artifact(proj, q) is not None

    check(fresh_probe(), "基线：索引新鲜 ⇒ 有结果")

    shutil.rmtree(idx_dir)
    check(not fresh_probe(), "★ 索引目录被删 ⇒ 回落（None）")
    check(indexer.build_index(proj, "demo", force=True, with_vectors=False).get("ok"), "重建索引")

    meta_file = S.meta_path(idx_dir)
    saved = meta_file.read_text(encoding="utf-8")
    meta_file.write_text("{ 这不是 json", encoding="utf-8")
    check(not fresh_probe(), "★ index.json 写坏 ⇒ 按不可用处理")
    meta_file.write_text(saved, encoding="utf-8")

    mdl_file = proj / "target" / "mdl.json"
    mdl_saved = mdl_file.read_text(encoding="utf-8")
    payload = json.loads(mdl_saved)
    payload["models"][0]["properties"]["displayName"] = "工时表（改名后）"
    mdl_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    check(not fresh_probe(), "★ mdl.json 改过（rev 不符）⇒ 陈旧索引一律不用")
    mdl_file.write_text(mdl_saved, encoding="utf-8")
    check(fresh_probe(), "mdl 还原 ⇒ 索引重新被视为新鲜（rev 相等）")

    check(adapters.context_artifact(proj, "") is None, "空问题 ⇒ None")
    check(adapters.matches_artifact(proj, "   ") is None, "空问题 ⇒ matches 也 None")
    set_mode("off")
    check(adapters.context_artifact(proj, q) is None, "off ⇒ None（开关是最外层判据）")
    set_mode("fts")


def run_middleware(ws: pathlib.Path, big: pathlib.Path) -> None:
    """⑤ 知识料裁剪中间件：可证明的原文子集重排 + 六条负对照 + 接线闸。"""
    section("⑤ KnowledgeTrimMiddleware：子集重排 / 地板 / 负对照")
    set_mode("fts")
    indexer.build_index(big, "demo", force=True, with_vectors=False)

    from wren.context import load_rules

    raw, used_legacy = load_rules(big)
    check(raw is not None and len(raw) > 8000, f"夹具原文 {len(raw or '')} chars（>8000 才够触发）")
    parts = kt.rules_parts(big)
    check("\n\n".join(t for _, t in parts) == raw,
          "wren load_rules 的拼接 == 本中间件的重建口径（闸 1 的同构前提）")

    question = "加班工时怎么算"
    msg = ToolMessage(
        content=json.dumps({"instructions": raw, "used_legacy": used_legacy}, ensure_ascii=False),
        name="wrenai_demo_get_instructions", tool_call_id="tc-trim",
    )
    state = {"messages": [HumanMessage(content=question)]}
    req = SimpleNamespace(tool=tool("get_instructions", big), state=state)

    mw = KnowledgeTrimMiddleware()      # 默认档：地板 3 份 ⇒ 4 份里恰好省下 1 份
    out = mw._process_sync(msg, req)
    payload = json.loads(out.content)
    trimmed = payload["instructions"]
    check(trimmed != raw and len(trimmed) < len(raw),
          "默认档（地板 3 份）仍裁掉 1 份", f"{len(raw)} → {len(trimmed)}")
    check(set(payload) == {"instructions", "used_legacy"},
          "键集不变（只换 instructions）", str(sorted(payload)))
    check(payload["used_legacy"] == used_legacy, "used_legacy 原样带回")
    check(getattr(out, "tool_call_id", None) == "tc-trim" and out.name == msg.name,
          "重建后 tool_call_id / name 保留（model_copy 不丢字段）")

    kept = []
    for name, text in parts:
        if text in trimmed:
            kept.append(name)
    check(len(kept) >= 3, f"保留份数 ≥ 地板 3（实得 {len(kept)}）", str(kept))
    check(kept == [n for n, _ in parts if n in kept],
          "保留部分保持原序（口径有先后依赖，不许重排）")
    check(all(f"\n\n{t}\n\n" in f"\n\n{trimmed}\n\n" or trimmed.startswith(t) for _, t in parts
              if t in trimmed),
          "每一片都能在裁剪结果里逐字找到（子集，不是重写）")
    omitted = [n for n, _ in parts if n not in kept]
    check(all(n in trimmed for n in omitted) and "与当前问题无关" in trimmed,
          "trailer 列出未展示文件名，且措辞是「无关」不是「读不到」", str(omitted))
    check(not any("找不到" in trimmed or "读不到" in trimmed for _ in [0]),
          "trailer 不含「读不到」类措辞（免得模型去 VFS 找文件）")

    # 让步档：单份命中即止（证明「命中的那几份」确实由检索决定，而非平均主义）
    small = KnowledgeTrimMiddleware(min_chars=10, min_files=1, keep_ratio=0.0)
    out2 = small._process_sync(msg, req)
    t2 = json.loads(out2.content)["instructions"]
    check(t2 != trimmed and len(t2) < len(trimmed),
          "放宽地板 ⇒ 只留命中的那份（策略真的在起作用）",
          f"{len(trimmed)} → {len(t2)}")
    hit_text = next(t for n, t in parts if n == "报工口径.md")
    check(hit_text in t2 and all(t not in t2 for n, t in parts if n != "报工口径.md"),
          "命中的正是含「加班工时」的那一份（检索真的在选文件）", "报工口径.md")

    # ── 负对照（必须逐字原样）──────────────────────────────
    def untouched(label: str, middleware: KnowledgeTrimMiddleware, request, message) -> None:
        got = middleware.wrap_tool_call(request, lambda _r: message)
        check(got.content == message.content, label)

    os.environ["NL2SQL_KNOWLEDGE_TRIM"] = "off"
    got = small.wrap_tool_call(req, lambda r: msg)
    check(got.content == msg.content, "★ 负对照：NL2SQL_KNOWLEDGE_TRIM=off ⇒ 逐字原样")
    os.environ.pop("NL2SQL_KNOWLEDGE_TRIM", None)

    # 非裁剪工具：ToolMessage 的 name 与真实工具名一致（生产就是如此），此时一个字都不改
    for name in ("run_sql", "read_file", "list_knowledge"):
        r = SimpleNamespace(tool=tool(name, big), state=state)
        other = msg.model_copy(update={"name": f"wrenai_demo_{name}"})
        untouched(f"★ 负对照：非裁剪工具 {name} ⇒ 逐字原样", small, r, other)

    # 重建≠原文（把 wren 那边的口径弄错）：只会静默不裁，绝不会裁错内容
    with mock.patch.object(kt, "_rules_parts", lambda p: [("x.md", "别的东西")]):
        untouched("★ 负对照：重建 ≠ 原文 ⇒ 放弃裁剪（闸 1 承重）", small, req, msg)

    r_noq = SimpleNamespace(tool=tool("get_instructions", big), state={"messages": []})
    untouched("★ 负对照：拿不到当前问题 ⇒ 逐字原样", small, r_noq, msg)

    r_bad = SimpleNamespace(tool=tool("get_instructions", big), state=state)
    bad = ToolMessage(content="不是 JSON", name="wrenai_demo_get_instructions", tool_call_id="t")
    untouched("★ 负对照：内容不是 JSON ⇒ 逐字原样", small, r_bad, bad)

    # 无 _wren_project_path 的工具（非 wren 工具）⇒ 不动
    r_nopath = SimpleNamespace(tool=SimpleNamespace(name="wrenai_demo_get_instructions"), state=state)
    untouched("★ 负对照：工具没有 _wren_project_path ⇒ 逐字原样", small, r_nopath, msg)

    # 小语料 + 默认阈值 ⇒ 闸 2（不足 8000 字符）不裁
    small_proj = ws / "tiny_wrenai"
    make_project(small_proj, rules={"一份.md": "## 加班工时\n\n以报工单为准。\n"}, examples={})
    indexer.build_index(small_proj, "demo", force=True, with_vectors=False)
    tiny_raw, tiny_legacy = load_rules(small_proj)
    tiny_msg = ToolMessage(content=json.dumps({"instructions": tiny_raw, "used_legacy": tiny_legacy},
                                              ensure_ascii=False),
                           name="wrenai_demo_get_instructions", tool_call_id="t2")
    r_tiny = SimpleNamespace(tool=tool("get_instructions", small_proj), state=state)
    untouched("★ 负对照：小语料（<8000 字符）⇒ 逐字原样", KnowledgeTrimMiddleware(), r_tiny, tiny_msg)

    # ── 异步路径（awrap_tool_call 走 to_thread，结果必须与同步一致）──
    async def _run_async():
        async def handler(_r):
            return msg

        return await small.awrap_tool_call(req, handler)

    got_async = asyncio.run(_run_async())
    check(got_async.content == out2.content, "异步路径结果与同步逐字一致（to_thread 不漏字段）")

    # ── 接线闸：两个组合根都得挂上 ────────────────────────
    for path in ("src/agent/graphs/nl2sql_agent.py", "src/agent/main_agent.py"):
        src = (_REPO / path).read_text(encoding="utf-8")
        check("KnowledgeTrimMiddleware(" in src, f"接线：{path} 里挂了中间件")
    nl2sql_src = (_REPO / "src/agent/graphs/nl2sql_agent.py").read_text(encoding="utf-8")
    check(nl2sql_src.index("MessageSlimmerMiddleware(") < nl2sql_src.index("KnowledgeTrimMiddleware(),"),
          "接线：挂在 MessageSlimmer **之后**（更内层 ⇒ 先裁后瘦身，不出现两套账）")


def run_store_query_refresh(ws: pathlib.Path, proj: pathlib.Path) -> None:
    """⑥ store_query 写盘后显式刷新索引（含 id 一致性与 need_rebuild 分支）。"""
    section("⑥ store_query 刷新：可搜到 / id 一致 / 形状不污染")
    set_mode("fts")
    idx_dir = S.index_dir_for(proj)
    indexer.build_index(proj, "demo", force=True, with_vectors=False)

    t = tool("store_query", proj)
    nl, sql = "查询各仓库库存合计", "SELECT SUM(qty) AS total FROM hr.t_stock"
    art = artifact_of(fast(t, {"nl_query": nl, "sql_query": sql, "tags": "库存,盘点"}))
    check(set(art) == {"path"}, "返回值仍是 {\"path\": …}（刷新结果只进日志）")
    md_path = pathlib.Path(art["path"])
    check(md_path.is_file() and md_path.parent.name == "sql", "markdown 落在 knowledge/sql/")

    adapters._fresh_cache.clear()
    hits = search.search_project(proj, "仓库库存合计", limit=5, kinds={S.KIND_EXAMPLE_SQL})
    got = next((h for h in hits if h["src_path"].endswith(md_path.name)), None)
    check(got is not None, "新写的范例对**立刻**可被检索到（无需全量重建）")
    if got:
        check((got.get("meta") or {}).get("nl") == nl and (got.get("meta") or {}).get("sql") == sql,
              "meta 里的 nl/sql 逐字回读", str((got.get("meta") or {}).get("path")))
        check((got.get("meta") or {}).get("tags") == ["库存", "盘点"],
              "tags 规范化成 list", str((got.get("meta") or {}).get("tags")))
        id_after_upsert = got["id"]

        # id 一致性闸：全量重建后同一条语料的 id 必须一模一样（否则 merge_insert 会插重复）
        indexer.build_index(proj, "demo", force=True, with_vectors=False)
        adapters._fresh_cache.clear()
        hits2 = search.search_project(proj, "仓库库存合计", limit=5, kinds={S.KIND_EXAMPLE_SQL})
        got2 = next((h for h in hits2 if h["src_path"].endswith(md_path.name)), None)
        check(got2 is not None and got2["id"] == id_after_upsert,
              "★ id 一致性：增量刷新与全量重建算出同一个 id",
              str(id_after_upsert)[:16])
        check(id_after_upsert == indexer.example_item(proj, "demo", md_path).id,
              "★ id 与 example_item() 的唯一构造一致（同源不重算）")

    # 负对照：索引不存在 ⇒ upsert 报 need_rebuild ⇒ 落到 build_index，之后照样能搜
    adapters._fresh_cache.clear()
    shutil.rmtree(idx_dir)
    art2 = artifact_of(fast(t, {"nl_query": "查询部门人数", "sql_query": "SELECT COUNT(*) FROM hr.t_dept"}))
    res = adapters.refresh_examples(proj, "demo", pathlib.Path(art2["path"]))
    check(res.get("rebuilt") is True or res.get("ok") is True,
          "★ 负对照：索引缺失 ⇒ 自动落到 build_index", str(res)[:120])
    adapters._fresh_cache.clear()
    hits3 = search.search_project(proj, "部门人数", limit=5, kinds={S.KIND_EXAMPLE_SQL})
    check(bool(hits3), "重建后新范例对仍可搜到")

    # 负对照：off ⇒ refresh 不建索引、不报错
    shutil.rmtree(idx_dir, ignore_errors=True)
    set_mode("off")
    res_off = adapters.refresh_examples(proj, "demo", md_path)
    check(res_off.get("skipped") is True and not idx_dir.exists(),
          "★ 负对照：off ⇒ 直接跳过，且不留下任何索引目录", str(res_off))
    set_mode("fts")


def run_db_name(ws: pathlib.Path, proj: pathlib.Path, backup: pathlib.Path) -> None:
    """⑦ project → db_name：只信注入属性与注册表，绝不按目录名拼。"""
    section("⑦ project→db_name：注册表精确匹配（≠ 目录名）/ 未知路径为空")
    name_from_registry = adapters.db_name_for(SimpleNamespace(), proj)
    check(name_from_registry == "demo" and proj.name == "demo_wrenai",
          "注册表解析出 name=demo（**不等于**目录名 demo_wrenai）",
          f"{name_from_registry} vs {proj.name}")
    check(adapters.db_name_for(SimpleNamespace(_wren_db_name="witops"), proj) == "witops",
          "工具上注入的 _wren_db_name 优先（探测器同源，比回查注册表更准）")
    check(adapters.db_name_for(SimpleNamespace(), backup) == "",
          "★ 负对照：<name>.备份-* 这种没注册的目录 ⇒ 空串（不猜）")
    check(adapters.db_name_for(SimpleNamespace(), ws / "nowhere") == "",
          "★ 负对照：不存在的路径 ⇒ 空串（fail-open：搜索侧不传库过滤）")
    src = (_REPO / "src/agent/tools/mcp_tool.py").read_text(encoding="utf-8")
    check("_t._wren_db_name = str(_db)" in src,
          "接线：mcp_tool.py 与 _wren_project_path 同处注入 _wren_db_name")


def run_corpus_same_source(ws: pathlib.Path, proj: pathlib.Path) -> None:
    """⑧ R12：索引语料与「全量注入/核验」同源；新语义只做加法。"""
    section("⑧ 语料同源（R12）：knowledge/sql/*.md → example_sql，字段与 wren 逐字")
    set_mode("fts")
    no_nl = proj / "knowledge" / "sql" / "缺字段.md"
    no_nl.write_text("## 第一节\n\n没有 frontmatter 的说明。\n\n## 第二节\n\n继续。\n", encoding="utf-8")
    items = indexer.collect_items(proj, "demo")
    from wren.memory.markdown import load_query_pairs

    pairs = {p["path"]: p for p in load_query_pairs(proj)}

    # 按 src_path 归一（分块条目的 src_path 带 `#index`）后再比对
    by_src: dict[str, list] = {}
    for i in items:
        by_src.setdefault(i.src_path.split("#", 1)[0], []).append(i)

    for md in sorted((proj / "knowledge" / "sql").glob("*.md")):
        src = f"knowledge/{md.relative_to(proj / 'knowledge')}"
        got = by_src.get(src, [])
        item = next((i for i in got if i.kind == S.KIND_EXAMPLE_SQL), None)
        if md.name == "缺字段.md":
            check(item is None, "★ 负对照：缺 nl 的 md 不冒充范例对（回落旧分支）")
            check(got and all(i.kind == S.KIND_KNOWLEDGE_RULE for i in got),
                  "★ 负对照：缺字段的 md 仍按 knowledge_rule 切块",
                  f"{len(got)} 块：{[i.src_path for i in got]}")
            check(len(got) > 1 and any("#" in i.src_path for i in got),
                  "★ 负对照：仍分块（src_path 带 #index）")
            continue
        pair = pairs.get(str(md.relative_to(proj)))
        if not check(item is not None and pair is not None, f"{md.name} 产出 example_sql"):
            continue
        meta = item.meta
        check(meta["nl"] == pair["nl"] and meta["sql"] == pair["sql"],
              f"{md.name}: nl/sql 与 load_query_pairs 逐字相等")
        check(meta["datasource"] == pair.get("datasource", ""),
              f"{md.name}: datasource 一致", repr(meta["datasource"]))
        check([str(t) for t in meta["tags"]] == [str(t) for t in pair.get("tags", [])],
              f"{md.name}: tags 一致", str(meta["tags"]))
        check(meta["path"] == pair["path"], f"{md.name}: path 逐字照抄 wren", meta["path"])
        check(meta["chunk"] == 0 and "#" not in item.src_path,
              f"{md.name}: 一文件一条、不再分块")
        check(item.title == meta["nl"][:120], f"{md.name}: title 取 NL（召回主信号）")

    # `.sql`（老夹具形态）仍是 example_sql；rules/*.md 仍是 knowledge_rule
    (proj / "knowledge" / "sql" / "老式.sql").write_text("SELECT 1;\n", encoding="utf-8")
    items2 = indexer.collect_items(proj, "demo")
    check(any(i.kind == S.KIND_EXAMPLE_SQL and i.src_path.endswith("老式.sql") for i in items2),
          "★ 负对照：.sql 文件仍是 example_sql（加法，不是替换）")
    check(any(i.kind == S.KIND_KNOWLEDGE_RULE for i in items2 if "rules" in i.src_path),
          "★ 负对照：rules/*.md 仍是 knowledge_rule（没被范例对逻辑吃掉）")
    (proj / "knowledge" / "sql" / "老式.sql").unlink()
    no_nl.unlink()


def run_lazy_import() -> None:
    """⑨ 惰性 import：off 下不许加载检索层；`get_config()` 在中间件里真能读到问题。"""
    section("⑨ 惰性 import + 每 run 问题的真实来源")
    env = {**os.environ, "PYTHONPATH": str(_REPO / "src"), "AGENT_DATA_ROOT": str(_WORK)}

    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, agent.retrieval.adapters\n"
         "assert 'lancedb' not in sys.modules, 'import 期加载了 lancedb'\n"
         "print('lazy-ok')\n"],
        capture_output=True, text=True, env=env,
    )
    check(proc.returncode == 0 and "lazy-ok" in proc.stdout,
          "import adapters 不加载 lancedb", (proc.stderr or "").strip()[-200:])

    env_off = {k: v for k, v in env.items() if k != "NL2SQL_RETRIEVAL"}
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, agent.utils.path_resolver\n"
         "assert 'agent.retrieval' not in sys.modules, 'off 下把检索层 import 了'\n"
         "print('off-ok')\n"],
        capture_output=True, text=True, env=env_off,
    )
    check(proc.returncode == 0 and "off-ok" in proc.stdout,
          "off ⇒ import path_resolver 后 agent.retrieval 不在 sys.modules",
          (proc.stderr or "").strip()[-200:])

    # 第 4 条不确定项：`wrap_tool_call` 里 `get_config()` 到底拿不拿得到 user_question
    from typing import TypedDict

    from langgraph.config import get_config
    from langgraph.graph import END, START, StateGraph

    class _St(TypedDict, total=False):
        messages: list
        seen: str
        seen_by_mw: str

    def node(state):  # noqa: ANN001
        cfg = get_config()
        q = str((cfg.get("metadata", {}) or {}).get("user_question", "") or "")
        # 中间件读的是同一个上下文：以「state 里没有消息」的极端情形证明它读的是 config
        return {"seen": q, "seen_by_mw": kt._current_question({"messages": []})}

    g = StateGraph(_St)
    g.add_node("n", node)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    out = g.compile().invoke({"messages": []},
                             config={"metadata": {"user_question": "加班工时怎么算"}})
    check(out.get("seen") == "加班工时怎么算",
          "run 内 get_config()['metadata']['user_question'] 可读")
    check(out.get("seen_by_mw") == "加班工时怎么算",
          "★ 中间件的 _current_question 在 run 内优先读 config（state 空也能拿到问题）")


def main() -> int:
    print(f"AGENT_DATA_ROOT = {_WORK}")
    print(f"WREN_MEMORY_BACKEND = {os.environ.get('WREN_MEMORY_BACKEND')}"
          f" / 后端 = {backends.backend_name()} / lancedb = {backends.lance_available()}")
    ws = _WORK / "workspace"
    proj = ws / "demo_wrenai"
    backup = ws / "demo_wrenai.备份-20260921-101010"
    big = ws / "big_wrenai"
    bare = ws / "bare_wrenai"      # 无范例对 ⇒ 验 recall_queries 的空短路

    with mock.patch.object(S, "workspace_root", lambda: ws), mock.patch.object(
        _wm_mod, "get_workspace_manager", lambda: _FakeWM(ws)
    ):
        make_project(proj, examples={"工时示例.md": _EXAMPLE_A, "加班示例.md": _EXAMPLE_B})
        make_project(backup, tag="backup", examples={})
        make_project(big, rules=_big_rules(), examples={})
        make_project(bare, examples={})
        write_db_config(ws, proj)

        run_r1_gate(ws, proj)
        run_off_identical(ws, proj, bare)
        run_fts_schema(ws, proj)
        run_matches_shape(ws, proj)
        run_fallback_negatives(ws, proj)
        run_middleware(ws, big)
        run_store_query_refresh(ws, proj)
        run_db_name(ws, proj, backup)
        run_corpus_same_source(ws, proj)

    run_lazy_import()

    passed = sum(1 for ok, _l, _s in results if ok)
    total = len(results)
    print(f"\n{'=' * 62}\n{passed}/{total} 通过")
    if passed != total:
        print("失败项：")
        for ok, label, scene in results:
            if not ok:
                print(f"  - [{scene}] {label}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
