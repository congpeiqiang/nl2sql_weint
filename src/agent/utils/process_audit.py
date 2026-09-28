"""process_audit — 中间产物审计：目录轴规范化 + SQL 来源判决 + 子任务 manifest。

三件事，都是**确定性**的（零模型往返、零提示词改动）：

1. **目录轴恒等于技能名**（`normalize_skill_dir`）
   落盘目录原先取 `langfuse_span._resolve_display_skill` 的返回值，它在共享工具
   （只有 `dry_run`）遇到「线程活动 skill 过期」时会**回退成启发式族名**
   （`sql-generation`），于是 `nl2sql_process_data/{thread}/` 下同时存在技能名目录
   与族名目录两套。读者（`check_progress._backfill_process_data_sql`）曾写死读
   `sql-generation/`，两套只在「活动 skill 恰好过期」时才碰巧对上 —— **常年静默 0
   且无人察觉**（回填本身 fail-open，不报错也不打日志）。
   本函数把目录轴收敛：**返回值恒 ∈ `SKILL_DIR_NAMES` ∪ {""}**，绝不返回族名。
   ⚠️ 不回改 `_resolve_display_skill` 本身 —— 它的回退行为被
   `scripts/verify_skill_owner_table.py` 的负例钉住（span 轴仍按原样工作）。

2. **SQL 来源判决**（`judge_sql_origin`）
   此前只有 `check_progress` 里的 `sql_kind="cube"`，且**恒为 cube**（手写靠「键
   缺席」表达）⇒ **混合路径（Cube 出指标主体 + 模型包外层）与纯手写长得一模一样**，
   而它恰恰是最需要区分的一态。本函数从工具调用轨迹确定性判定四态，含混合。
   ⚠️ 不复制 `agent/eval/run_experiment.py::_extract_strategy` 的两个缺陷：它把
   `list_cubes`（纯检索）算作 Cube 证据，且把混合归入 `"C"`。

3. **子任务 manifest**（`write_process_artifacts`）
   一条问数最终落成一份可审计的索引：工具轨迹 + 七个技能各自是否有产物 + 来源判决
   + 四个子布局（`{skill}/`、`skill_sop/{skill}/`、`wren_plan/`、`query_result/`）的
   全部产物路径。`wren-orchestrator` 的路由是「零工具调用」的纯推理（SKILL.md 明写），
   工具边界抓不到 ⇒ 由平台从轨迹**复算**（复用同一份来源判据，不写第二套）。

**边界（写进代码，别再当成联动关系）**：前端进度条逐字显示模型自己写的
`todos[].content`，与本模块产物之间**没有任何数据通路**；改这里不会影响进度条，
改进度条也不会影响这里。

**交付面**：本模块只走代码发版，**不改任何 `SKILL.md` 与提示词** ⇒ 无需推 Langfuse、
无需同步运行期技能副本。若将来为了「让模型配合」而改了那两处，必须同时
`cp -a` 到 `<AGENT_DATA_ROOT>/shared/skills`（`scripts/check_skills_drift.py` 体检）
并重启（提示词 import 期求值）—— 这是历史事故高发点。

**fail-open 铁律**：本模块所有落盘与解析都不许抛出到调用方。审计是旁路，绝不能因为
它失败就影响查询主流程；异常一律 `_logger.debug/info` 后返回空值。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable

from agent.utils.wren_call_extract import (
    CUBE_TOOL_HINT,
    call_result_message,
    cube_arg_lines,
    msg_get,
    normalize_cube_spec,
    tool_result_error,
    _msg_content_str,
)

_logger = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION = 1

# ── 技能目录名（盘上真实目录，与 SKILL.md frontmatter `name` 逐一相等）──────
# verify_process_audit.py 会把这张表与盘上目录集合、frontmatter 名双向断言，
# 拼写漂移由测试抓（不靠 import 抓）。
SKILL_DIR_NAMES = (
    "wren-orchestrator",
    "wren-retrieve",
    "wren-clarify",
    "wren-metric-query",
    "wren-sql-author",
    "wren-perf-optimize",
    "wren-execution",
    "wren-writeback",
)
# 会话内可达的技能（`wren-writeback` 是循环外回写规范，由 FeedbackStore 桥接执行，
# 不产会话期产物）。manifest 的 skills 一节按这张表铺开，缺产物也要在场并说明原因。
SESSION_SKILLS = SKILL_DIR_NAMES[:7]

# ── 工具族（启发式）→ 规范技能目录 ────────────────────────────────────────
# 键 = `langfuse_span.TOOL_SKILL_MAP` 的全部取值（8 个族）；值 = 该族的规范 owner，
# `""` = 无目录语义 / 不落盘。映射安全的前提：每个族在 `_TOOL_OWNER_SKILLS` 里的
# owner 是唯一的（schema-linking / knowledge-retrieval / recall-queries 全族 →
# wren-retrieve；cube-query → wren-metric-query；sql-execution → wren-execution；
# sql-generation 的 dry_plan 唯一属 wren-sql-author、dry_run 的规范产出方也是它），
# 所以回退只是把族名兜底收敛回它的规范 owner，不会张冠李戴。
HEURISTIC_TO_SKILL: dict[str, str] = {
    "schema-linking": "wren-retrieve",
    "knowledge-retrieval": "wren-retrieve",
    "recall-queries": "wren-retrieve",
    "cube-query": "wren-metric-query",
    "sql-generation": "wren-sql-author",
    "sql-execution": "wren-execution",
    "artifact-write": "",
    "artifact-read": "",
}

# process_data 下的非 per-call 子布局（manifest 要索引它们，回填扫描要跳过它们）
LAYOUT_TOOL_DUMP = "tool_dump"
LAYOUT_SKILL_SOP = "skill_sop"
LAYOUT_WREN_PLAN = "wren_plan"
LAYOUT_QUERY_RESULT = "query_result"
LAYOUT_MANIFEST = "_manifest"
_SKIP_DIRS = frozenset({LAYOUT_SKILL_SOP, LAYOUT_WREN_PLAN, LAYOUT_QUERY_RESULT,
                        LAYOUT_MANIFEST})

# ── SQL 来源四态 ─────────────────────────────────────────────────────────
SQL_ORIGIN_CUBE = "cube_metric"           # 由 Cube 具名指标编译下发
SQL_ORIGIN_MIXED = "cube_metric+llm_outer"  # Cube 出主体 + 模型包外层（混合路径）
SQL_ORIGIN_LLM = "llm_from_schema"        # 模型照语义库 schema 手写
SQL_ORIGIN_UNKNOWN = "unknown"            # 无证据 —— 不猜

# 纯检索/探查工具：**一律不是**来源证据，只进 evidence.metadata_only_tools 备查。
# 列在这里是显式声明「我们审过这些名字」；`list_cubes` 尤其重要 —— 它是「看过
# 有哪些 cube」，不是「用 cube 取了数」，`_extract_strategy` 就栽在这一条上。
METADATA_ONLY_TOOLS = (
    "list_cubes", "describe_cube", "get_mdl", "list_models", "describe_model",
    "describe_schema", "get_db_info", "get_data_source", "get_context",
    "get_instructions", "list_knowledge", "get_all_knowledge", "recall_queries",
    "list_stored_queries", "dry_plan", "dry_run",
)

_WS_RE = re.compile(r"\s+")
_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.+?)```", re.S | re.I)

_TRACE_MAX = 500          # tool_trace 条数上限（超出加 warning 并停止追加）
_JSON_MAX_BYTES = 4 * 1024 * 1024   # 单份产物落盘上限


# ══════════════════════════════════════════════════════════════════════════
# A. 目录轴规范化
# ══════════════════════════════════════════════════════════════════════════
def normalize_skill_dir(skill: str, heuristic: str) -> str:
    """展示 skill / 启发式族 → **技能目录名**（恒 ∈ `SKILL_DIR_NAMES` ∪ {""}）。

    优先级：
    1. `skill` 已是真实技能目录名 → 原样返回（绝大多数情况，`_resolve_display_skill`
       命中唯一归属或活动 skill 时走这条）；
    2. 否则按启发式族回退到它的规范 owner（例如 `("sql-generation", "sql-generation")`
       → `"wren-sql-author"`）—— 这正是此前目录名会变成族名的那条路；
    3. 都落空（未知的新族名 / 空串）→ `""`，调用方**不落盘**。

    绝不返回未知值：宁可不落盘，也不拿族名或拼错的名字去建目录（建出来的目录没人读，
    等于静默丢产物）。
    """
    s = str(skill or "")
    if s in SKILL_DIR_NAMES:
        return s
    return HEURISTIC_TO_SKILL.get(str(heuristic or ""), "")


def attribute_tool_skill(tool_name: str) -> tuple[str, str]:
    """工具名 → ``(技能目录, 归属依据)``（依据 ∈ ``unique_owner`` / ``derived`` / ``""``）。

    这是**从消息轨迹复算**的归属，不读进程级的 `_THREAD_ACTIVE_SKILL`（那是内存态、
    会过期、重启即失，用它做审计等于把结论建在易失状态上）。共享工具（`dry_run` 有
    5 个 owner）无法从名字定归属 ⇒ 退回启发式族的规范 owner，依据记 `derived`，
    manifest 里如实标注，不假装确定。
    """
    name = str(tool_name or "")
    if not name:
        return "", ""
    try:
        from agent.middlewares.langfuse_span import _classify_skill, _tool_owners
    except Exception:  # noqa: BLE001  审计旁路，取不到就不归属
        return "", ""
    try:
        owners = _tool_owners(name)
        if owners and len(owners) == 1:
            return owners[0], "unique_owner"
        heur = _classify_skill(name) or ""
        return normalize_skill_dir("", heur), "derived"
    except Exception:  # noqa: BLE001
        return "", ""


def _short_tool(tool_name: str) -> str:
    """`wrenai_WIT_query_cube` → `query_cube`（工具名自身含下划线，不能 rsplit）。"""
    name = str(tool_name or "")
    try:
        from agent.middlewares.langfuse_span import TOOL_SKILL_MAP, _TOOL_OWNER_SKILLS
    except Exception:  # noqa: BLE001
        return name
    vocab = set(TOOL_SKILL_MAP) | set(_TOOL_OWNER_SKILLS)
    for k in sorted(vocab, key=len, reverse=True):
        if name == k or name.endswith("_" + k):
            return k
    return name


# ══════════════════════════════════════════════════════════════════════════
# B. 轨迹扫描（只扫一次，判来源与建 manifest 共用）
# ══════════════════════════════════════════════════════════════════════════
def _norm_sql(s: Any) -> str:
    return _WS_RE.sub(" ", str(s or "")).strip()


def _digest(s: Any) -> str:
    t = _norm_sql(s)
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:12] if t else ""


def _tool_calls_of(m) -> list:
    calls = msg_get(m, "tool_calls") or []
    if not calls:
        calls = (msg_get(m, "additional_kwargs") or {}).get("tool_calls") or []
    return [c for c in calls if isinstance(c, dict)]


def _call_args(call: dict) -> dict:
    args = call.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            return {}
    return args if isinstance(args, dict) else {}


def scan_tool_calls(messages) -> list[dict]:
    """轨迹扫描：每条调用一条记录（含结果成败），按 ``seq`` 排序。

    ``seq = (消息下标, 消息内调用位次)`` —— 同一轮并发多次调用也算得清先后
    （与 `wren_call_extract.extract_last_cube_call` 同口径）。

    fail-open：非序列入参、残缺消息、args 是垃圾字符串 → 跳过该条，绝不抛。
    """
    out: list[dict] = []
    if not messages:
        return out
    try:
        iterable: Iterable = messages
    except TypeError:
        return out
    for i, m in enumerate(iterable):
        if m is None:
            continue
        try:
            calls = _tool_calls_of(m)
        except Exception:  # noqa: BLE001
            continue
        for pos, call in enumerate(calls):
            name = str(call.get("name") or "")
            if not name:
                continue
            args = _call_args(call)
            try:
                res = call_result_message(messages, i, call.get("id"), name)
                err = bool(res is not None and tool_result_error(res))
            except Exception:  # noqa: BLE001
                res, err = None, False
            skill, owner = attribute_tool_skill(name)
            out.append({
                "seq": (i, pos),
                "msg_index": i,
                "tool": name,
                "short": _short_tool(name),
                "family": _family(name),
                "ok": not err,
                "args": args,
                "result_text": _msg_content_str(res) if res is not None else "",
                "skill": skill,
                "skill_owner": owner,
            })
    out.sort(key=lambda r: r["seq"])
    for n, rec in enumerate(out, 1):
        rec["seq"] = n
    return out


def _result_kind(rec: dict) -> str:
    """结果形态（审计用，不做判据）：rows / error / text / empty。"""
    if not rec.get("ok"):
        return "error"
    text = (rec.get("result_text") or "").strip()
    if not text:
        return "empty"
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return "text"
    if isinstance(obj, dict) and any(k in obj for k in ("rows", "columns", "row_count")):
        return "rows"
    return "text"


def _compiled_sql_of(rec: dict) -> str:
    """从一次 cube 调用的结果正文里抠编译 SQL（**只认确定形态**，否则空串）。

    只认两种：结果是 JSON 且带 `sql` 键（`query_cube(sql_only=True)` 的返回就是
    `{"sql": …}`）；或正文里有 ```sql 围栏。抠不到返回空串 —— 这个信号只作次要
    置信标注（`evidence.cube_sql_in_final`），**不参与来源枚举**，所以宁可拿不到，
    也不去猜一段可能是别的东西的文本。
    """
    text = (rec.get("result_text") or "").strip()
    if not text:
        return ""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and isinstance(obj.get("sql"), str) and obj["sql"].strip():
            return obj["sql"]
    except (json.JSONDecodeError, ValueError):
        pass
    m = _SQL_FENCE_RE.search(text)
    if m and m.group(1).strip():
        return m.group(1)
    return ""


# ══════════════════════════════════════════════════════════════════════════
# C. SQL 来源判决
# ══════════════════════════════════════════════════════════════════════════
def judge_sql_origin(messages, producing_sql: str = "", _recs: list[dict] | None = None) -> dict:
    """本次 SQL 的来源判决。**唯一判据** —— manifest / check 结果 / 报告三处共用。

    **锚点定义（必须写清，它是本判据的全部争议所在）**：来源 = 「**产出最终结果表
    的那一次执行**」。`producing_sql` 由调用方传入（`check_progress` 的
    `_extract_last_sql`，也正是前端与报告逐字展示给用户的那条）。不在这里重算，
    一是避免 O(n²) 级重复扫描，二是避免本模块反向 import `check_progress` 成环。
    不以 `extract_last_cube_call` 为锚 —— 它取「最后一次**成功**调用」，可能只是一次
    探值/预览。

    返回 ``{"origin", "anchor", "confidence", "evidence"}``。四态与判据：

    ============================================  ==========================
    条件                                           origin
    ============================================  ==========================
    `producing_sql` 非空，且锚点**之前**有 cube 调用  `cube_metric+llm_outer`
    （`data_bearing`，或 `ok and sql_only`）
    `producing_sql` 非空，其余（含 cube 调用都在      `llm_from_schema`
    锚点之后）
    `producing_sql` 为空，有 `data_bearing` 的       `cube_metric`
    cube 调用
    `producing_sql` 为空，只有 `sql_only` 预览成功   `unknown`
    ============================================  ==========================

    其余一律 `unknown`（澄清中断、抽取失败、结果来自上下文/缓存）—— **无证据不猜**。

    ⚠️ 已知的定义性取舍：模型若先用 `query_cube`（取数）拿表、再用 `run_sql` 探值或
    补算，按上表判为 mixed。这是「最终数据来源」这一定义的自然结果，不是误判；逐条
    明细留在 `evidence` 里供事后复核。

    ⚠️ 纯检索工具（`list_cubes` / `describe_cube` / `get_mdl` / `describe_schema` /
    `get_context` …）**不是来源证据**：它们只说明「看过有哪些 cube」，不说明「用了
    cube 取数」。`run_experiment._extract_strategy` 把 `list_cubes` 算作 Cube 证据、
    把混合归为 `"C"`，本函数刻意不复制这两个缺陷（见 `METADATA_ONLY_TOOLS`）。
    """
    ev: dict[str, Any] = {
        "cube_calls": 0, "cube_data_calls": 0, "cube_preview_calls": 0,
        "cube_before_anchor": 0, "run_sql_calls": 0, "anchor_found": False,
        "metadata_only_tools": [], "cube_sql_in_final": None, "reason": "",
    }
    try:
        recs = _recs if _recs is not None else scan_tool_calls(messages)
    except Exception as e:  # noqa: BLE001  审计旁路，绝不影响主流程
        _logger.debug("[process_audit] 轨迹扫描失败: %s", e)
        return {"origin": SQL_ORIGIN_UNKNOWN, "anchor": "none",
                "confidence": "none", "evidence": {**ev, "reason": "scan_failed"}}

    cube_recs, run_recs = [], []
    meta_tools: list[str] = []
    for r in recs:
        short, name = r["short"], r["tool"]
        if CUBE_TOOL_HINT in name:
            cube_recs.append(r)
        elif name.endswith("run_sql") or short.endswith("run_sql"):
            run_recs.append(r)
        elif short in METADATA_ONLY_TOOLS:
            meta_tools.append(short)

    ev["cube_calls"] = len(cube_recs)
    ev["cube_data_calls"] = sum(
        1 for r in cube_recs if r["ok"] and not r["args"].get("sql_only")
    )
    ev["cube_preview_calls"] = sum(
        1 for r in cube_recs if r["ok"] and r["args"].get("sql_only")
    )
    ev["run_sql_calls"] = len(run_recs)
    ev["metadata_only_tools"] = sorted(set(meta_tools))

    target = _norm_sql(producing_sql)

    if target:
        anchor_seq = None
        for r in run_recs:                       # 最后一次「就产出这条 SQL」的执行
            if _norm_sql(r["args"].get("sql")) == target:
                anchor_seq = r["seq"]
        ev["anchor_found"] = anchor_seq is not None
        before = [r for r in cube_recs if anchor_seq is None or r["seq"] < anchor_seq]
        ev["cube_before_anchor"] = len(before)
        # 判据 = 锚点**之前**存在成功的 cube 调用（取数或仅预览都算：预览编译 SQL 后
        # 手写外层正是混合路径的形态）。失败的调用不算证据。
        qualifying = [r for r in before if r["ok"]]
        if qualifying:
            for r in reversed(qualifying):
                compiled = _compiled_sql_of(r)
                if compiled:
                    ev["cube_sql_in_final"] = _norm_sql(compiled) in target
                    break
            return {
                "origin": SQL_ORIGIN_MIXED, "anchor": "run_sql",
                # 锚点能定位 = 次序可信；只知「表里存在 cube 成功调用」= 次序不可证
                "confidence": "strong" if anchor_seq is not None else "weak",
                "evidence": ev,
            }
        ev["reason"] = "no_cube_call_before_anchor" if cube_recs else "no_cube_call"
        return {"origin": SQL_ORIGIN_LLM, "anchor": "run_sql",
                "confidence": "strong" if anchor_seq is not None else "weak",
                "evidence": ev}

    if ev["cube_data_calls"]:
        # `cube_sql_in_final` 只在「有最终 SQL 可比」时有意义，此处如实留 None（不猜）
        return {"origin": SQL_ORIGIN_CUBE, "anchor": "cube",
                "confidence": "strong", "evidence": ev}

    if ev["cube_preview_calls"]:
        ev["reason"] = "preview_only_no_execution"
        return {"origin": SQL_ORIGIN_UNKNOWN, "anchor": "none",
                "confidence": "weak", "evidence": ev}

    ev["reason"] = "no_sql_evidence"
    return {"origin": SQL_ORIGIN_UNKNOWN, "anchor": "none",
            "confidence": "none", "evidence": ev}


# ══════════════════════════════════════════════════════════════════════════
# D. 产物收集 + manifest 组装 + 落盘
# ══════════════════════════════════════════════════════════════════════════
def _vfs(rel: str) -> str:
    return "/workspace/nl2sql_process_data/" + rel.lstrip("/")


def collect_skill_artifacts(root: str, session_thread_id: str) -> dict:
    """扫四个子布局，按技能归集产物路径（VFS 视角）。

    返回 ``{"by_skill": {skill: {"files": [...], "sop_files": [...]}},
    "layouts": {四个布局名: 路径模板}, "artifacts": [...全部去重排序]}``。

    四个布局**不合并**：写入方各不同（服务端 dump / 模型按 SKILL.md 契约自写 /
    `wren_plan.write_plan_file` / `QueryResultOffload`），合并要把 `SKILL.md` 里的
    落盘路径一起改 —— 那是第三个交付面（运行期生效的是
    `<AGENT_DATA_ROOT>/shared/skills`，改仓库 ≠ 线上生效）。manifest 全索引即可。
    """
    base = Path(root) / "nl2sql_process_data" / session_thread_id
    by_skill: dict[str, dict[str, list[str]]] = {
        s: {"files": [], "sop_files": []} for s in SKILL_DIR_NAMES
    }
    artifacts: list[str] = []
    layouts = {
        LAYOUT_TOOL_DUMP: f"{_vfs(session_thread_id)}/{{skill}}/*.json",
        LAYOUT_SKILL_SOP: f"{_vfs(session_thread_id)}/skill_sop/{{skill}}/*",
        LAYOUT_WREN_PLAN: f"{_vfs(session_thread_id)}/wren_plan/*.sql",
        LAYOUT_QUERY_RESULT: f"{_vfs(session_thread_id)}/query_result/*.md",
        LAYOUT_MANIFEST: f"{_vfs(session_thread_id)}/_manifest/*.json",
    }
    try:
        if not base.is_dir():
            return {"by_skill": by_skill, "layouts": layouts, "artifacts": []}
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            if d.name in _SKIP_DIRS:
                continue
            if d.name not in by_skill:       # 历史遗留族名目录：索引但不假装是技能
                by_skill[d.name] = {"files": [], "sop_files": []}
            for f in sorted(d.glob("*.json")):
                rel = f"{session_thread_id}/{d.name}/{f.name}"
                by_skill[d.name]["files"].append(rel)
                artifacts.append(_vfs(rel))
        sop = base / LAYOUT_SKILL_SOP
        if sop.is_dir():
            for d in sorted(p for p in sop.iterdir() if p.is_dir()):
                by_skill.setdefault(d.name, {"files": [], "sop_files": []})
                for f in sorted(p for p in d.rglob("*") if p.is_file()):
                    rel = f"{session_thread_id}/skill_sop/{d.name}/{f.relative_to(d)}"
                    by_skill[d.name]["sop_files"].append(rel.replace("\\", "/"))
                    artifacts.append(_vfs(rel.replace("\\", "/")))
        for sub, pat in ((LAYOUT_WREN_PLAN, "*.sql"), (LAYOUT_QUERY_RESULT, "*.md")):
            d = base / sub
            if d.is_dir():
                for f in sorted(d.glob(pat)):
                    artifacts.append(_vfs(f"{session_thread_id}/{sub}/{f.name}"))
    except Exception as e:  # noqa: BLE001
        _logger.debug("[process_audit] 产物收集失败: %s", e)
    return {"by_skill": by_skill, "layouts": layouts,
            "artifacts": sorted(set(artifacts))}


def _sop_skills(collected: dict) -> set[str]:
    return {s for s, v in (collected.get("by_skill") or {}).items() if v.get("sop_files")}


def _tool_trace(recs: list[dict]) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    trace: list[dict] = []
    for r in recs[:_TRACE_MAX]:
        spec = normalize_cube_spec(r["args"]) if CUBE_TOOL_HINT in r["tool"] else None
        trace.append({
            "seq": r["seq"], "msg_index": r["msg_index"], "tool": r["tool"],
            "short": r["short"], "family": r["family"],
            "skill": r["skill"], "skill_owner": r["skill_owner"],
            "ok": r["ok"], "result_kind": _result_kind(r),
            "sql_digest": _digest(r["args"].get("sql")),
            "cube_spec": spec,
        })
    if len(recs) > _TRACE_MAX:
        warnings.append(f"tool_trace 截断：{len(recs)} → {_TRACE_MAX} 条")
    return trace, warnings


def _family(tool_name: str) -> str:
    """工具族名（启发式），无则空串。"""
    try:
        from agent.middlewares.langfuse_span import _classify_skill
        return _classify_skill(tool_name) or ""
    except Exception:  # noqa: BLE001
        return ""


def build_routing_artifact(recs: list[dict], origin: dict, sop_skills: set[str]) -> dict:
    """wren-orchestrator 的路由判定（平台复算，**复用 origin 的同一份判据**）。

    `wren-orchestrator` 的路由是「零工具调用」的纯推理（SKILL.md 步骤(3) 明确写
    「本文件执行，零工具调用」），所以工具边界抓不到它 —— 只能由平台从轨迹复算。
    产物里显式写 `model_self_report: null`，声明这不是模型自述，防止被误读成模型的
    决策记录。
    """
    def _any_ok(pred) -> bool:
        return any(r["ok"] and pred(r) for r in recs)

    phases = {
        "retrieve": _any_ok(lambda r: r["skill"] == "wren-retrieve"),
        "clarify": "wren-clarify" in sop_skills,
        "metric_query": any(CUBE_TOOL_HINT in r["tool"] for r in recs),
        "sql_author": _any_ok(lambda r: r["short"] in ("dry_run", "dry_plan")),
        "perf_optimize": "wren-perf-optimize" in sop_skills,
        "execution": _any_ok(lambda r: r["tool"].endswith("run_sql")),
    }
    seq_short: list[str] = []
    for r in recs:
        if not seq_short or seq_short[-1] != r["short"]:
            seq_short.append(r["short"])
    return {
        "step": "route",
        "decided_by": "platform-deterministic",
        "route": origin.get("origin") or SQL_ORIGIN_UNKNOWN,
        "sql_origin_evidence": origin.get("evidence") or {},
        "phases": phases,
        "tool_sequence": seq_short,
        "model_self_report": None,
    }


def build_cube_summary(recs: list[dict]) -> dict:
    """wren-metric-query 的汇总：用了哪个 cube / 哪些 measure、dimension、取数还是仅预览。

    `query_cube` 是唯一归属工具，所以每次调用**本来就已经**落在
    `{session_thread}/wren-metric-query/` 下（payload 的 `input` 就是入参）。这里补的是
    「本次整体用了什么口径」的汇总。**只在真有 cube 调用时写** —— 手写路径不建假目录，
    manifest 用 `observed:false` 如实记录缺席。
    """
    cube_recs = [r for r in recs if CUBE_TOOL_HINT in r["tool"]]
    if not cube_recs:
        return {}
    calls = []
    for r in cube_recs:
        calls.append({
            "seq": r["seq"], "tool": r["tool"], "ok": r["ok"],
            "sql_only": bool(r["args"].get("sql_only")),
            "spec": normalize_cube_spec(r["args"]),
            "lines": cube_arg_lines(r["tool"], r["args"]),
        })
    data_calls = [c for c in calls if c["ok"] and not c["sql_only"]]
    preview_calls = [c for c in calls if c["ok"] and c["sql_only"]]
    last = (data_calls or preview_calls or calls)[-1]
    return {
        "used": True,
        "cube": str(last["spec"].get("cube") or ""),
        "measures": last["spec"].get("measures") or [],
        "dimensions": last["spec"].get("dimensions") or [],
        "filters": last["spec"].get("filters") or [],
        "time_dimension": last["spec"].get("time_dimension") or "",
        "calls": calls,
        "data_bearing_calls": len(data_calls),
        "preview_only": bool(preview_calls) and not data_calls,
        "decision": ("not_used" if not calls else
                     "cube_metric" if data_calls else "preview_only"),
    }


def _skills_section(collected: dict, recs: list[dict], origin: dict,
                    sop_skills: set[str]) -> dict:
    """七个技能各一节 —— **键恒在**，缺产物也要在场并写明原因。

    这是「每个技能都有产物」这句话的**可断言形式**：verify 脚本断言
    `set(manifest["skills"]) == set(SESSION_SKILLS)`，于是「静默没有」不可能发生。
    """
    by_skill = collected.get("by_skill") or {}
    touched: dict[str, list[str]] = {}
    for r in recs:
        if r["skill"]:
            touched.setdefault(r["skill"], []).append(f"tool:{r['short']}#{r['seq']}")

    out: dict[str, dict] = {}
    for s in SESSION_SKILLS:
        files = list(by_skill.get(s, {}).get("files") or [])
        sop_files = list(by_skill.get(s, {}).get("sop_files") or [])
        evidence = sorted(set(touched.get(s, [])))
        observed = bool(files or sop_files or evidence)
        reason = ""
        if not observed:
            if s == "wren-metric-query":
                reason = f"route={origin.get('origin')}"
            elif s == "wren-clarify":
                reason = "no_verdict_no_stop_marker"
            elif s == "wren-perf-optimize":
                reason = "no_optimization_artifact"
            else:
                reason = "not_observed"
        out[s] = {"observed": observed, "evidence": evidence,
                  "files": files, "sop_files": sop_files, "reason": reason}
    return out


def build_process_manifest(*, messages, session_thread_id: str, sub_thread_id: str = "",
                           question_id: str = "", user_question: str = "",
                           db_name: str = "", status: str = "success",
                           producing_sql: str = "", root: str = "",
                           result: dict | None = None,
                           _recs: list[dict] | None = None) -> dict:
    """组装子任务 manifest（**只组装，不落盘**；写盘走 `write_process_artifacts`）。

    `_recs` 是内部优化用的已扫描轨迹（调用方扫一次就够，避免逐函数重复 O(n) 扫描）；
    外部调用不用传。
    """
    recs = _recs if _recs is not None else scan_tool_calls(messages)
    collected = collect_skill_artifacts(root, session_thread_id) if root else {
        "by_skill": {}, "layouts": {}, "artifacts": []}
    origin = judge_sql_origin(messages, producing_sql=producing_sql, _recs=recs)
    sop_skills = _sop_skills(collected) or _sop_skills_from_messages(messages)
    trace, warnings = _tool_trace(recs)

    cube_snap: dict = {}
    if producing_sql == "" or origin.get("origin") == SQL_ORIGIN_MIXED:
        try:
            from agent.utils.wren_call_extract import cube_snapshot
            cube_snap = cube_snapshot(messages) or {}
        except Exception as e:  # noqa: BLE001  fail-open（复算失败只是没有 SQL）
            _logger.debug("[process_audit] cube 快照失败: %s", e)

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "thread_id": session_thread_id,
        "sub_thread_id": sub_thread_id,
        "question_id": question_id,
        "user_question": user_question,
        "db_name": db_name,
        "status": status,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sql_origin": origin.get("origin"),
        "sql_origin_anchor": origin.get("anchor"),
        "sql_origin_confidence": origin.get("confidence"),
        "sql_origin_evidence": origin.get("evidence") or {},
        "final_sql": producing_sql or "",
        "final_sql_digest": _digest(producing_sql),
        "final_cube_spec": (cube_snap.get("spec") or {}) if cube_snap else {},
        "physical_sql_file": _physical_sql_pointer(result),
        "tool_trace": trace,
        "skills": _skills_section(collected, recs, origin, sop_skills),
        "layouts": collected.get("layouts") or {},
        "artifacts": collected.get("artifacts") or [],
        "warnings": warnings,
    }
    return manifest


def _sop_skills_from_messages(messages) -> set[str]:
    """从消息里的 write_file 落盘路径反推「哪些技能写了 SOP 产物」。

    磁盘扫描是主路径；这里只作补充（例如产物目录刚被保留策略清过，但消息还在）。
    """
    out: set[str] = set()
    for r in scan_tool_calls(messages):
        if not r["tool"].endswith("write_file"):
            continue
        path = str(r["args"].get("file_path") or r["args"].get("path") or "")
        if "skill_sop" not in path:
            continue
        for s in SKILL_DIR_NAMES:
            if f"/{s}/" in path:
                out.add(s)
    return out


def _physical_sql_pointer(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    v = result.get("dialect_sql_file")
    return str(v) if isinstance(v, str) and v else ""


def _write_json(path: Path, obj: Any) -> bool:
    """落盘一份 JSON（fail-open）。返回是否写入。"""
    try:
        blob = json.dumps(obj, ensure_ascii=False, default=str)
        if len(blob.encode("utf-8")) > _JSON_MAX_BYTES:
            _logger.info("[process_audit] %s 超 %d 字节，跳过落盘",
                         path.name, _JSON_MAX_BYTES)
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(blob, encoding="utf-8")
        return True
    except Exception as e:  # noqa: BLE001  审计旁路
        _logger.debug("[process_audit] 落盘 %s 失败: %s", path, e)
        return False


def _stem(question_id: str, sub_thread_id: str) -> str:
    q, s = (question_id or "")[:8], (sub_thread_id or "")[:8]
    return f"{q}-{s}" if q and s else (q or s or "")


def write_process_artifacts(*, root: str, session_thread_id: str, sub_thread_id: str = "",
                            messages, question_id: str = "", user_question: str = "",
                            db_name: str = "", status: str = "success",
                            producing_sql: str = "", result: dict | None = None) -> dict:
    """写 manifest（+ 成功时另写 routing / cube_summary）；返回 VFS 指针字典。

    **一次调用写完**：调用点只需 3 行，fail-open 包装也只有一处。返回
    ``{"manifest": vfs, "routing": vfs, "cube_summary": vfs}``，失败项为空串。

    幂等：文件名按 `{qid8}-{sub8}` 稳定（不用序号），反复 check 同名覆盖 ——
    序号命名会让每次 check 堆一个新文件。
    """
    out = {"manifest": "", "routing": "", "cube_summary": ""}
    try:
        if not root or not session_thread_id:
            return out
        recs = scan_tool_calls(messages)
        origin = judge_sql_origin(messages, producing_sql=producing_sql, _recs=recs)
        base = Path(root) / "nl2sql_process_data" / session_thread_id
        stem = _stem(question_id, sub_thread_id)

        collected = collect_skill_artifacts(root, session_thread_id)
        sop_skills = _sop_skills(collected) or _sop_skills_from_messages(messages)
        manifest = build_process_manifest(
            messages=messages, session_thread_id=session_thread_id,
            sub_thread_id=sub_thread_id, question_id=question_id,
            user_question=user_question, db_name=db_name, status=status,
            producing_sql=producing_sql, root=root, result=result, _recs=recs,
        )

        if status == "success":
            routing = build_routing_artifact(recs, origin, sop_skills)
            if _write_json(base / "wren-orchestrator" / f"_routing-{stem}.json", routing):
                out["routing"] = _vfs(f"{session_thread_id}/wren-orchestrator/_routing-{stem}.json")
            summary = build_cube_summary(recs)
            if summary and _write_json(
                base / "wren-metric-query" / f"_cube_summary-{stem}.json", summary
            ):
                out["cube_summary"] = _vfs(
                    f"{session_thread_id}/wren-metric-query/_cube_summary-{stem}.json")

        # 产物索引里补上刚落盘的合成件，再写 manifest（顺序不能反）
        collected = collect_skill_artifacts(root, session_thread_id)
        manifest["skills"] = _skills_section(collected, recs, origin, sop_skills)
        manifest["artifacts"] = collected.get("artifacts") or []

        name = f"manifest-{stem}.json" if stem else "manifest.json"
        if _write_json(base / LAYOUT_MANIFEST / name, manifest):
            out["manifest"] = _vfs(f"{session_thread_id}/{LAYOUT_MANIFEST}/{name}")
    except Exception as e:  # noqa: BLE001  审计是旁路，绝不影响查询主流程
        _logger.debug("[process_audit] 写审计产物失败: %s", e)
    return out
