# -*- coding: utf-8 -*-
"""产出 SQL 选取（`check_progress._extract_last_sql`）的契约验证（离线，无需后端/MCP/网络）。

**要关的口子**（2026-09-29 修，发版后用户实测 trace dcce39a0）：

数据由 **Cube 通道**产出（4 次 `query_cube`，cube `bug_quality`，measures
`bug_count` / `finished_count` 按 `create_time:day`），但子 agent 中途跑过一次
`SELECT CURRENT_DATE AS today, DATE_FORMAT(CURRENT_DATE, '%Y-%m-01') AS month_start,
LAST_DAY(CURRENT_DATE) AS month_end` 探值 —— 那是整条轨迹里**唯一**的 run_sql。
答案表是「日期|新增缺陷数|完成缺陷数」的中文表头日明细：与探值三列
（`today` / `month_start` / `month_end`）既无值交集也无列名交集 ⇒ tier0（值匹配）/
0b（标量）/1（列名）全落空 ⇒ tier2「返回行数最多」把这条**恒 1 行**的探值当成了产出
SQL。三重后果（用户看到的那张报告）：
  ① 「4. 执行 SQL（物理，实际下发）」里是那条探值编译后的语句，与数据结果不匹配；
  ② 调用方 `if sql:` 分支压掉 `else` 里的 Cube 查询定义 + `plan_cube_sql`
     ⇒ 真实物理 SQL **整节消失**；
  ③ `judge_sql_origin` 因「锚点之前没有成功 cube 调用」判成 `llm_from_schema`
     ⇒ 报告那行「模型依据语义库 schema 手写（未使用 Cube 具名指标）」与事实正好相反。
     （由 ③ 可反推真实轨迹里探值在取数的 cube 调用**之前**——否则锚点之前有成功 cube
     调用会判 `cube_metric+llm_outer`，与线上报告那行不符；本脚本夹具按此排序。）

修法：兜底层（tier 2/3）之前加**通道闸** —— tier 0/0b/1 全落空（= 选中的这条 SQL
没有任何「产出了最终表」的证据）且同批消息里存在成功取数的 Cube 调用 ⇒ 返回 ""
（空串本就是契约内「没有产出 SQL」的表达），交回 Cube 通道如实渲染。

本脚本断言：五层启发式各自的选取（tier0 压过行数 / 0b 标量对齐 / 1 列名 / 2、3 兜底
仍在）、通道闸的正例与**五条负对照**、闸与 `judge_sql_origin.evidence.cube_data_calls`
**同源**、以及「先选到探值 ⇒ 报告那行说反话 / 修好后 ⇒ 说 Cube」的对照。

⚠️ 只校验**本地代码**：线上跑的是发版镜像里的 `src`，生产对照必须 `docker exec md5sum`。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_sql_pick.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

# 与仓内 verify 脚本同款：先钉临时根，避免任何落盘写进真实工作区。
_TMP_ROOT = tempfile.mkdtemp(prefix="nl2sql-verify-sqlpick-")
os.environ.setdefault("AGENT_DATA_ROOT", _TMP_ROOT)

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
_SRC = _ROOT / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from agent.subagents.check_progress import (  # noqa: E402
    _cube_produced_data,
    _extract_last_sql,
)
from agent.tools import report_builder as rb  # noqa: E402
from agent.utils.process_audit import judge_sql_origin  # noqa: E402
from agent.utils.wren_call_extract import extract_last_cube_call  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

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
    """工具名带库前缀（`wrenai_witops_run_sql`）—— 后缀匹配才是线上真实路径。"""

    def __init__(self) -> None:
        self.msgs: list[dict] = []
        self._n = 0

    def call(self, name: str, args: dict | None = None,
             result: object = None, error: bool = False) -> None:
        self._n += 1
        cid = f"c{self._n}"
        self.msgs.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"name": name, "args": args or {}, "id": cid}],
        })
        if result is not None or error:
            content = (result if isinstance(result, str)
                       else json.dumps(result, ensure_ascii=False))
            msg: dict = {"role": "tool", "name": name, "tool_call_id": cid,
                         "content": content}
            if error:
                msg["content"] = f"Error executing tool {name}: boom"
                msg["status"] = "error"
            self.msgs.append(msg)

    def ai(self, text: str) -> None:
        self.msgs.append({"role": "assistant", "content": text})


def _res(columns: list[str], rows: list, row_count: int | None = None) -> dict:
    return {"columns": columns, "rows": rows,
            "row_count": row_count if row_count is not None else len(rows)}


_SQL_DETAIL = ("SELECT e.name, e.dept, e.hire_date FROM emp_detail e "
               "WHERE e.hire_date >= '2026-08-01'")
_SQL_DIST = "SELECT d.stat_date, d.status, COUNT(*) AS cnt FROM defect d GROUP BY 1, 2"
_SQL_SCALAR = "SELECT COUNT(DISTINCT dept) AS dept_cnt, COUNT(*) AS emp_cnt FROM emp"
_SQL_TOPN = "SELECT dept_name, num FROM dept_stat ORDER BY num DESC LIMIT 5"
_SQL_PROBE = ("SELECT CURRENT_DATE AS today, DATE_FORMAT(CURRENT_DATE, '%Y-%m-01') "
              "AS month_start, LAST_DAY(CURRENT_DATE) AS month_end")


def _dist_24() -> dict:
    rows = [{"stat_date": f"2026-08-{d:02d}",
             "status": ("已关闭" if d % 2 else "已确认"), "cnt": d}
            for d in range(1, 25)]
    return _res(["stat_date", "status", "cnt"], rows, 24)


def _probe_1() -> dict:
    return _res(["today", "month_start", "month_end"],
                [{"today": "2026-09-29", "month_start": "2026-09-01",
                  "month_end": "2026-09-30"}], 1)


_CUBE_DATA_ROWS = _res(["create_time", "bug_count"],
                       [{"create_time": "2026-09-01", "bug_count": 12},
                        {"create_time": "2026-09-02", "bug_count": 9}], 2)
_CUBE_DONE_ROWS = _res(["complete_time", "finished_count"],
                       [{"complete_time": "2026-09-01", "finished_count": 8},
                        {"complete_time": "2026-09-02", "finished_count": 5}], 2)


# ══════════════════════════════════════════════════════════════════════
# ⓿ 夹具自检：Cube-only 轨迹里根本没有 run_sql 候选
# ══════════════════════════════════════════════════════════════════════
def s0_fixture_sanity() -> None:
    section("⓿ 夹具自检：候选过滤（`\"run_sql\" in name`）")
    t = Trace()
    t.call("wrenai_witops_query_cube", {"cube": "bug_quality",
                                       "measures": ["bug_count"]},
           result=_CUBE_DATA_ROWS)
    t.ai("| 日期 | 新增缺陷数 |\n| --- | --- |\n| 2026-09-01 | 12 |")
    check(_extract_last_sql(t.msgs) == "",
          "★ Cube-only 轨迹无 run_sql 候选 ⇒ 空串（else 分支靠这一条成立）")
    check(_cube_produced_data(t.msgs) is True,
          "同批消息被通道闸认定为「Cube 已取数」（正例夹具成立）")
    # 负对照：同一轨迹里 cube 调用失败 ⇒ 不算取数
    t2 = Trace()
    t2.call("wrenai_witops_query_cube", {"cube": "bug_quality"}, error=True)
    check(_cube_produced_data(t2.msgs) is False,
          "负对照：cube 调用失败 ⇒ 不算「已取数」")


# ══════════════════════════════════════════════════════════════════════
# ① tier 0 值匹配（trace 99903681 形状）：压过「行数最多」
# ══════════════════════════════════════════════════════════════════════
_FINAL_DETAIL = (
    "查询完成，明细如下：\n\n"
    "| 姓名 | 部门 | 入职日期 |\n"
    "| --- | --- | --- |\n"
    "| 张伟 | 研发部 | 2026-08-01 |\n"
    "| 李娜 | 市场部 | 2026-08-03 |\n"
    "| 王强 | 研发部 | 2026-08-05 |\n"
)


def a_tier0_value_match() -> None:
    section("① tier 0 值匹配（中文表头 vs 英文列）")
    t = Trace()
    t.call("wrenai_witops_run_sql", {"sql": _SQL_DETAIL},
           result=_res(["name", "dept", "hire_date"],
                       [{"name": "张伟", "dept": "研发部", "hire_date": "2026-08-01"},
                        {"name": "李娜", "dept": "市场部", "hire_date": "2026-08-03"},
                        {"name": "王强", "dept": "研发部", "hire_date": "2026-08-05"}], 3))
    t.ai("中间态：先看分布。")
    t.call("wrenai_witops_run_sql", {"sql": _SQL_DIST}, result=_dist_24())
    t.ai(_FINAL_DETAIL)
    got = _extract_last_sql(t.msgs)
    check(got == _SQL_DETAIL, "★ 整行值匹配选中产出明细的 SQL", got[:60])
    check(got != _SQL_DIST, "★ 而非 24 行分布探值（行数更多）")
    check(_cube_produced_data(t.msgs) is False,
          "无 cube 调用 ⇒ 通道闸不介入（本组测的是纯 run_sql 通道）")

    # 负对照：最终答复没有数据表 ⇒ tier0 无从匹配 ⇒ 回到 tier2「行数最多」（老行为仍在）
    t2 = Trace()
    t2.call("wrenai_witops_run_sql", {"sql": _SQL_DETAIL},
            result=_res(["name"], [{"name": "张伟"}], 1))
    t2.call("wrenai_witops_run_sql", {"sql": _SQL_DIST}, result=_dist_24())
    t2.ai("查询完成，请看上方结果。")
    check(_extract_last_sql(t2.msgs) == _SQL_DIST,
          "负对照：答案表缺席 ⇒ tier2「行数最多」照旧生效（兜底未被拆掉）")


# ══════════════════════════════════════════════════════════════════════
# ② tier 0b 标量摘要（trace d60b / 01a07043 形状）
# ══════════════════════════════════════════════════════════════════════
def b_tier0b_scalar() -> None:
    section("② tier 0b 标量摘要对齐（两列 / 三列口径表）")
    for label, final in (
        ("两列指标|数值",
         "| 指标 | 数值 |\n| --- | --- |\n| 部门数量 | 431 |\n| 部门人数 | 1736 |\n"),
        ("三列统计项|口径|数量",
         "| 统计项 | 口径 | 数量 |\n| --- | --- | --- |\n"
         "| 部门总数 | 在职 | 431 |\n| 去重员工总数 | 在职 | 280 |\n"),
    ):
        nums = [431, 1736] if "两列" in label else [431, 280]
        t = Trace()
        t.call("wrenai_witops_run_sql", {"sql": _SQL_TOPN},
               result=_res(["dept_name", "num"],
                           [{"dept_name": d, "num": 500 - i * 10}
                            for i, d in enumerate(["研发", "销售", "生产", "客服", "行政"])], 5))
        t.call("wrenai_witops_run_sql", {"sql": _SQL_SCALAR},
               result=_res(["dept_cnt", "emp_cnt"],
                           [{k: v} for k, v in zip(("dept_cnt", "emp_cnt"), nums)], 1))
        t.ai(final)
        got = _extract_last_sql(t.msgs)
        check(got == _SQL_SCALAR, f"★ {label}：覆盖全部最终数值的标量 SQL 胜出（而非 Top-5）",
              got[:50])

    # 负对照：真明细表（两列全数字）⇒ 00b 刻意不启用 ⇒ 回落 tier2
    t = Trace()
    t.call("wrenai_witops_run_sql", {"sql": _SQL_TOPN}, result=_res(
        ["dept_name", "num"], [{"dept_name": d, "num": 500 - i * 10}
                               for i, d in enumerate(["研发", "销售", "生产", "客服", "行政"])], 5))
    t.ai("| id | parent_id |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n")
    check(_extract_last_sql(t.msgs) == _SQL_TOPN,
          "负对照：两列全数字的明细表 ⇒ tier0b 不启用（防把明细当摘要），走 tier2")


# ══════════════════════════════════════════════════════════════════════
# ③ tier 1 列名 ∩ 表头 + 与 tier0 的优先级
# ══════════════════════════════════════════════════════════════════════
def c_tier1_and_priority() -> None:
    section("③ tier 1 列名匹配 与「tier0 优先于 tier1」")
    _SQL_COLNAME = "SELECT dept_name, total FROM dept_summary WHERE year = 2026"
    t = Trace()
    t.call("wrenai_witops_run_sql", {"sql": _SQL_COLNAME},
           result=_res(["dept_name", "total"], [{"dept_name": "销售", "total": "9"}], 1))
    t.ai("| dept_name | total |\n| --- | --- |\n| 研发 | 12 |\n| 市场 | 7 |\n")
    check(_extract_last_sql(t.msgs) == _SQL_COLNAME,
          "★ 列名 ∩ 表头非空 ⇒ tier1 选中（值对不上也不影响）")

    # 优先级：值能对上的那条（列名完全不搭）胜过列名对得上的那条
    _SQL_VALUES = "SELECT x, y FROM t WHERE k = 'a'"
    t2 = Trace()
    t2.call("wrenai_witops_run_sql", {"sql": _SQL_COLNAME},
            result=_res(["dept_name", "total"], [{"dept_name": "销售", "total": "9"}], 1))
    t2.call("wrenai_witops_run_sql", {"sql": _SQL_VALUES},
            result=_res(["x", "y"], [{"x": "研发", "y": "12"}], 1))
    t2.ai("| dept_name | total |\n| --- | --- |\n| 研发 | 12 |\n")
    check(_extract_last_sql(t2.msgs) == _SQL_VALUES,
          "★ tier0（值）优先于 tier1（列名）——同一条最终行两条候选都能认")


# ④ 通道闸（本轮修复）
# ══════════════════════════════════════════════════════════════════════
_FINAL_CUBE = (
    "本月每日新增与完成缺陷数如下：\n\n"
    "| 日期 | 新增缺陷数 | 完成缺陷数 |\n"
    "| --- | --- | --- |\n"
    "| 2026-09-01 | 12 | 8 |\n"
    "| 2026-09-02 | 9 | 5 |\n"
)


def _trace_cube_channel(*, probe_first: bool = True, cube_error: bool = False,
                        only_preview: bool = False) -> Trace:
    """复现生产 trace dcce39a0 的形状：唯一 run_sql = 日期探值 + 成功取数的 cube 调用。

    `probe_first=True` 与线上 `llm_from_schema` 那行一致（见模块 docstring 的 ③）。
    """
    t = Trace()
    if probe_first:
        t.call("wrenai_witops_run_sql", {"sql": _SQL_PROBE}, result=_probe_1())
    t.call("wrenai_witops_list_cubes", {}, result=_res(["name"], [{"name": "bug_quality"}], 1))
    t.call("wrenai_witops_describe_cube", {"cube": "bug_quality"},
           result=_res(["field"], [{"field": "bug_count"}], 1))
    t.call("wrenai_witops_query_cube",
           {"cube": "bug_quality", "measures": ["bug_count"],
            "time_dimension": "create_time:day:2026-09-01,2026-09-30", "sql_only": True},
           result=_res(["sql"], [{"sql": "SELECT ..."}], 1))
    if not only_preview:
        t.call("wrenai_witops_query_cube",
               {"cube": "bug_quality", "measures": ["bug_count"],
                "time_dimension": "create_time:day:2026-09-01,2026-09-30"},
               result=_CUBE_DATA_ROWS, error=cube_error)
        t.call("wrenai_witops_query_cube",
               {"cube": "bug_quality", "measures": ["finished_count"],
                "time_dimension": "complete_time:day:2026-09-01,2026-09-30"},
               result=_CUBE_DONE_ROWS, error=cube_error)
    if not probe_first:
        t.call("wrenai_witops_run_sql", {"sql": _SQL_PROBE}, result=_probe_1())
    t.ai(_FINAL_CUBE)
    return t


def d_cube_channel_gate() -> None:
    section("④ 通道闸：无证据的 run_sql 兜底候选 vs Cube 通道（本轮修复）")
    t = _trace_cube_channel()
    got = _extract_last_sql(t.msgs)

    check(got == "", "★ 正例：返回空串（不再用探值充当产出 SQL）", repr(got[:60]))
    check(got != _SQL_PROBE, "★ 且明确不是那条 CURRENT_DATE 探值")

    # 交回 Cube 通道后，下游真的拿得到东西（否则「交回」是空头支票）
    cube = extract_last_cube_call(t.msgs)
    check(cube.get("args", {}).get("measures") == ["finished_count"],
          "★ 交回目标存在：`extract_last_cube_call` 拿到最后一次成功取数的 cube 调用",
          str(cube.get("args", {}).get("measures")))
    check(bool(cube.get("lines")), "同一次扫描还给出「查询定义」文本（报告定义节用）")

    # 报告那行来源：探值当锚点 ⇒ 说反话；空串 ⇒ 如实说 Cube
    old_origin = judge_sql_origin(t.msgs, _SQL_PROBE)
    new_origin = judge_sql_origin(t.msgs, "")
    check(old_origin["origin"] == "llm_from_schema",
          "★ 对照：把探值当产出 SQL ⇒ 来源判成 llm_from_schema（线上那行就是这么来的）",
          old_origin["origin"])
    old_line = rb._sql_origin_line({"sql_origin": old_origin["origin"]})
    check("未使用 Cube 具名指标" in old_line,
          "★ 对照：报告那行逐字就是「模型依据语义库 schema 手写（未使用 Cube 具名指标）」",
          old_line)
    check(new_origin["origin"] == "cube_metric",
          "★ 修好后：来源判成 cube_metric（判据唯一实现在 process_audit）",
          new_origin["origin"])
    new_line = rb._sql_origin_line({"sql_origin": new_origin["origin"]})
    check("Cube" in new_line and "未使用 Cube" not in new_line,
          "★ 报告那行改为「由 Cube 语义层的具名指标直接编译下发」", new_line)

    # ── 同源闸：通道闸与 judge_sql_origin.evidence.cube_data_calls 必须一致 ──
    for label, fx in (
        ("正例（取数成功）", _trace_cube_channel()),
        ("cube 全失败", _trace_cube_channel(cube_error=True)),
        ("只有 sql_only 预览", _trace_cube_channel(only_preview=True)),
        ("无 cube 调用", Trace()),
        ("探值在 cube 之后", _trace_cube_channel(probe_first=False)),
    ):
        n = judge_sql_origin(fx.msgs, "").get("evidence", {}).get("cube_data_calls", 0)
        check(_cube_produced_data(fx.msgs) == (n > 0),
              f"同源闸：{label} ⇒ 闸={_cube_produced_data(fx.msgs)} / cube_data_calls={n}")


def e_cube_channel_gate_negative() -> None:
    section("⑤ 通道闸负对照（不得误伤）")
    t = _trace_cube_channel(cube_error=True)
    check(_extract_last_sql(t.msgs) == _SQL_PROBE,
          "负对照1：cube 调用全失败（没取到数）⇒ 兜底照旧（宁可少跳，不静默抹掉 SQL 节）")

    t = _trace_cube_channel(only_preview=True)
    check(_extract_last_sql(t.msgs) == _SQL_PROBE,
          "负对照2：只有 sql_only 预览（没返回数据）⇒ 不算取数，兜底照旧")

    t = Trace()
    t.call("wrenai_witops_run_sql", {"sql": _SQL_PROBE}, result=_probe_1())
    t.ai(_FINAL_CUBE)
    check(_extract_last_sql(t.msgs) == _SQL_PROBE,
          "负对照3：没有 Cube 调用 ⇒ 纯 run_sql 轨迹行为逐字不变")

    # 负对照4：Cube 取了数，但这条 run_sql 有产出证据（tier0 命中）⇒ 闸不得介入
    _SQL_MATCHES = "SELECT d, new_cnt, done_cnt FROM daily_stat"
    t = _trace_cube_channel()
    t.msgs = t.msgs[:-1]  # 去掉最终 AI 答复，重排
    t.call("wrenai_witops_run_sql", {"sql": _SQL_MATCHES},
           result=_res(["d", "new_cnt", "done_cnt"],
                       [{"d": "2026-09-01", "new_cnt": "12", "done_cnt": "8"}], 1))
    t.ai(_FINAL_CUBE)
    check(_extract_last_sql(t.msgs) == _SQL_MATCHES,
          "★ 负对照4：Cube 通道下，能对上最终表的 run_sql 仍然胜出（闸只管无证据的兜底）")

    # 负对照5：全部 run_sql 失败 ⇒ 旧行为（退回全集）不变，闸仍把它交回 Cube 通道
    t = _trace_cube_channel()
    t2 = Trace()
    t2.call("wrenai_witops_run_sql", {"sql": _SQL_PROBE}, result=_probe_1(), error=True)
    t2.msgs += t.msgs[2:]  # 挪用同一批 cube 调用 + 最终答复
    check(_extract_last_sql(t2.msgs) == "",
          "负对照5：唯一 run_sql 失败 + Cube 取数成功 ⇒ 同样交回 Cube（不是把失败语句当产出）")


# ══════════════════════════════════════════════════════════════════════
def main() -> None:
    s0_fixture_sanity()
    a_tier0_value_match()
    b_tier0b_scalar()
    c_tier1_and_priority()
    d_cube_channel_gate()
    e_cube_channel_gate_negative()

    print()
    passed = sum(1 for ok, _ in results if ok)
    print(f"{passed}/{len(results)} 通过"
          + ("" if passed == len(results) else "   ← 有失败"))
    if passed == len(results):
        print(f"（落盘根：{_TMP_ROOT}）")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
