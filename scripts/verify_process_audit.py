# -*- coding: utf-8 -*-
"""中间产物审计 + SQL 来源标记的契约验证（离线，无需后端/MCP/网络）。

**要关的三个口子**

1. **目录轴有两套写法**：落盘目录取 `langfuse_span._resolve_display_skill` 的返回值，
   它在共享工具 `dry_run` 遇到「线程活动 skill 过期」时会回退成**启发式族名**
   （`sql-generation`）；而读者 `check_progress._backfill_process_data_sql` 曾**写死**
   读 `.../sql-generation/`。两套只在「活动 skill 恰好过期」时才碰巧对上 ⇒ 回填
   **常年静默 0**（它本身 fail-open，不报错、不打日志，所以没人发现）。
   修法：`normalize_skill_dir` 让**目录名恒等于技能名**，读者改为**跨目录扫描**。

2. **SQL 来源说不出来**：此前只有 `sql_kind="cube"`，且「手写」靠键缺席表达 ⇒
   **混合路径（Cube 出指标主体 + 模型手写外层）与纯手写长得一模一样**，而这恰是最
   需要区分的一态。修法：`judge_sql_origin` 从工具轨迹确定性判四态（含混合）。

3. **有技能没有产物**：`wren-orchestrator`（零工具调用的纯推理）与
   `wren-metric-query`（走手写时本就没调 `query_cube`）此前在审计目录里**完全不存在**，
   分不清「没产物」与「跑漏了」。修法：平台**确定性合成** + manifest 的 skills 一节
   **七个键恒在**（缺产物也要在场并写明原因）。

本脚本断言：目录轴规范化、来源四态（含两条负对照）、manifest schema、每技能产物
（含缺席负对照）、fail-open、回填跨目录（含旧实现漏改的负对照）、报告那一行。

⚠️ 末尾那组只校验**本地文件**：线上跑的是发版镜像里的 `src`。生产对照要靠
`docker exec` 看 `/app/data/workspace/nl2sql_process_data/` 下的真实产物。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_process_audit.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import tempfile

# 与仓内 verify 脚本同款：先钉临时根，避免任何落盘写进真实工作区。
_TMP_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-processaudit-")
os.environ.setdefault("AGENT_DATA_ROOT", _TMP_ROOT)

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
_SRC = _ROOT / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from langchain_core.messages import ToolMessage  # noqa: E402

from agent.middlewares import langfuse_span as ls  # noqa: E402
from agent.tools import report_builder as rb  # noqa: E402
from agent.utils import process_audit as pa  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, extra: object = "") -> bool:
    results.append((bool(cond), label))
    print(f"  {OK if cond else NG}  {label}" + (f"  [{extra}]" if extra != "" else ""))
    return bool(cond)


# ══════════════════════════════════════════════════════════════════════
# fixtures
# ══════════════════════════════════════════════════════════════════════
class Trace:
    """合成工具轨迹（**dict 形态** —— 与 HTTP 拿回的 LangGraph state 同形）。

    真实工具名带库前缀（`wrenai_WIT_dry_run`）：短路匹配靠后缀，用裸名会把
    `_short_tool` / `attribute_tool_skill` 的整条查表路径绕过去，测不到真东西。
    """

    def __init__(self) -> None:
        self.msgs: list[dict] = []
        self._n = 0

    def call(self, name: str, args: dict | None = None,
             result: object = None, error: bool = False) -> str:
        self._n += 1
        cid = f"c{self._n}"
        self.msgs.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"name": name, "args": args or {}, "id": cid}],
        })
        if result is not None or error:
            if isinstance(result, str) or result is None:
                content = result or ""
            else:
                content = json.dumps(result, ensure_ascii=False)
            msg: dict = {"role": "tool", "name": name, "tool_call_id": cid,
                         "content": content}
            if error:
                msg["content"] = f"Error executing tool {name}: boom"
                msg["status"] = "error"
            self.msgs.append(msg)
        return cid


_CUBE_SQL = ("SELECT a.id, SUM(b.amount) AS amt FROM cube_view a "
             "JOIN b ON a.id = b.id GROUP BY a.id")
_FINAL_MIXED = _CUBE_SQL + " ORDER BY amt DESC"
_LLM_SQL = "SELECT dept_name, COUNT(*) AS c FROM employees GROUP BY dept_name"

_ROWS = {"columns": ["dept_name", "c"], "rows": [["研发", 12]], "row_count": 1}


def _t_pure_cube() -> list:
    t = Trace()
    t.call("wrenai_WIT_query_cube",
           {"cube": "workhour_analysis", "measures": ["total_hours"]}, result=_ROWS)
    return t.msgs


def _t_pure_llm() -> list:
    t = Trace()
    t.call("wrenai_WIT_get_context", {"question": "各部门人数"}, result="{...}")
    t.call("wrenai_WIT_dry_run", {"sql": _LLM_SQL}, result={"ok": True})
    t.call("wrenai_WIT_run_sql", {"sql": _LLM_SQL, "limit": 100}, result=_ROWS)
    return t.msgs


def _t_mixed() -> list:
    """混合路径：Cube 先预览编译 SQL → 模型包一层外层 → 干跑 → 执行。"""
    t = Trace()
    t.call("wrenai_WIT_query_cube",
           {"cube": "workhour_analysis", "measures": ["total_hours"],
            "sql_only": True}, result={"sql": _CUBE_SQL})
    t.call("wrenai_WIT_dry_run", {"sql": _FINAL_MIXED}, result={"ok": True})
    t.call("wrenai_WIT_run_sql", {"sql": _FINAL_MIXED, "limit": 100}, result=_ROWS)
    return t.msgs


def _t_metadata_only() -> list:
    """负对照 A：只**看**过 cube（list_cubes / describe_cube），SQL 是手写的。"""
    t = Trace()
    t.call("wrenai_WIT_list_cubes", {}, result={"cubes": ["workhour_analysis"]})
    t.call("wrenai_WIT_describe_cube", {"cube": "workhour_analysis"}, result="{...}")
    t.call("wrenai_WIT_dry_run", {"sql": _LLM_SQL}, result={"ok": True})
    t.call("wrenai_WIT_run_sql", {"sql": _LLM_SQL, "limit": 100}, result=_ROWS)
    return t.msgs


def _t_cube_after_anchor() -> list:
    """负对照 B：cube 调用在锚点**之后**（先执行，再拿 cube 复核）。"""
    t = Trace()
    t.call("wrenai_WIT_dry_run", {"sql": _LLM_SQL}, result={"ok": True})
    t.call("wrenai_WIT_run_sql", {"sql": _LLM_SQL, "limit": 100}, result=_ROWS)
    t.call("wrenai_WIT_query_cube",
           {"cube": "workhour_analysis", "measures": ["total_hours"]}, result=_ROWS)
    return t.msgs


def _t_preview_only() -> list:
    t = Trace()
    t.call("wrenai_WIT_query_cube",
           {"cube": "workhour_analysis", "measures": ["total_hours"],
            "sql_only": True}, result={"sql": _CUBE_SQL})
    return t.msgs


def _t_full_loop() -> list:
    """跑满主循环的合成轨迹（含澄清/性能优化的 SKILL.md 契约产物不在此造，见 D 组）。"""
    t = Trace()
    t.call("wrenai_WIT_get_context", {"question": "各部门工时"}, result="{...}")
    t.call("wrenai_WIT_recall_queries", {"question": "各部门工时"}, result="[]")
    t.call("wrenai_WIT_query_cube",
           {"cube": "workhour_analysis", "measures": ["total_hours"],
            "sql_only": True}, result={"sql": _CUBE_SQL})
    t.call("wrenai_WIT_query_cube",
           {"cube": "workhour_analysis", "measures": ["total_hours"]}, result=_ROWS)
    t.call("wrenai_WIT_dry_run", {"sql": _FINAL_MIXED}, result={"ok": True})
    t.call("wrenai_WIT_run_sql", {"sql": _FINAL_MIXED, "limit": 100}, result=_ROWS)
    return t.msgs


# ══════════════════════════════════════════════════════════════════════
# A. 目录轴恒等于技能名
# ══════════════════════════════════════════════════════════════════════
def a_directory_axis() -> None:
    print("\n== A. 目录轴恒等于技能名（族名绝不可成为目录名） ==")
    check(pa.normalize_skill_dir("sql-generation", "sql-generation") == "wren-sql-author",
          "族名回退 → 规范 owner：(\"sql-generation\",\"sql-generation\") → wren-sql-author",
          pa.normalize_skill_dir("sql-generation", "sql-generation"))
    check(pa.normalize_skill_dir("wren-retrieve", "cube-query") == "wren-retrieve",
          "真实技能名不被启发式覆盖（skill 优先于 heuristic）")
    check(pa.normalize_skill_dir("sql-execution", "sql-execution") == "wren-execution",
          "(\"sql-execution\",\"sql-execution\") → wren-execution")
    check(pa.normalize_skill_dir("cube-query", "cube-query") == "wren-metric-query",
          "(\"cube-query\",\"cube-query\") → wren-metric-query")
    # 展示名是垃圾但**族名认得** ⇒ 回退到该族的规范 owner（这是修「族名目录」的那条路：
    # 名字不可信时以族为准，而不是把不可信的名字当目录名建出去）
    check(pa.normalize_skill_dir("totally-unknown", "cube-query") == "wren-metric-query",
          "展示名未知 + 族名认得 → 该族规范 owner（不拿未知名建目录）",
          pa.normalize_skill_dir("totally-unknown", "cube-query"))
    for bad, heur in (("sql-generation", "brand-new-family"),
                      ("totally-unknown", "brand-new-family"),
                      ("", ""), (None, None)):
        check(pa.normalize_skill_dir(bad, heur) == "",
              f"未知族/空值 → 空串（宁可不落盘）：({bad!r}, {heur!r})",
              pa.normalize_skill_dir(bad, heur))

    # 族名永远不会成为目录名 —— 这条断言是「两套目录轴」不再复发的机械保证
    heur_vals = set(pa.HEURISTIC_TO_SKILL.values()) - {""}
    fam_vals = set(ls.TOOL_SKILL_MAP.values())
    check(bool(fam_vals) and bool(heur_vals), "  （前置）两侧取值都非空，断言非空转",
          f"{sorted(heur_vals)} / {len(fam_vals)} 族")
    check(not (heur_vals & fam_vals),
          "HEURISTIC_TO_SKILL 的值 ∩ 启发式族名 == ∅ ⇒ 规范化后不可能建出族名目录",
          sorted(heur_vals & fam_vals))

    # 每个启发式族都被显式映射过（新族名冒出来时这里失败，而不是静默不落盘）
    unmapped = sorted(fam_vals - set(pa.HEURISTIC_TO_SKILL))
    check(not unmapped,
          f"TOOL_SKILL_MAP 的 {len(fam_vals)} 个族全部在 HEURISTIC_TO_SKILL 里有交待",
          unmapped)

    # 盘上目录 == 常量表 == frontmatter name（拼写漂移由测试抓，不靠 import 抓）
    skills_dir = _SRC / "agent" / "shared" / "skills" / "nl2sql"
    on_disk = sorted(p.name for p in skills_dir.iterdir() if p.is_dir())
    check(on_disk == sorted(pa.SKILL_DIR_NAMES),
          f"SKILL_DIR_NAMES == 盘上目录集合（{len(on_disk)} 个）",
          sorted(set(on_disk) ^ set(pa.SKILL_DIR_NAMES)) or "")
    fm_bad = []
    for s in pa.SKILL_DIR_NAMES:
        skill_md = skills_dir / s / "SKILL.md"
        if not skill_md.is_file():
            fm_bad.append(f"{s}: 无 SKILL.md")
            continue
        head = skill_md.read_text(encoding="utf-8")[:400]
        name_line = next((ln for ln in head.splitlines() if ln.startswith("name:")), "")
        got = name_line.split(":", 1)[1].strip().strip("\"'") if name_line else ""
        if got != s:
            fm_bad.append(f"{s} != {got!r}")
    check(not fm_bad, "每个技能目录名 == 自己 SKILL.md frontmatter 的 name", fm_bad)

    owners = {o for v in ls._TOOL_OWNER_SKILLS.values() for o in v}
    check(owners and owners <= set(pa.SKILL_DIR_NAMES),
          f"_TOOL_OWNER_SKILLS 的全部 owner ⊆ SKILL_DIR_NAMES（{len(owners)} 个）",
          sorted(owners - set(pa.SKILL_DIR_NAMES)) or "")

    # 归属复算：唯一归属走查表，共享工具（dry_run 五个 owner）如实标 derived
    check(pa.attribute_tool_skill("wrenai_WIT_query_cube") == ("wren-metric-query", "unique_owner"),
          "query_cube → wren-metric-query / unique_owner")
    check(pa.attribute_tool_skill("wrenai_WIT_dry_run") == ("wren-sql-author", "derived"),
          "dry_run（5 owner）→ 规范 owner + derived（不假装确定）",
          pa.attribute_tool_skill("wrenai_WIT_dry_run"))
    check(pa.attribute_tool_skill("wrenai_WIT_write_file") == ("", "derived"),
          "artifact-write 族无目录语义 → 空技能（文件类产物走 layout 索引）")


# ══════════════════════════════════════════════════════════════════════
# B. 来源四态 + 两条负对照
# ══════════════════════════════════════════════════════════════════════
def b_sql_origin() -> None:
    print("\n== B. SQL 来源判决（含两条负对照） ==")
    o = pa.judge_sql_origin(_t_pure_cube(), "")
    check(o["origin"] == pa.SQL_ORIGIN_CUBE, "纯 Cube 取数（无 run_sql）→ cube_metric", o["origin"])
    check(o["anchor"] == "cube" and o["confidence"] == "strong", "  anchor=cube / confidence=strong")
    check(o["evidence"]["cube_data_calls"] == 1, "  evidence 记 1 次带数据 cube 调用")

    o = pa.judge_sql_origin(_t_pure_llm(), _LLM_SQL)
    check(o["origin"] == pa.SQL_ORIGIN_LLM, "纯手写（无 cube 调用）→ llm_from_schema", o["origin"])
    check(o["evidence"]["reason"] == "no_cube_call", "  reason=no_cube_call", o["evidence"]["reason"])

    o = pa.judge_sql_origin(_t_mixed(), _FINAL_MIXED)
    check(o["origin"] == pa.SQL_ORIGIN_MIXED,
          "混合（query_cube 预览 → 模型包外层 → run_sql）→ cube_metric+llm_outer", o["origin"])
    check(o["origin"] != pa.SQL_ORIGIN_LLM, "  **与纯手写区分开**（这正是本次要修的那一态）")
    check(o["evidence"]["cube_sql_in_final"] is True,
          "  次要信号：编译 SQL 被最终 SQL 包含 ⇒ cube_sql_in_final=True")
    check(o["confidence"] == "strong", "  锚点定位成功 ⇒ confidence=strong")

    o = pa.judge_sql_origin(_t_metadata_only(), _LLM_SQL)
    check(o["origin"] == pa.SQL_ORIGIN_LLM,
          "负对照 A：只 list_cubes/describe_cube（纯检索）+ 手写 SQL → llm_from_schema", o["origin"])
    check("list_cubes" in o["evidence"]["metadata_only_tools"]
          and "describe_cube" in o["evidence"]["metadata_only_tools"],
          "  两个纯检索工具如实记进 metadata_only_tools，不算来源证据",
          o["evidence"]["metadata_only_tools"])
    check(o["evidence"]["cube_calls"] == 0, "  它们根本不计入 cube_calls")

    o = pa.judge_sql_origin(_t_cube_after_anchor(), _LLM_SQL)
    check(o["origin"] == pa.SQL_ORIGIN_LLM,
          "负对照 B：cube 调用在锚点**之后** → llm_from_schema（次序不可倒推）", o["origin"])
    check(o["evidence"]["reason"] == "no_cube_call_before_anchor", "  reason=no_cube_call_before_anchor",
          o["evidence"]["reason"])

    o = pa.judge_sql_origin(_t_preview_only(), "")
    check(o["origin"] == pa.SQL_ORIGIN_UNKNOWN, "仅预览无执行 → unknown（不猜）", o["origin"])
    check(o["evidence"]["reason"] == "preview_only_no_execution", "  reason=preview_only_no_execution")

    o = pa.judge_sql_origin(_t_pure_llm(), _FINAL_MIXED)
    check(o["origin"] == pa.SQL_ORIGIN_LLM,
          "锚点对不上任何一次执行（抽取的 SQL 不在轨迹里）→ llm_from_schema",
          o["origin"])
    check(o["evidence"]["anchor_found"] is False, "  anchor_found=False，confidence 降级",
          o["confidence"])

    for garbage in (None, [], "not-a-list", 12345, [None, 42, {"role": "assistant"}]):
        try:
            r = pa.judge_sql_origin(garbage, "")
            ok = r.get("origin") == pa.SQL_ORIGIN_UNKNOWN
            err = ""
        except Exception as e:  # noqa: BLE001
            ok, err = False, f"{type(e).__name__}: {e}"
        check(ok, f"垃圾输入不抛且判 unknown：{type(garbage).__name__}", err)


# ══════════════════════════════════════════════════════════════════════
# C/D. manifest 与「每个技能都有产物」
# ══════════════════════════════════════════════════════════════════════
_SID = "sess-thread-0001"
_QID = "trace-abcdef123456"
_SUB = "sub-thread-7777"


def _real(vfs: str) -> pathlib.Path:
    rel = vfs.split("/workspace/nl2sql_process_data/", 1)[-1]
    return pathlib.Path(_TMP_ROOT) / "nl2sql_process_data" / rel


def _seed_sop(skill: str, name: str = "verdict.md") -> pathlib.Path:
    """造一份「模型按 SKILL.md 契约自写」的 SOP 产物。"""
    p = (pathlib.Path(_TMP_ROOT) / "nl2sql_process_data" / _SID
         / "skill_sop" / skill / name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# 技能产物\n", encoding="utf-8")
    return p


def _write(messages, producing_sql: str, *, result: dict | None = None,
           status: str = "success") -> dict:
    return pa.write_process_artifacts(
        root=_TMP_ROOT, session_thread_id=_SID, sub_thread_id=_SUB,
        messages=messages, question_id=_QID, user_question="各部门工时对比",
        db_name="WIT", status=status, producing_sql=producing_sql,
        result=result or {})


def c_manifest() -> None:
    print("\n== C. manifest schema 与幂等 ==")
    msgs = _t_full_loop()
    ptrs = _write(msgs, _FINAL_MIXED)
    man_path = _real(ptrs["manifest"])
    check(man_path.is_file(), "manifest 已落盘（VFS 指针可达真实文件）", ptrs["manifest"])
    man = json.loads(man_path.read_text(encoding="utf-8"))

    need = {"schema_version", "thread_id", "sub_thread_id", "question_id",
            "user_question", "db_name", "status", "ts", "sql_origin",
            "sql_origin_evidence", "final_sql", "final_sql_digest",
            "final_cube_spec", "physical_sql_file", "tool_trace", "skills",
            "layouts", "artifacts", "warnings"}
    check(need <= set(man), f"顶层键齐全（{len(need)} 个）", sorted(need - set(man)) or "")
    check(man["sql_origin"] == pa.SQL_ORIGIN_MIXED, "manifest 的 sql_origin 与判决一致",
          man["sql_origin"])
    check(man["sub_thread_id"] == _SUB and man["question_id"] == _QID
          and man["db_name"] == "WIT", "归属字段如实落（子线程/问题/库）")
    check(man["final_sql"] == _FINAL_MIXED and man["final_sql_digest"], "  final_sql + 指纹在场")

    # 「每个技能一节」的可断言形式 —— 静默缺席不可能发生
    check(set(man["skills"]) == set(pa.SESSION_SKILLS),
          f"set(skills) == set(SESSION_SKILLS)（{len(pa.SESSION_SKILLS)} 节恒在）",
          sorted(set(man["skills"]) ^ set(pa.SESSION_SKILLS)) or "")

    check(bool(man["artifacts"]), f"artifacts 非空（{len(man['artifacts'])} 条）")
    missing = [p for p in man["artifacts"] if not _real(p).exists()]
    check(not missing, "artifacts 里每条路径都真实存在（1:1 可复查）", missing[:3])
    check(set(man["layouts"]) == {"tool_dump", "skill_sop", "wren_plan",
                                  "query_result", "_manifest"},
          "四个子布局 + manifest 布局全部索引到（不合并布局）",
          sorted(man["layouts"]))

    tr = man["tool_trace"]
    check(len(tr) == len(msgs) // 2 and all(
        {"seq", "msg_index", "tool", "family", "skill", "ok", "sql_digest",
         "cube_spec", "result_kind"} <= set(r) for r in tr),
        f"tool_trace 每条含摘要+指纹（{len(tr)} 条），正文不入 manifest")
    check(all(r["skill_owner"] in ("unique_owner", "derived", "") for r in tr),
          "  归属依据只有 unique_owner / derived（无第三种来源）")

    n_before = len(list((pathlib.Path(_TMP_ROOT) / "nl2sql_process_data" / _SID).rglob("*")))
    ptrs2 = _write(msgs, _FINAL_MIXED)
    n_after = len(list((pathlib.Path(_TMP_ROOT) / "nl2sql_process_data" / _SID).rglob("*")))
    check(ptrs2["manifest"] == ptrs["manifest"], "重复写幂等：manifest 文件名稳定（同名覆盖）")
    check(n_before == n_after, f"重复写幂等：文件数不变（{n_before}）")

    size = man_path.stat().st_size
    check(size < 256 * 1024, f"manifest 体积 {size} 字节 < 256KB（只放摘要与指纹）")


def d_every_skill_has_artifact() -> None:
    print("\n== D. 每个技能都有产物（缺产物也要在场并说明原因） ==")
    _seed_sop("wren-clarify", "verdict.md")
    _seed_sop("wren-perf-optimize", "perf-review.md")
    ptrs = _write(_t_full_loop(), _FINAL_MIXED)
    base = pathlib.Path(_TMP_ROOT) / "nl2sql_process_data" / _SID

    check(_real(ptrs["routing"]).is_file(),
          "wren-orchestrator 的路由判定由平台复算落盘", ptrs["routing"])
    routing = json.loads(_real(ptrs["routing"]).read_text(encoding="utf-8"))
    check(routing["route"] == pa.SQL_ORIGIN_MIXED,
          "  routing.route 逐字取同一份判决（不写第二套判据）", routing["route"])
    check(routing["model_self_report"] is None,
          "  显式 model_self_report=null（声明这不是模型自述）")
    check(routing["decided_by"] == "platform-deterministic", "  decided_by=platform-deterministic")
    check(all(routing["phases"].get(k) for k in
              ("retrieve", "clarify", "metric_query", "sql_author", "perf_optimize",
               "execution")),
          "  六个阶段全部 observed（合成轨迹跑满主循环）", routing["phases"])
    check("query_cube" in routing["tool_sequence"] and "run_sql" in routing["tool_sequence"],
          "  tool_sequence 如实记录调用次序", routing["tool_sequence"])

    check(_real(ptrs["cube_summary"]).is_file(),
          "wren-metric-query 的口径汇总（用了什么 cube/measure、取数还是预览）",
          ptrs["cube_summary"])
    summ = json.loads(_real(ptrs["cube_summary"]).read_text(encoding="utf-8"))
    check(summ["used"] is True and summ["cube"] == "workhour_analysis"
          and summ["data_bearing_calls"] == 1 and summ["preview_only"] is False,
          "  汇总内容与轨迹一致（1 次取数 + 1 次预览，末次取数胜出）",
          {k: summ[k] for k in ("cube", "data_bearing_calls", "preview_only")})
    check(len(summ["calls"]) == 2, "  两次 cube 调用逐条留痕")

    man = json.loads(_real(ptrs["manifest"]).read_text(encoding="utf-8"))
    for s in ("wren-orchestrator", "wren-metric-query", "wren-sql-author",
              "wren-execution", "wren-retrieve"):
        check(man["skills"][s]["observed"], f"  skills[{s}].observed == True",
              man["skills"][s]["files"][:1])
    check(man["skills"]["wren-clarify"]["observed"]
          and man["skills"]["wren-perf-optimize"]["observed"],
          "  clarify / perf-optimize 靠 SKILL.md 契约产物（模型自写）被观测到")

    # 负对照：只走手写 → 不建假目录，manifest 如实说缺席原因
    t = Trace()
    t.call("wrenai_WIT_get_context", {"question": "各部门人数"}, result="{...}")
    t.call("wrenai_WIT_dry_run", {"sql": _LLM_SQL}, result={"ok": True})
    t.call("wrenai_WIT_run_sql", {"sql": _LLM_SQL, "limit": 100}, result=_ROWS)
    p2 = pa.write_process_artifacts(
        root=_TMP_ROOT, session_thread_id="sess-thread-0002", sub_thread_id=_SUB,
        messages=t.msgs, question_id=_QID, user_question="各部门人数",
        db_name="WIT", status="success", producing_sql=_LLM_SQL, result={})
    check(p2["cube_summary"] == "",
          "负对照：纯手写不写 cube 汇总（不建假目录、不编口径）", p2["cube_summary"])
    check(not (pathlib.Path(_TMP_ROOT) / "nl2sql_process_data" / "sess-thread-0002"
               / "wren-metric-query").exists(),
          "负对照：纯手写不建 wren-metric-query/ 目录")
    man2 = json.loads(_real(p2["manifest"]).read_text(encoding="utf-8"))
    mq = man2["skills"]["wren-metric-query"]
    check(mq["observed"] is False and mq["reason"] == f"route={pa.SQL_ORIGIN_LLM}",
          "负对照：该节**恒在**且如实写 observed=false + reason",
          {k: mq[k] for k in ("observed", "reason")})


def e_fail_open() -> None:
    print("\n== E. fail-open：审计旁路绝不影响主流程 ==")
    out = pa.write_process_artifacts(
        root="", session_thread_id="", messages=None, producing_sql="",
    )
    check(out == {"manifest": "", "routing": "", "cube_summary": ""},
          "空 root/空 session → 返回空指针，不抛")

    bad_root = pathlib.Path(_TMP_ROOT) / "a-file-not-a-dir"
    bad_root.write_text("x", encoding="utf-8")
    out = pa.write_process_artifacts(
        root=str(bad_root), session_thread_id=_SID, messages=[{"role": "user"}],
        producing_sql="SELECT 1",
    )
    check(set(out) == {"manifest", "routing", "cube_summary"},
          "落盘目标不可用（路径是文件）→ 仍返回结构完整的空指针，不抛", out)

    try:
        m = pa.build_process_manifest(messages=None, session_thread_id=_SID, root=str(bad_root))
        ok, err = set(m) >= {"skills", "tool_trace", "sql_origin"}, ""
    except Exception as e:  # noqa: BLE001
        ok, err = False, f"{type(e).__name__}: {e}"
    check(ok, "build_process_manifest 收到 None 消息也不抛", err)

    check(set(pa.collect_skill_artifacts("", "")) == {"by_skill", "layouts", "artifacts"},
          "collect_skill_artifacts 空入参返回结构完整的空结果")


# ══════════════════════════════════════════════════════════════════════
# F. 回填跨目录（含旧实现漏改的负对照）
# ══════════════════════════════════════════════════════════════════════
_DRY_SQL = "SELECT id FROM t WHERE x = 1"


def _seed_dump(sid: str, skill: str, fname: str, tool: str, sql: str) -> pathlib.Path:
    d = pathlib.Path(_TMP_ROOT) / "nl2sql_process_data" / sid / skill
    d.mkdir(parents=True, exist_ok=True)
    p = d / fname
    p.write_text(json.dumps({"tool": tool, "skill": skill,
                             "input": {"sql": sql}, "output": {}}), encoding="utf-8")
    return p


def f_backfill_cross_dir() -> None:
    print("\n== F. dry_run 回填：跨技能子目录扫描（旧实现漏改的那份） ==")
    from agent.subagents import check_progress as cp

    sid = "sess-backfill-0001"
    for skill_dir in (pathlib.Path(_TMP_ROOT) / "nl2sql_process_data" / sid
                      / "skill_sop" / "wren-clarify",):
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "v.md").write_text("x", encoding="utf-8")

    a = _seed_dump(sid, "wren-sql-author", "q_dry_run-1.json", "wrenai_WIT_dry_run", _DRY_SQL)
    b = _seed_dump(sid, "wren-perf-optimize", "q_dry_run-1.json", "wrenai_WIT_dry_run", _DRY_SQL)
    c = _seed_dump(sid, "wren-execution", "q_run_sql-1.json", "wrenai_WIT_run_sql", _FINAL_MIXED)
    d = _seed_dump(sid, "wren-execution", "q_dry_run-2.json", "wrenai_WIT_dry_run",
                   "SELECT other FROM t")
    e = _seed_dump(sid, "wren-orchestrator", "_routing-x.json", "platform:route", "")
    f = _seed_dump(sid, "wren-metric-query", "_cube_summary-x.json", "platform:cube", "")
    bad = pathlib.Path(_TMP_ROOT) / "nl2sql_process_data" / sid / "wren-sql-author" / "broken.json"
    bad.write_text("{ not json", encoding="utf-8")
    # 逐字节留底：下面按「一个字都没变」断言非 dry_run 文件未被触碰
    untouched = {p: p.read_bytes() for p in (c, e, f, d)}

    msgs = Trace()
    msgs.call("wrenai_WIT_dry_run", {"sql": _DRY_SQL}, result={"ok": True})
    msgs.call("wrenai_WIT_run_sql", {"sql": _FINAL_MIXED, "limit": 100}, result=_ROWS)

    _saved_sid, _saved_ws = cp._session_thread_id, ls._active_workspace_path
    cp._session_thread_id = lambda: sid
    ls._active_workspace_path = lambda: _TMP_ROOT
    try:
        n = cp._backfill_process_data_sql(msgs.msgs, _FINAL_MIXED)
    finally:
        cp._session_thread_id, ls._active_workspace_path = _saved_sid, _saved_ws

    check(n == 2, f"跨目录改写 2 份（wren-sql-author + wren-perf-optimize），实得 {n}", n)

    def _sql_of(p: pathlib.Path) -> str:
        return str(json.loads(p.read_text(encoding="utf-8"))["input"].get("sql") or "")

    check(_sql_of(a) == _FINAL_MIXED, "  wren-sql-author/ 的干跑文件已回填为产出 SQL")
    check(_sql_of(b) == _FINAL_MIXED,
          "  **wren-perf-optimize/** 的干跑文件也回填（旧实现写死单目录 ⇒ 漏改这一份）")
    check(json.loads(a.read_text(encoding="utf-8")).get("_backfilled") is True,
          "  回填留痕：_backfilled / _dry_run_sql / _final_sql")
    changed = [p.name for p, blob in untouched.items() if p.read_bytes() != blob]
    check(not changed,
          "  非 dry_run 文件与别的子任务的干跑文件**逐字节未动**（run_sql / _routing "
          "/ _cube_summary / 异 SQL 的 dry_run）", changed)
    check(json.loads(f.read_text(encoding="utf-8"))["tool"] == "platform:cube",
          "  平台合成件未被误改（tool 不是 dry_run 后缀）")
    check(bad.read_text(encoding="utf-8") == "{ not json",
          "  坏 JSON 被跳过，未被改写")

    _saved_sid, _saved_ws = cp._session_thread_id, ls._active_workspace_path
    cp._session_thread_id = lambda: sid
    ls._active_workspace_path = lambda: _TMP_ROOT
    try:
        n2 = cp._backfill_process_data_sql(msgs.msgs, _FINAL_MIXED)
    finally:
        cp._session_thread_id, ls._active_workspace_path = _saved_sid, _saved_ws
    check(n2 == 0, f"二次调用改写 0（幂等：已一致则跳过），实得 {n2}", n2)

    # 负对照：把旧实现的选择逻辑原样复现 —— 写死单目录，看看它漏掉几份
    def _old_selection() -> list:
        root = pathlib.Path(_TMP_ROOT) / "nl2sql_process_data" / sid
        skill_dir = root / "sql-generation"     # ← 旧实现里那个常量
        if not skill_dir.exists():
            return []
        return [p for p in sorted(skill_dir.glob("*.json"))
                if str(json.loads(p.read_text(encoding="utf-8")).get("tool") or "")
                .endswith("dry_run")]

    check(_old_selection() == [],
          "负对照：旧实现（写死 sql-generation/）在本轨迹下选中 0 份 —— 且它 fail-open 不报错")
    check(n == 2, "  ⇒ 新实现改的这 2 份正是旧实现**静默漏掉**的（本脚本存在的理由）")


# ══════════════════════════════════════════════════════════════════════
# G. 报告那一行
# ══════════════════════════════════════════════════════════════════════
class _Runtime:
    """`_build_report_coro` 只用到 runtime.state["messages"]。"""

    def __init__(self, messages: list) -> None:
        self.state = {"messages": messages}


def _check_msg(result_text: str, *, sql: str = "", cube_query: str = "",
               extra: dict | None = None) -> ToolMessage:
    obj: dict = {"status": "success", "thread_id": "t-1", "result": result_text}
    if sql:
        obj["sql"] = sql
    if cube_query:
        obj["cube_query"] = cube_query
    obj.update(extra or {})
    return ToolMessage(content=json.dumps(obj, ensure_ascii=False),
                       name="check_async_task", tool_call_id="call_check")


def _report_dir() -> pathlib.Path:
    from agent.workspace_manager import get_workspace_manager
    return pathlib.Path(get_workspace_manager().report_dir)


def _build(messages: list) -> str:
    rd = _report_dir()
    before = set(rd.glob("*.md")) if rd.is_dir() else set()
    asyncio.run(rb._build_report_coro(
        report_name="来源验证", analysis="解读文本", task_id="",
        runtime=_Runtime(messages)))
    new = sorted(set(_report_dir().glob("*.md")) - before)
    return new[-1].read_text(encoding="utf-8") if new else ""


def g_report_line() -> None:
    print("\n== G. 报告里的「SQL 生成来源」一行 ==")
    text = _build([_check_msg("各部门人数如下。", sql=_LLM_SQL,
                              extra={"sql_origin": "llm_from_schema"})])
    check("> SQL 生成来源：模型依据语义库 schema 手写" in text, "手写 → 报告出现对应来源行")
    check("## 2. 执行 SQL\n" in text, "  标题保持 `## N. 执行 SQL`（非混合不动标题）")

    text = _build([_check_msg("混合路径结果如下。", sql=_FINAL_MIXED,
                              extra={"sql_origin": "cube_metric+llm_outer"})])
    check("> SQL 生成来源：Cube 语义层出指标主体，模型手写外层（混合路径）" in text,
          "混合 → 来源行如实说明「主体 + 外层」")
    check("Cube 指标主体 + 模型手写外层" in text, "  节标题演进为「Cube 指标主体 + 模型手写外层」")

    text = _build([_check_msg("Cube 结果如下。", cube_query="cube: workhour_analysis",
                              extra={"sql_origin": "cube_metric"})])
    check("> SQL 生成来源：由 Cube 语义层的具名指标直接编译下发" in text,
          "纯 Cube（cube_query 分支）→ 同样标出来源")

    # 负对照：老 check 结果没有这个字段 ⇒ 报告与改动前逐字一致（不硬安一个来源）
    text = _build([_check_msg("老结果。", sql=_LLM_SQL)])
    check("SQL 生成来源" not in text, "负对照：无 sql_origin 字段 → 不出该行（老结果不被改版）")
    check("## 2. 执行 SQL\n" in text and "Cube 指标主体" not in text,
          "负对照：标题回到 `## N. 执行 SQL`")

    text = _build([_check_msg("说不出来源。", sql=_LLM_SQL, extra={"sql_origin": "unknown"})])
    check("SQL 生成来源" not in text, "负对照：unknown → 不说话（报告不许自己编来源）")

    check(rb._sql_origin_line(None) == "" and rb._sql_origin_line({}) == ""
          and rb._sql_origin_line("x") == "",
          "负对照：非 dict / 空 dict / 垃圾入参 → 空串，不抛")


# ══════════════════════════════════════════════════════════════════════
def main() -> None:
    a_directory_axis()
    b_sql_origin()
    c_manifest()
    d_every_skill_has_artifact()
    e_fail_open()
    f_backfill_cross_dir()
    g_report_line()

    print()
    passed = sum(1 for ok, _ in results if ok)
    print(f"{passed}/{len(results)} 通过"
          + ("" if passed == len(results) else "   ← 有失败"))
    if passed == len(results):
        print(f"（落盘根：{_TMP_ROOT}）")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
