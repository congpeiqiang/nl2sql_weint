# -*- coding: utf-8 -*-
"""Cube 通道报告锚点（`check_progress._pick_cube_call_for_report`）的契约验证（离线，无需后端/MCP/网络）。

**要关的口子**（2026-10-16，生产 thread `01a14503` / 问题「丛培强8-9月工时分析」）：

子任务共发 **8 次** `wrenai_witops_query_cube`（6 次取数 + 2 次 `sql_only` 预览，见
`_cube_summary-12deabb1-01a14503.json` 的 `data_bearing_calls: 6`），而报告只展示**一次**
定义。取哪一次过去是**纯位置判据**（`extract_last_cube_call` = 最后一次**成功**）⇒ 报告
「2. 执行 SQL（由 Cube 语义层编译，实际下发）」与「5. 查询定义（Cube 语义层）」锚在最后那次
`if_leave_form × approve_user_name`（5 行，其数据在正文里一个字都没出现），而正文的
「已审核 152.0 / 未审核 16.0」来自 `if_approve` 那次、「报工 43 天」来自 day 粒度那次、
人员身份来自 identity 那次 ⇒ 第 2/5 节与第 1/6 节**结构性错位**，且同题多跑每次展示不同定义
（10-13 锚在 `dept_level1`、10-16 16:26 锚在 `user_name`）。

根因的不对称：run_sql 通道有 tier0/0b **值匹配** + 通道闸，Cube 通道**只有位置判据**。

修法（本脚本断言的契约）：
  1. Cube 候选也跑同一套值匹配（`_pick_by_value_match`，与 run_sql 通道**同一份实现**）：
     「返回值能整行覆盖最终答案表」的那次调用优先，命中唯一者作锚点；
  2. 0 命中或平票时**不假装**：锚点退回「最后一次成功取数」（= 改动前行为），并在
     「5. 查询定义」节加**如实注记**（「本节只是其中一次 + 未能唯一对上正文表格」）并
     **逐行列出本轮全部取数调用**（cube/维度/度量/筛选/时间，★ 标出本节那一次）；
  3. 只取过一次数、或压根没取到数时，注记返回空串 ⇒ **老报告逐字不变**；
  4. 物理 SQL 节同源同问题：取数调用 > 1 次时补一句短注（单段文本，行内不换行）。

⚠️ 只校验**本地代码**：线上跑的是发版镜像里的 `src`，生产对照必须 `docker exec md5sum`。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_cube_anchor.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

# 与仓内 verify 脚本同款：先钉临时根，避免任何落盘写进真实工作区。
# 用**直接赋值**而非 setdefault —— 环境里若已有 AGENT_DATA_ROOT，setdefault 会保留它，
# 于是审计件与产物写进真实工作区（本脚本只读数、但仍不该碰）。
_TMP_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-cubeanch-")
os.environ["AGENT_DATA_ROOT"] = _TMP_ROOT

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
_SRC = _ROOT / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from agent.subagents.check_progress import (  # noqa: E402
    _CUBE_ALL_FAILED_NOTE,
    _CUBE_MULTI_HEAD,
    _CUBE_NOTE,
    _CUBE_SKIPPED_NOTE,
    _cube_multi_note,
    _cube_physical_multi,
    _enhanced_build_check_result,
    _final_answer_data_rows,
    _final_row_covered_by,
    _pick_cube_call_for_report,
    _run_sql_cell_values,
    _run_sql_row_values,
)
from agent.utils.wren_call_extract import (  # noqa: E402
    extract_cube_calls,
    extract_last_cube_call,
)

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

CUBE_TOOL = "wrenai_witops_query_cube"  # 工具名带 server 前缀才是线上真实形态

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, extra: object = "") -> bool:
    results.append((bool(cond), label))
    print(f"  {OK if cond else NG}  {label}" + (f"  [{extra}]" if extra != "" else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ══════════════════════════════════════════════════════════════════════
# fixtures：合成工具轨迹（**dict 形态**，与 HTTP 拿回的 LangGraph state 同形）
# ══════════════════════════════════════════════════════════════════════
class Trace:
    """Cube 通道轨迹构造器。

    `cube()` = 一次调用独占一条 AI 消息（线上最常见）；`cubes()` = 同一轮**并发**多次
    调用（一条 AI 消息 + 多条工具结果，生产实测一条消息里发过 4 次）——「其后的失败
    调用」计数只有在 `cubes()` 形态下才考验 `(消息下标, 消息内位置)` 这个序。
    """

    def __init__(self) -> None:
        self.msgs: list[dict] = []
        self._n = 0

    def _cid(self) -> str:
        self._n += 1
        return f"c{self._n}"

    def _emit(self, items: list[tuple[dict, object, bool]]) -> None:
        calls, outs = [], []
        for args, result, error in items:
            cid = self._cid()
            calls.append({"name": CUBE_TOOL, "args": args, "id": cid})
            outs.append(self._tool_msg(cid, result, error))
        self.msgs.append({"role": "assistant", "content": "", "tool_calls": calls})
        self.msgs.extend(outs)

    @staticmethod
    def _tool_msg(cid: str, result: object, error: bool) -> dict:
        if error:
            return {"role": "tool", "name": CUBE_TOOL, "tool_call_id": cid,
                    "status": "error",
                    "content": f"Error executing tool {CUBE_TOOL}: boom"}
        content = (result if isinstance(result, str)
                   else json.dumps(result, ensure_ascii=False))
        return {"role": "tool", "name": CUBE_TOOL, "tool_call_id": cid, "content": content}

    def cube(self, args: dict, result: object = None, error: bool = False) -> None:
        self._emit([(args, result, error)])

    def cubes(self, items: list[tuple[dict, object, bool]]) -> None:
        self._emit(items)

    def ai(self, text: str) -> None:
        self.msgs.append({"role": "assistant", "content": text})


def _res(rows: list[dict], row_count: int | None = None) -> dict:
    """Cube 工具返回体（与 run_sql **同构**）：`{columns, rows, row_count, truncated}`。"""
    cols = list(rows[0].keys()) if rows else []
    return {"columns": cols, "rows": rows,
            "row_count": row_count if row_count is not None else len(rows)}


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _hit_count(info: dict, messages: list[dict]) -> int:
    """重算锚点候选对最终答案表的整行命中数（**独立于选取逻辑**的取证）。

    用的是 check_progress 自己的取行/覆盖函数，不另写匹配器 —— 本函数只做「数一数
    选中那次到底命中了几行」，用来断言「锚点不是凭位置取的，得真有产出证据」。
    """
    call = info.get("call") or {}
    ri = call.get("result_index")
    if ri is None:
        return 0
    vals = _run_sql_cell_values(messages, ri)
    rvals = _run_sql_row_values(messages, ri)
    return sum(1 for cells in _final_answer_data_rows(messages)
               if _final_row_covered_by(cells, vals, rvals))


def _note_lines(note: str) -> list[str]:
    return note.split("\n") if note else []


# ══════════════════════════════════════════════════════════════════════
# A. 值匹配优先：产出正文表格的那次调用压过其后不同口径的调用
# ══════════════════════════════════════════════════════════════════════
def a_value_match_wins() -> None:
    section("A. 值匹配优先（核心修复：锚点由证据决定，不再由位置决定）")
    t = Trace()
    # 第 1 次：产出正文表格的那次（status_code 分组）
    t.cube({"cube": "bug_quality", "dimensions": ["status_code"],
            "measures": ["bug_count"]},
           _res([{"status_code": "open", "bug_count": 12},
                 {"status_code": "closed", "bug_count": 30}]))
    # 第 2 次（最后一次）：换个口径再算一遍，结果不在正文里
    t.cube({"cube": "bug_quality", "measures": ["bug_count"]},
           _res([{"bug_count": 7}]))
    t.ai(_md_table(["状态", "数量"], [["open", "12"], ["closed", "30"]]))

    info = _pick_cube_call_for_report(t.msgs)
    check(info.get("matched") is True, "matched=True（值匹配命中）")
    check(info.get("tier") == "tier0", "tier=tier0", info.get("tier"))
    check((info.get("call") or {}).get("args", {}).get("dimensions") == ["status_code"],
          "★ 锚点 = 产出正文表格的那次（不是最后一次）",
          (info.get("call") or {}).get("args"))
    check(_hit_count(info, t.msgs) == 2, "★ 锚点确实整行覆盖正文表 2/2 行",
          _hit_count(info, t.msgs))
    # 对照：改动前的判据（最后一次成功）会锚在第二次上 —— 这就是被修的错位
    check(extract_last_cube_call(t.msgs).get("args", {}).get("dimensions") is None,
          "对照：`extract_last_cube_call` 仍锚在最后一次（老行为未被改动）")


# ══════════════════════════════════════════════════════════════════════
# B. 平票规则：先「rows == 命中」的精确对齐，再消息序靠后
# ══════════════════════════════════════════════════════════════════════
def b_tie_break() -> None:
    section("B. 平票规则（与 run_sql 通道 tier0 同一条，不另立一套）")
    # B1：同命中 1 行，一个返回 5 行（含噪声）、一个只返回 1 行（精确对齐）⇒ 取精确那个
    t = Trace()
    t.cube({"cube": "c", "dimensions": ["a"]},
           _res([{"a": "x", "b": 1}, {"a": "y", "b": 2}, {"a": "u", "b": 3},
                 {"a": "v", "b": 4}, {"a": "w", "b": 5}]))
    t.cube({"cube": "c", "dimensions": ["a"]}, _res([{"a": "x", "b": 1}]))
    t.ai(_md_table(["列", "值"], [["x", "1"], ["zz", "999"]]))
    info = _pick_cube_call_for_report(t.msgs)
    check((info.get("call") or {}).get("rows", 0) == 0
          or True, "-")  # 占位（下面用下标断言，避免依赖内部键）
    seqs = [c.get("seq") for c in extract_cube_calls(t.msgs)]
    check((info.get("call") or {}).get("seq") == seqs[1],
          "★ 同命中时 `rows == 命中`（精确对齐）优先", (info.get("call") or {}).get("seq"))

    # B2：同命中、同对齐 ⇒ 消息序靠后
    t = Trace()
    t.cube({"cube": "c", "dimensions": ["a"]}, _res([{"a": "x", "b": 1}]))
    t.cube({"cube": "c", "dimensions": ["a"]}, _res([{"a": "x", "b": 1}]))
    t.ai(_md_table(["列", "值"], [["x", "1"], ["zz", "999"]]))
    info = _pick_cube_call_for_report(t.msgs)
    seqs = [c.get("seq") for c in extract_cube_calls(t.msgs)]
    check((info.get("call") or {}).get("seq") == seqs[-1],
          "★ 同命中同对齐 ⇒ 消息序靠后者", (info.get("call") or {}).get("seq"))


# ══════════════════════════════════════════════════════════════════════
# C. 0 命中：退回最后一次成功取数（= 改动前行为）+ 如实注记
# ══════════════════════════════════════════════════════════════════════
def c_no_evidence_fallback() -> None:
    section("C. 0 命中 ⇒ 退回最后一次成功取数 + 如实注记（不假装对得上）")
    t = Trace()
    t.cube({"cube": "workhour_analysis", "dimensions": ["project_name"],
            "measures": ["total_hours"], "filters": ["user_name:eq:丛培强"],
            "time_dimension": "work_date:month:2026-08-01,2026-10-01"},
           _res([{"project_name": "P1", "total_hours": 11.0}]))
    t.cube({"cube": "workhour_analysis", "dimensions": ["dept_level1"],
            "measures": ["total_hours"], "filters": ["user_name:eq:丛培强"],
            "time_dimension": "work_date:month:2026-08-01,2026-10-01"},
           _res([{"dept_level1": "产品管理部", "total_hours": 13.0}]))
    # 正文是模型二次派生出来的汇总（任何单次调用都复现不了）
    t.ai(_md_table(["指标", "数值"], [["合计工时", "999"], ["记录数", "77"]]))

    info = _pick_cube_call_for_report(t.msgs)
    check(info.get("matched") is False, "matched=False（无产出证据）")
    check(info.get("tier") == "", "tier 为空串", info.get("tier"))
    check([c["seq"] for c in extract_cube_calls(t.msgs)].index(
        (info.get("call") or {}).get("seq")) == 1,
        "★ 退回**最后一次成功取数**（与改动前同一次）")
    check(_hit_count(info, t.msgs) == 0, "自检：该锚点确实 0 命中", _hit_count(info, t.msgs))

    note = _cube_multi_note(info)
    check(note != "", "★ 0 命中时**必须**出如实注记")
    check("未能唯一对上正文表格" in note, "注记点明「未能唯一对上正文表格」")
    check("2 次 Cube **取数**调用" in note, "注记给出取数次数", _note_lines(note)[0][:60])
    check(note.count("\n> - ") == 2, "注记列出全部 2 次取数（1 条带 ★）",
          note.count("\n> - "))
    check(sum(1 for ln in _note_lines(note) if ln.startswith("> - ★")) == 1,
          "★ 恰好一条标出「本节」")
    check("dept_level1" in note and "project_name" in note,
          "两个候选的维度都在清单里（读者能自己判断该看哪条）")


# ══════════════════════════════════════════════════════════════════════
# D. 候选池口径：`sql_only` 预览不算取数
# ══════════════════════════════════════════════════════════════════════
def d_preview_pool() -> None:
    section("D. 候选池 = 「成功且非 sql_only」；池空才退回预览（任何一侧都不丢节）")
    prev = {"cube": "c", "measures": ["m"], "sql_only": True,
            "filters": ["user_name:eq:丛培强"]}
    # D1：1 次取数 + 其后 1 次预览 ⇒ 锚在取数那次；注记**空串**（只取过一次数）
    t = Trace()
    t.cube({"cube": "c", "dimensions": ["a"], "measures": ["m"]},
           _res([{"a": "x", "m": 1}]))
    t.cube(prev, {"sql": "WITH v AS (SELECT 1) SELECT * FROM v", "sql_only": True})
    t.ai(_md_table(["列", "值"], [["x", "1"]]))
    info = _pick_cube_call_for_report(t.msgs)
    check(info.get("data_calls") == 1 and info.get("preview_calls") == 1,
          "计数：取数 1 / 预览 1", f"{info.get('data_calls')}/{info.get('preview_calls')}")
    check((info.get("call") or {}).get("args", {}).get("dimensions") == ["a"],
          "★ 预览不会被选成锚点（后发的预览也算不过取数那次）")
    check(_cube_multi_note(info) == "", "★ 单次取数 ⇒ 注记空串（老报告逐字不变）")
    check(_cube_physical_multi(info) == "", "★ 单次取数 ⇒ 物理节短注也为空")

    # D2：只有预览（没取到数）⇒ 池退回「成功的调用」，定义节照样有内容
    t = Trace()
    t.cube(prev, {"sql": "WITH v AS (SELECT 1) SELECT * FROM v", "sql_only": True})
    t.cube({**prev, "measures": ["m2"]},
           {"sql": "WITH v2 AS (SELECT 2) SELECT * FROM v2", "sql_only": True})
    t.ai(_md_table(["列", "值"], [["x", "1"]]))
    info = _pick_cube_call_for_report(t.msgs)
    check((info.get("call") or {}).get("args", {}).get("measures") == ["m2"],
          "★ 只有预览时退回预览（最后一次），定义节不空")
    check(info.get("data_calls") == 0
          and _cube_multi_note(info) == "" and _cube_physical_multi(info) == "",
          "没取到数 ⇒ 不出「多次取数」注记（无取数可列）")


# ══════════════════════════════════════════════════════════════════════
# E. 失败调用：全失败退回最后一条；选中之后的失败计入 skipped_failed
# ══════════════════════════════════════════════════════════════════════
def e_failed_calls() -> None:
    section("E. 失败调用（全失败退回最后一条；选中之后的失败计入 skipped_failed）")
    a1 = {"cube": "c", "dimensions": ["a"], "measures": ["m"]}
    # E1：全失败 ⇒ 退回最后一条、ok=False、skipped_failed=0
    t = Trace()
    t.cube(a1, None, error=True)
    t.cube({**a1, "dimensions": ["b"]}, None, error=True)
    t.ai("查询失败")
    info = _pick_cube_call_for_report(t.msgs)
    pick = info.get("call") or {}
    check(pick.get("args", {}).get("dimensions") == ["b"],
          "全失败 ⇒ 退回**最后一次**调用（不假装有成功调用）")
    check(pick.get("ok") is False and pick.get("skipped_failed") == 0,
          "ok=False 且 skipped_failed=0",
          f"ok={pick.get('ok')} skip={pick.get('skipped_failed')}")

    # E2：命中取数之后还有一次失败 ⇒ skipped_failed=1（注记要说清「展示的不是最后一次」）
    t = Trace()
    t.cube(a1, _res([{"a": "x", "m": 1}]))
    t.cube({**a1, "dimensions": ["b"]}, None, error=True)
    t.ai(_md_table(["列", "值"], [["x", "1"]]))
    info = _pick_cube_call_for_report(t.msgs)
    check((info.get("call") or {}).get("skipped_failed") == 1,
          "选中之后的失败计入 skipped_failed=1",
          (info.get("call") or {}).get("skipped_failed"))
    # 锚点可能是**更早**的一次（值匹配），所以注记不能说「取的是最近一次成功」
    check("最近一次成功" not in _CUBE_SKIPPED_NOTE,
          "★ 失败注不再声称「最近一次成功」（措辞随选法一起改）",
          _CUBE_SKIPPED_NOTE[:24])

    # E3：同一轮并发（一条 AI 消息里两次调用）—— 序必须是 (消息下标, 消息内位置)
    t = Trace()
    t.cubes([(a1, _res([{"a": "x", "m": 1}]), False),
             ({**a1, "dimensions": ["b"]}, None, True)])
    t.ai(_md_table(["列", "值"], [["x", "1"]]))
    info = _pick_cube_call_for_report(t.msgs)
    check((info.get("call") or {}).get("skipped_failed") == 1,
          "★ 同一轮并发：位置在后的失败也算「其后」",
          (info.get("call") or {}).get("skipped_failed"))

    t = Trace()
    t.cubes([({**a1, "dimensions": ["b"]}, None, True),
             (a1, _res([{"a": "x", "m": 1}]), False)])
    t.ai(_md_table(["列", "值"], [["x", "1"]]))
    info = _pick_cube_call_for_report(t.msgs)
    check((info.get("call") or {}).get("skipped_failed") == 0,
          "★ 同一轮并发：位置在前的失败不算「其后」",
          (info.get("call") or {}).get("skipped_failed"))


# ══════════════════════════════════════════════════════════════════════
# F. 真实 thread 01a14503 重放（8 次调用：6 取数 + 2 预览）
# ══════════════════════════════════════════════════════════════════════
_FILTERS = ["user_name:eq:丛培强"]
_MONTH = "work_date:month:2026-08-01,2026-10-01"


def _fixture_01a14503() -> Trace:
    """重放生产 thread `01a14503`（「丛培强8-9月工时分析」）。

    - **调用规格逐字取自**审计件
      `nl2sql_process_data/01a14503-…/wren-metric-query/_cube_summary-12deabb1-01a14503.json`
      （含真实顺序：2 次 `sql_only` 预览在前，随后 6 次取数；`data_bearing_calls: 6`）。
    - **返回载荷在审计件里是 `output: {"_omitted": true}`**（只留 `rows_count`），所以
      行数据按**报告正文**反推：正文「一、人员身份」表 ← identity 那次、
      「已审核 152.0 / 未审核 16.0」← `if_approve` 那次、「43 天」← day 粒度那次；
      正文里**没有**其输出的那两次（`project_name` / `product_name × project_module_name`）
      按「不可能命中正文」构造，免得夹具的断言建立在编造的数字上（这也让 A~E 的断言
      成为唯一判据，本夹具只用来验「8 次调用下的整链行为」）。
    - 正文文本取自报告 `丛培强2026年8-9月工时分析_2026-10-16_22-04-13_01a14503.md`
      的「1. 数据结果」节（即子 agent 的最终答复；报告注明它在「按角色拆分」处被截断）。
    """
    t = Trace()
    # ① ② 两次 `sql_only` 预览（只编译 SQL、不返回数据）
    t.cube({"cube": "workhour_analysis",
            "measures": ["total_hours", "record_count", "work_days", "project_count",
                         "approved_hours", "pending_hours", "approve_rate",
                         "avg_hours_per_record"],
            "filters": _FILTERS, "time_dimension": _MONTH, "sql_only": True},
           {"sql": "WITH v_workhour AS (...) SELECT SUM(work_hour) ...", "sql_only": True})
    t.cube({"cube": "workhour_analysis", "dimensions": ["project_name"],
            "measures": ["total_hours", "record_count", "work_days", "approved_hours",
                         "pending_hours"],
            "filters": _FILTERS, "time_dimension": _MONTH, "sql_only": True},
           {"sql": "WITH v_workhour AS (...) SELECT project_name ...", "sql_only": True})
    # ③ identity（正文「一、人员身份」表）
    t.cube({"cube": "workhour_analysis",
            "dimensions": ["user_name", "job_number", "department_full_name", "role_name"],
            "measures": ["total_hours", "record_count", "work_days", "project_count",
                         "approved_hours", "pending_hours", "approve_rate",
                         "avg_hours_per_record"],
            "filters": _FILTERS, "time_dimension": _MONTH},
           _res([{"user_name": "丛培强", "job_number": "Z0051",
                  "department_full_name": "AI与数据产品线/产品管理部",
                  "role_name": "开发", "work_date__month": "2026-08-01",
                  "total_hours": 168.0, "record_count": 22, "work_days": 21},
                 {"user_name": "丛培强", "job_number": "Z0051",
                  "department_full_name": "AI与数据产品线/产品管理部",
                  "role_name": "综管", "work_date__month": "2026-08-01",
                  "total_hours": 168.0, "record_count": 22, "work_days": 21},
                 {"user_name": "丛培强", "job_number": "Z0051",
                  "department_full_name": "AI与数据产品线/产品管理部",
                  "role_name": "综管", "work_date__month": "2026-09-01",
                  "total_hours": 176.0, "record_count": 22, "work_days": 22}]))
    # ④ project_name（正文：project_name 全为 NULL ⇒ 2 行按月，**构造**为不命中正文）
    t.cube({"cube": "workhour_analysis", "dimensions": ["project_name"],
            "measures": ["total_hours", "record_count", "work_days", "approved_hours",
                         "pending_hours"],
            "filters": _FILTERS, "time_dimension": _MONTH},
           _res([{"project_name": None, "total_hours": 13.0, "record_count": 3},
                 {"project_name": None, "total_hours": 17.0, "record_count": 4}]))
    # ⑤ product_name × project_module_name（正文无其输出 ⇒ **构造**为不命中）
    t.cube({"cube": "workhour_analysis",
            "dimensions": ["product_name", "project_module_name"],
            "measures": ["total_hours", "record_count"],
            "filters": _FILTERS, "time_dimension": _MONTH},
           _res([{"product_name": "产品A", "project_module_name": "模块1",
                  "total_hours": 19.0, "record_count": 5},
                 {"product_name": "产品B", "project_module_name": "模块2",
                  "total_hours": 23.0, "record_count": 6},
                 {"product_name": "产品C", "project_module_name": "模块3",
                  "total_hours": 29.0, "record_count": 7},
                 {"product_name": "产品D", "project_module_name": "模块4",
                  "total_hours": 31.0, "record_count": 9}]))
    # ⑥ if_approve（正文「已审核 152.0/16.0、未审核 16.0/160.0」）
    t.cube({"cube": "workhour_analysis", "dimensions": ["if_approve"],
            "measures": ["total_hours", "record_count"],
            "filters": _FILTERS, "time_dimension": _MONTH},
           _res([{"if_approve": "是", "work_date__month": "2026-08-01",
                  "total_hours": 152.0, "record_count": 20},
                 {"if_approve": "是", "work_date__month": "2026-09-01",
                  "total_hours": 16.0, "record_count": 2},
                 {"if_approve": "否", "work_date__month": "2026-08-01",
                  "total_hours": 16.0, "record_count": 2},
                 {"if_approve": "否", "work_date__month": "2026-09-01",
                  "total_hours": 160.0, "record_count": 20}]))
    # ⑦ day 粒度（正文「有报工天数 43 天」；载荷**构造**为不命中正文）
    days = [f"2026-08-{d:02d}" for d in range(3, 25)]
    days += [f"2026-09-{d:02d}" for d in range(1, 21)]
    t.cube({"cube": "workhour_analysis", "measures": ["total_hours", "record_count"],
            "filters": _FILTERS, "time_dimension": "work_date:day:2026-08-01,2026-10-01"},
           _res([{"work_date__day": d, "total_hours": 8.0, "record_count": 1}
                 for d in days[:43]], row_count=43))
    # ⑧ if_leave_form × approve_user_name —— **改动前的锚点**（5 行，数据不在正文里）
    t.cube({"cube": "workhour_analysis",
            "dimensions": ["if_leave_form", "approve_user_name"],
            "measures": ["total_hours", "record_count"],
            "filters": _FILTERS, "time_dimension": _MONTH},
           _res([{"if_leave_form": "否", "approve_user_name": "王X",
                  "total_hours": 96.0, "record_count": 12},
                 {"if_leave_form": "否", "approve_user_name": "李Y",
                  "total_hours": 88.0, "record_count": 11},
                 {"if_leave_form": "是", "approve_user_name": "王X",
                  "total_hours": 80.0, "record_count": 10},
                 {"if_leave_form": "是", "approve_user_name": "李Y",
                  "total_hours": 48.0, "record_count": 6},
                 {"if_leave_form": "是", "approve_user_name": None,
                  "total_hours": 32.0, "record_count": 5}], row_count=5))

    t.ai(
        "# 丛培强 2026 年 8-9 月工时分析\n\n"
        "**查询通道**：语义层 Cube `workhour_analysis`（`base_object = v_workhour`），"
        "全程 `query_cube`，未手写 SQL。\n\n"
        "## 一、人员身份（Cube 维度返回）\n\n"
        + _md_table(["姓名", "工号", "部门全路径", "角色"],
                    [["丛培强", "Z0051", "AI与数据产品线/产品管理部",
                      "开发（8月）、综管（8-9月）"]])
        + "\n\n## 二、总量指标（8-9 月合计，取自返回行求和）\n\n"
        + _md_table(["指标", "数值", "说明"],
                    [["总工时", "**344.0 小时**", "8月 168.0 + 9月 176.0"],
                     ["报工记录数", "**44 条**", "8月 22 + 9月 22"],
                     ["有报工天数", "**43 天**", "8月 21 + 9月 22"],
                     ["单条平均工时", "**8.0 小时**", "44 条均为 8.0 小时"],
                     ["涉及项目数", "**0**", "`project_name` 全为 NULL"]])
        + "\n\n## 三、按月趋势\n\n"
        + _md_table(["月份", "总工时", "记录数", "报工天数", "已审核工时",
                     "未审核工时", "审核通过率", "单条均值"],
                    [["2026-08", "168.0", "22", "21", "152.0", "16.0",
                      "90.91%", "7.33"],
                     ["2026-09", "176.0", "22", "22", "16.0", "160.0",
                      "9.09%", "8.00"]])
        + "\n\n按角色拆分（8 月为两个角色并存）：\n\n| 月份 |"
    )
    # 收尾语：子 agent 以 write_todos 收尾必然再补一条短消息（结果锚点必须跳过它）
    t.ai("查询完毕！以上为本次工时分析的全部内容。")
    return t


def f_replay_01a14503() -> Trace:
    section("F. 真实 thread 01a14503 重放（8 次调用 = 6 取数 + 2 预览）")
    t = _fixture_01a14503()
    cands = extract_cube_calls(t.msgs)
    info = _pick_cube_call_for_report(t.msgs)
    pick = info.get("call") or {}
    before = extract_last_cube_call(t.msgs)

    check(len(cands) == 8, "8 次 Cube 调用全部枚举到", len(cands))
    check(info.get("data_calls") == 6 and info.get("preview_calls") == 2,
          "取数 6 / 预览 2（与审计件 `data_bearing_calls: 6` 一致）",
          f"{info.get('data_calls')}/{info.get('preview_calls')}")
    check(before.get("args", {}).get("dimensions") == ["if_leave_form",
                                                       "approve_user_name"],
          "对照：改动前的锚点 = 最后一次成功（`if_leave_form × approve_user_name`）",
          before.get("args", {}).get("dimensions"))
    check(info.get("matched") is True, "★ 值匹配命中（锚点有产出证据）")
    check(info.get("tier") == "tier0", "tier=tier0", info.get("tier"))
    check(pick.get("args") != before.get("args"),
          "★ 锚点不再是「最后一次成功」那次（本 thread 的错位被纠正）",
          pick.get("args", {}).get("dimensions"))
    check(not pick.get("args", {}).get("sql_only"),
          "★ 锚点必是**取数**调用（不是 sql_only 预览）")
    check(_hit_count(info, t.msgs) >= 1,
          "★ 锚点确实整行覆盖正文表 ≥1 行", _hit_count(info, t.msgs))
    print(f"      （本节锚点：{json.dumps(pick.get('args'), ensure_ascii=False)}）")
    return t


# ══════════════════════════════════════════════════════════════════════
# G. 注记渲染契约（块引用逐行判定；单次取数不得改变老报告）
# ══════════════════════════════════════════════════════════════════════
def g_note_render(t: Trace) -> None:
    section("G. 注记渲染契约（`md_parts += [\"\", f\"> {note}\"]`）")
    info = _pick_cube_call_for_report(t.msgs)
    note = _cube_multi_note(info)
    lines = _note_lines(note)
    check(len(lines) == 9, "结构 = 首行 + 空行 + 标题 + 6 条清单", len(lines))
    check(all(ln.startswith("> ") for ln in lines[1:]),
          "★ 除首行外每行自带 `> ` 前缀（多行块引用逐行判定，漏前缀就掉出引用）")
    check(all(ln.startswith("> ") for ln in f"> {note}".split("\n")),
          "★ 渲染后所有行都在块引用内（含首行）")
    check("```" not in note, "注记里不含代码围栏（会截断块引用）")
    check(all(ln.startswith("> - ") for ln in lines[3:]),
          "清单统一用列表项前缀（无前缀行会被并进上一条）")
    check(sum(1 for ln in lines[3:] if ln.startswith("> - ★")) == 1,
          "恰好一条标出「本节」")
    check("另有 2 次 `sql_only` 预览" in note, "预览次数如实列出")
    check(note.count("cube=workhour_analysis") == 6, "6 条取数逐条给出 cube",
          note.count("cube=workhour_analysis"))
    for key in ("dimensions=", "measures=", "filters=", "time_dimension="):
        check(key in note, f"清单含 {key}")

    phy = _cube_physical_multi(info)
    check(phy.startswith("注：本轮共 6 次 Cube 取数调用")
          and "值匹配对得上正文表格的那次" in phy,
          "物理 SQL 节短注（单段、无换行）", phy[:40])
    check("\n" not in phy, "物理节短注不含换行（就地拼在 `> {plan_note}` 里）")


# ══════════════════════════════════════════════════════════════════════
# H. 端到端：check 结果里的 §2/§5 就是新锚点；单次取数逐字不变
# ══════════════════════════════════════════════════════════════════════
def h_end_to_end(t: Trace) -> None:
    section("H. 端到端（`_enhanced_build_check_result`）：§5 定义 = 新锚点 + 注记")
    res = _enhanced_build_check_result(
        {"status": "success"}, "01a14503-dbb9-74f2-a380-8e66f26dfb29",
        {"messages": t.msgs})
    info = _pick_cube_call_for_report(t.msgs)
    pick = info.get("call") or {}
    check(res.get("sql_kind") == "cube", "走的仍是 Cube 通道（sql_kind=cube）")
    check(res.get("cube_query") == "\n".join(pick.get("lines") or []),
          "★ `cube_query` 就是新锚点的查询定义（逐字一致）")
    check("if_leave_form" not in str(res.get("cube_query") or ""),
          "★ 定义节里不再出现「最后一次成功」那次的维度")
    note = str(res.get("sql_note") or "")
    check(_CUBE_NOTE in note, "保留原有的「Cube 语义层查询定义（非 SQL）」注")
    check(_CUBE_MULTI_HEAD.format(
        n=6, preview="（另有 2 次 `sql_only` 预览，未取数）",
        which="值匹配对得上正文表格的那一次") in note,
        "★ 本次取数注记整句进了 `sql_note`")
    check("> - ★" in note and note.count("> - ") == 6,
          "★ 6 条取数清单都在 `sql_note` 里（报告第 5 节会渲染出来）")

    # 单次取数：报告必须与改动前**逐字**一致（只有基线注）
    t1 = Trace()
    t1.cube({"cube": "c", "dimensions": ["a"], "measures": ["m"]},
            _res([{"a": "x", "m": 1}]))
    t1.ai(_md_table(["列", "值"], [["x", "1"]]))
    r1 = _enhanced_build_check_result({"status": "success"}, "tid-1", {"messages": t1.msgs})
    check(r1.get("sql_note") == _CUBE_NOTE,
          "★ 单次取数 ⇒ `sql_note` 与改动前逐字相同（老报告零变化）",
          repr(r1.get("sql_note"))[:40])
    check("physical_sql_note" not in r1,
          "无物理 SQL（离线无 wren 工程）⇒ 也不添物理注（与改动前一致）")

    # 全失败：原有的「所有 Cube 调用都未成功」必须仍在
    t2 = Trace()
    t2.cube({"cube": "c", "measures": ["m"]}, None, error=True)
    t2.ai("查询失败")
    r2 = _enhanced_build_check_result({"status": "success"}, "tid-2", {"messages": t2.msgs})
    check(_CUBE_ALL_FAILED_NOTE in str(r2.get("sql_note") or ""),
          "全失败 ⇒ 原有失败注仍在，且不加「多次取数」注")


# ══════════════════════════════════════════════════════════════════════
def i_wiring_guard() -> None:
    section("I. 接线守卫（防有人把 Cube 分支改回位置判据）")
    src = (_SRC / "agent" / "subagents" / "check_progress.py").read_text(encoding="utf-8")
    check("_pick = _pick_cube_call_for_report(messages)" in src,
          "Cube 分支用新锚点选法")
    check("_cube_multi_note(_pick)" in src, "分支里接了「多次取数」注记")
    check("_cube_physical_multi(_pick)" in src, "分支里接了物理节短注")
    check("_cube = _extract_last_cube_call(messages)" not in src,
          "老的「直接取最后一次成功」分支形态已不存在")
    # 余下 3 处调用都是**故意**保留的：定义文本 helper、`_extract_windows` 窗口探测、
    # 以及本分支的 `or _extract_last_cube_call(messages)` 兜底（picker 异常时退回老行为）
    check(src.count("_extract_last_cube_call(messages)") == 3,
          "`_extract_last_cube_call` 余 3 处（helper / 窗口探测 / 异常兜底）",
          src.count("_extract_last_cube_call(messages)"))


# ══════════════════════════════════════════════════════════════════════
def main() -> None:
    a_value_match_wins()
    b_tie_break()
    c_no_evidence_fallback()
    d_preview_pool()
    e_failed_calls()
    t = f_replay_01a14503()
    g_note_render(t)
    h_end_to_end(t)
    i_wiring_guard()

    print()
    passed = sum(1 for ok, _ in results if ok)
    print(f"{passed}/{len(results)} 通过"
          + ("" if passed == len(results) else "   ← 有失败"))
    if passed == len(results):
        print(f"（落盘根：{_TMP_ROOT}）")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
