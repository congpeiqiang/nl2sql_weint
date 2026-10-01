# -*- coding: utf-8 -*-
"""Cube 车道软闸 `CubeRouteGateMiddleware` 验证（离线，无需后端 / 真数据库 / 网络 / LLM）。

**被测的坑**（trace `9afcd8ea`，2026-09-30）：问题「今年 8 月和 9 月新增和完成缺陷」
——`bug_quality` cube 完全覆盖（`bug_count=COUNT(*)`、`finished_count=SUM(CASE WHEN
status_code=2 THEN 1 ELSE 0 END)`，`mention_time`/`complete_time` 都带 month 级，底座
`v_bug`），模型也**确实** `describe_cube` 看过它，却手写 5 段 `LEFT JOIN … FROM v_bug …
GROUP BY` 直接 `run_sql`：报告那行落成「模型依据语义库 schema 手写（未使用 Cube 具名
指标）」，而它手写的两个表达式**逐字就是这两个 measure**——数字对得上靠"抄对了"。

两侧都查过：这是**成文策略**允许的（`wren-metric-query/SKILL.md` 写着「多时间轴对比 →
转手写」、`wren-orchestrator/SKILL.md` 写着「`query_cube` 是软偏好不是独占车道」）。
政策改写与提示词收紧是同一轮的**另一个面**；本脚本只验代码层的确定性兜底。

**怎么验的**：用 `langchain.agents.create_agent` 起**真图**（`GenericFakeChatModel`
脚本化产出 tool_calls），中间件挂真的 `CubeRouteGateMiddleware`，
`config={"configurable":{"db_name":…,"thread_id":…}}` 走真实
`langgraph.config.get_config()` —— 中间件链、`ToolCallRequest`、线程标识全是真的。
唯一打桩的两处：① 工具函数本身（闭包计数，用来断言"到底执行了没有"）；
② `semantic_db.get_detector`（返回把库指向**临时 cube 目录**的假探测器，免得依赖
工作区里恰好有哪些库）。**①的正例夹具是 trace 里那条生产 SQL 的逐字原文**。

验十组：
  ⓪ 前置：真图里能读到 db_name；临时 cube 元数据读得出来；正则/判据是判别性的。
  ① 正例（真事故 SQL）：**不执行**，文案点名 cube + 给 `query_cube` 形状 + 逃生口。
  ② 负对照：多表 JOIN（关系集 ⊄ 单底座）⇒ 执行。
  ③ 负对照：明细查询（无聚合无 GROUP BY）⇒ 执行。
  ④ 负对照：cube 通道四工具（含**塞了同一条 SQL 参数**的 `query_cube`）⇒ 执行。
  ⑤ 负对照：未建模库 / 库名取不到 ⇒ 执行。
  ⑥ 每线程一次：同 thread 第二次 ⇒ 执行（**这就是逃生口的机制保证**）。
  ⑦ fail-open：sql 缺失 / 非 str / 无 cubes 目录 / 坏 YAML / args 非 dict ⇒ 执行且不抛。
  ⑧ 加法纪律：`load_cube_metadata` 既有六键与改动前**逐字等价**，新键只是新增。
  ⑨ 接线闸：AST 断言 `nl2sql_agent.py` 里挂了 `CubeRouteGateMiddleware()`。
  ⑩ 开关：`NL2SQL_CUBE_ROUTE_GATE=off` ⇒ 正例也执行（一键回退）。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_cube_route_gate.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile

# ⚠️ 必须在 import 业务模块之前：落盘目录都由它推导
os.environ.setdefault("AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-cubegate-"))
# 真图 invoke 会把每次 tool 调用当 run 上报（本机若开着 tracing → 403 噪声刷屏）
for _k in ("LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_TRACING"):
    os.environ[_k] = "false"

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []

MODELED = "witops"          # 已建模，且项目指向本脚本的临时 cube 目录
UNMODELED = "nowhere_db"    # 未建模（project_path_for → None）

BASE = pathlib.Path(os.environ["AGENT_DATA_ROOT"])
PROJ = BASE / "_proj_cube"
PROJ_NOCUBES = BASE / "_proj_nocubes"
PROJ_BADYAML = BASE / "_proj_badyaml"

# ── ① 正例夹具：trace 9afcd8ea 里模型手写的那条 SQL（逐字原文，只去掉了首尾空白）──
TRACE_SQL = (
    "SELECT m.ym AS ym,\n"
    "       COALESCE(n.new_cnt, 0) AS new_bug_cnt,\n"
    "       COALESCE(d.done_cnt, 0) AS done_bug_cnt,\n"
    "       COALESCE(a.done_any_cnt, 0) AS done_any_cnt,\n"
    "       COALESCE(c.created_cnt, 0) AS created_bug_cnt\n"
    "FROM (SELECT '2026-08' AS ym UNION ALL SELECT '2026-09' AS ym) m\n"
    "LEFT JOIN (\n"
    "  SELECT DATE_FORMAT(mention_time, '%Y-%m') AS ym, COUNT(*) AS new_cnt\n"
    "  FROM v_bug\n"
    "  WHERE mention_time >= '2026-08-01' AND mention_time < '2026-10-01'\n"
    "  GROUP BY DATE_FORMAT(mention_time, '%Y-%m')\n"
    ") n ON n.ym = m.ym\n"
    "LEFT JOIN (\n"
    "  SELECT DATE_FORMAT(complete_time, '%Y-%m') AS ym, COUNT(*) AS done_cnt\n"
    "  FROM v_bug\n"
    "  WHERE complete_time >= '2026-08-01' AND complete_time < '2026-10-01'\n"
    "    AND status_code = 2\n"
    "  GROUP BY DATE_FORMAT(complete_time, '%Y-%m')\n"
    ") d ON d.ym = m.ym\n"
    "LEFT JOIN (\n"
    "  SELECT DATE_FORMAT(create_time, '%Y-%m') AS ym, COUNT(*) AS created_cnt\n"
    "  FROM v_bug\n"
    "  WHERE create_time >= '2026-08-01' AND create_time < '2026-10-01'\n"
    "  GROUP BY DATE_FORMAT(create_time, '%Y-%m')\n"
    ") c ON c.ym = m.ym\n"
    "ORDER BY ym"
)
DETAIL_SQL = "SELECT id, title, status_code FROM v_bug WHERE mention_time > '2026-08-01'"
MULTI_SQL = (
    "SELECT COUNT(*) FROM v_bug b JOIN do_story s ON s.id = b.story_id "
    "GROUP BY s.iteration_id"
)

_CUBE_YML = """\
name: bug_quality
description: 缺陷质量：新增、完成、遗留
base_object: v_bug
measures:
  - name: bug_count
    expression: COUNT(*)
    description: 新增缺陷数
  - name: finished_count
    expression: SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END)
    description: 已完成缺陷数
dimensions:
  - name: severity
    expression: severity
    description: 严重程度
time_dimensions:
  - name: mention_time
    expression: mention_time
    type: date
    levels: [year, quarter, month, week, day]
  - name: complete_time
    expression: complete_time
    type: date
    levels: [year, quarter, month, week, day]
"""

_CUBE_YML_STORY = """\
name: story_delivery
description: 需求交付
base_object: v_story
measures:
  - name: story_count
    expression: COUNT(*)
    description: 需求数
"""


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ── 环境播种：临时 wren 项目 + 假探测器 ──────────────────────

_PATCHED: dict = {}


def seed() -> None:
    """建两个临时 wren 项目（正常 / 无 cubes 目录 / 坏 YAML），打桩 get_detector。"""
    cubes = PROJ / "cubes"
    (cubes / "bug_quality").mkdir(parents=True, exist_ok=True)
    (cubes / "bug_quality" / "metadata.yml").write_text(_CUBE_YML, encoding="utf-8")
    (cubes / "story_delivery").mkdir(parents=True, exist_ok=True)
    (cubes / "story_delivery" / "metadata.yml").write_text(
        _CUBE_YML_STORY, encoding="utf-8"
    )
    PROJ_NOCUBES.mkdir(parents=True, exist_ok=True)
    (PROJ_BADYAML / "cubes" / "broken").mkdir(parents=True, exist_ok=True)
    # 坏 YAML： tab/未闭合括号让 safe_load 抛 —— 必须走 load_cube_metadata 的 except
    (PROJ_BADYAML / "cubes" / "broken" / "metadata.yml").write_text(
        "name: broken\nbase_object: [unclosed\n", encoding="utf-8"
    )

    from agent.utils import semantic_db

    class _FakeDetector:
        def project_path_for(self, db_name: str) -> str | None:  # noqa: D401
            mapping = {
                MODELED: str(PROJ),
                "nocubes": str(PROJ_NOCUBES),
                "badyaml": str(PROJ_BADYAML),
            }
            return mapping.get(str(db_name or ""))

    _PATCHED["get_detector"] = semantic_db.get_detector
    semantic_db.get_detector = lambda: _FakeDetector()  # type: ignore[assignment]


def restore() -> None:
    from agent.utils import semantic_db

    if "get_detector" in _PATCHED:
        semantic_db.get_detector = _PATCHED["get_detector"]  # type: ignore[assignment]


# ── 真图执行 ────────────────────────────────────────────────

_TC_SEQ = {"n": 0}


def run_case(
    tool_name: str,
    args: dict,
    *,
    db: str | None = MODELED,
    thread_id: str | None = None,
    tool_names: tuple[str, ...] = (),
) -> dict:
    """跑一次真实 agent 图，返回 {content, status, ran}。

    `ran` = 工具函数**真的执行了**没有（闭包计数）⇒ 「拦住了」的判别性断言。
    不传 `thread_id` 时自动生成唯一值 —— 软闸是**每线程一次**，各用例共用一个
    thread 会让第一条吃掉后面所有的提醒额度（假绿）。
    """
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool

    from agent.middlewares.cube_route_gate import CubeRouteGateMiddleware

    seen = {"ran": 0}

    class _Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):  # type: ignore[no-untyped-def]
            return self

    def _mk(name: str):
        def _fn(sql=None, cube=None, measures=None, limit=None, **kw) -> str:
            seen["ran"] += 1
            return f"ROWS:{name}"

        _fn.__name__ = name
        _fn.__doc__ = f"{name}（桩：只记调用）"
        # ⚠️ `sql: Any`（不写 str）：写 str 会被 pydantic 把 123 强转成 "123"，
        # ⑦ 的「sql 非 str」就变成一条恒真断言了。
        _fn.__annotations__ = {
            "sql": object, "cube": str, "measures": list, "limit": int, "kw": dict,
        }
        return tool(_fn)

    bound = [_mk(n) for n in (tool_name, *tool_names)]
    model = _Model(messages=iter([
        AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "c1"}]),
        AIMessage(content="finish"),
    ]))
    agent = create_agent(
        model=model, tools=bound, middleware=[CubeRouteGateMiddleware()]
    )
    _TC_SEQ["n"] += 1
    tid = thread_id or f"tid-{_TC_SEQ['n']}"
    configurable: dict = {"thread_id": tid}
    if db is not None:
        configurable["db_name"] = db
    out = agent.invoke(
        {"messages": [{"role": "user", "content": "查一下"}]},
        config={"configurable": configurable},
    )
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    last = tool_msgs[-1] if tool_msgs else None
    return {
        "content": str(getattr(last, "content", "") or ""),
        "status": str(getattr(last, "status", "") or ""),
        "ran": seen["ran"],
    }


def blocked(res: dict, tool_name: str) -> tuple[bool, str]:
    """拦截判据：**没执行** + `status="error"` + `Error:` 前缀 + 点名本门。"""
    ok = (
        res["ran"] == 0
        and res["status"] == "error"
        and res["content"].startswith("Error:")
        and "[cube_route_gate]" in res["content"]
    )
    return ok, f"ran={res['ran']} status={res['status']} content={res['content'][:90]!r}"


def executed(res: dict, tool_name: str) -> tuple[bool, str]:
    """放行判据：执行了，且返回的是工具自己的结果（不是任何中间件的文案）。"""
    ok = res["ran"] == 1 and res["content"] == f"ROWS:{tool_name}"
    return ok, f"ran={res['ran']} content={res['content'][:90]!r}"


# ── ⓪ 前置 ──────────────────────────────────────────────────

def t0_preconditions() -> None:
    section("⓪ 前置：真图读得到 db_name、夹具读得出 cube、判据是判别性的")
    from agent.middlewares.cube_route_gate import (
        _cube_index, _has_aggregate, _matched_cubes, _relations,
    )

    # 真图里 db_name 真的能从 configurable 读到（否则 ⑤ 全部退化成恒真）
    probe = run_case("probe_tool", {"sql": TRACE_SQL}, tool_names=())
    check(probe["ran"] == 1, "非 run_sql 工具照常执行（中间件不越界）", f"ran={probe['ran']}")

    rels = _relations(TRACE_SQL)
    check(rels == {"v_bug"}, "真事故 SQL 的关系集合 = {v_bug}（4 段子查询取自同一底座）",
          str(sorted(rels)))
    check(_has_aggregate(TRACE_SQL), "真事故 SQL 判为聚合查询")
    check(not _has_aggregate(DETAIL_SQL) and _relations(DETAIL_SQL) == {"v_bug"},
          "明细 SQL：关系集合相同但无聚合 → 判据的区分度在聚合这一条上")

    index = _cube_index(str(PROJ))
    check(sorted(index) == ["v_bug", "v_story"],
          "临时项目里两个 cube 的 base_object 都建进索引了", str(sorted(index)))
    check(_matched_cubes(rels, index) == [("v_bug", "bug_quality",
                                           ["bug_count", "finished_count"])],
          "关系集 ⊆ 单底座 ⇒ 命中 bug_quality（measure 名单一并带出）",
          str(_matched_cubes(rels, index)))
    check(_matched_cubes({"v_bug", "do_story"}, index) == [],
          "关系集含底座之外的表 ⇒ **不**命中（跨源手写合理，放行）")


# ── ① 正例 ──────────────────────────────────────────────────

def t1_positive() -> None:
    section("① 正例（真事故 SQL）：拦下 + 文案齐备 + **handler 未被调用**")
    res = run_case(f"wrenai_{MODELED}_run_sql", {"sql": TRACE_SQL})
    check(*blocked(res, "run_sql"), "单底座手写聚合 SQL **不执行**")
    c = res["content"]
    check("bug_quality" in c and "v_bug" in c, "文案点名命中的 cube 与底座")
    check("bug_count" in c and "finished_count" in c, "文案列出该 cube 的 measure 名单")
    check(f"wrenai_{MODELED}_query_cube" in c and "measures=" in c and "time_dimension" in c,
          "文案给出正确的 `query_cube` 调用形状（带真实工具前缀）")
    check("原样重发" in c and "不会再拦" in c, "文案含**逃生口**（否则模型会卡死在这一步）")
    check("分次调用" in c, "文案点明「一次一条时间轴 ⇒ 分次调用并列」这条新政策")


# ── ②~④ 负对照 ──────────────────────────────────────────────

def t2_multi_table() -> None:
    section("② 负对照：多表 JOIN（关系集 ⊄ 单底座）⇒ 照常执行")
    res = run_case(f"wrenai_{MODELED}_run_sql", {"sql": MULTI_SQL})
    check(*executed(res, f"wrenai_{MODELED}_run_sql"),
          "v_bug JOIN do_story 的聚合查询**执行**（cube 表达不了跨源关联）")


def t3_detail() -> None:
    section("③ 负对照：明细查询（无聚合无 GROUP BY）⇒ 照常执行")
    res = run_case(f"wrenai_{MODELED}_run_sql", {"sql": DETAIL_SQL})
    check(*executed(res, f"wrenai_{MODELED}_run_sql"),
          "同一底座上的行级明细查询**执行**（cube 出不了明细行）")


def t4_cube_channel() -> None:
    section("④ 负对照：cube 通道四工具 ⇒ 照常执行")
    for tail in ("query_cube", "describe_cube", "list_cubes", "plan_cube_sql"):
        name = f"wrenai_{MODELED}_{tail}"
        # 刻意给 query_cube 塞一条**同款 SQL 参数**：若闸门只按 sql 判而不看工具名，
        # 这一条会被误拦 —— 断言才有区分度（真 query_cube 没有 sql 形参，多传
        # 由 **kw 吞掉，桩照常执行）。
        res = run_case(name, {"sql": TRACE_SQL, "cube": "bug_quality"})
        check(*executed(res, name), f"`{tail}` 不归本门管辖（Cube 车道自身）")


def t5_unmodeled() -> None:
    section("⑤ 负对照：未建模库 / 库名取不到 ⇒ 照常执行")
    res = run_case(f"wrenai_{UNMODELED}_run_sql", {"sql": TRACE_SQL}, db=UNMODELED)
    check(*executed(res, f"wrenai_{UNMODELED}_run_sql"),
          "未建模库（project_path_for → None）放行：没有 cube 可谈")
    res = run_case(f"wrenai_{MODELED}_run_sql", {"sql": TRACE_SQL}, db=None)
    check(*executed(res, f"wrenai_{MODELED}_run_sql"),
          "configurable 里取不到 db_name ⇒ fail-open 放行")
    res = run_case("dbmcp_run_sql", {"sql": TRACE_SQL, "db_name": MODELED})
    check(*executed(res, "dbmcp_run_sql"),
          "`dbmcp_*` 直连通道不归本门（已建模库上由 QueryGate 通道硬闸兜）")


# ── ⑥ 每线程一次 = 逃生口 ───────────────────────────────────

def t6_once_per_thread() -> None:
    section("⑥ 每线程一次：同 thread 第二次 ⇒ 执行（**逃生口的机制保证**）")
    tid = "tid-escape-hatch"
    first = run_case(f"wrenai_{MODELED}_run_sql", {"sql": TRACE_SQL}, thread_id=tid)
    check(*blocked(first, "run_sql"), "第一次拦下")
    second = run_case(f"wrenai_{MODELED}_run_sql", {"sql": TRACE_SQL}, thread_id=tid)
    check(*executed(second, f"wrenai_{MODELED}_run_sql"),
          "同线程原样重发 ⇒ **执行**（软门不无限弹回）")
    third = run_case(f"wrenai_{MODELED}_run_sql", {"sql": TRACE_SQL}, thread_id="tid-other")
    check(*blocked(third, "run_sql"), "换一个 thread ⇒ 额度独立（不是全局只拦一次）")


# ── ⑦ fail-open ─────────────────────────────────────────────

def t7_fail_open() -> None:
    section("⑦ fail-open：异常输入一律放行且不抛")
    name = f"wrenai_{MODELED}_run_sql"
    check(*executed(run_case(name, {}), name), "sql 缺失 ⇒ 放行（由工具自己报参数缺失）")
    check(*executed(run_case(name, {"sql": 123}), name), "sql 非 str ⇒ 放行")
    check(*executed(run_case(name, {"sql": "   "}), name), "sql 空白 ⇒ 放行")
    check(*executed(run_case(name, {"sql": TRACE_SQL}, db="nocubes"), name),
          "项目里没有 `cubes/` 目录 ⇒ 放行")
    check(*executed(run_case(name, {"sql": TRACE_SQL}, db="badyaml"), name),
          "`metadata.yml` 是坏 YAML ⇒ 放行（load_cube_metadata 的 except 兜住）")

    # args 非 dict：真图产不出这种 tool_call，直接喂 request（仍是真中间件入口）
    from agent.middlewares.cube_route_gate import CubeRouteGateMiddleware
    import types

    mw = CubeRouteGateMiddleware()
    for bad in ("not-a-dict", None, 42, ["x"]):
        req = types.SimpleNamespace(tool_call=bad, runtime=None, state=None)
        try:
            out = mw._gate(req, lambda r: "PASSED")
            check(out == "PASSED", f"`tool_call` 是 {type(bad).__name__} ⇒ 放行且不抛")
        except Exception as e:  # noqa: BLE001
            check(False, f"`tool_call` 是 {type(bad).__name__} ⇒ 抛了异常", repr(e))


# ── ⑧ 加法纪律 ──────────────────────────────────────────────

def t8_additive_keys() -> None:
    section("⑧ 加法纪律：`load_cube_metadata` 既有六键逐字不变，新键只是新增")
    from agent.utils.wren_call_extract import load_cube_metadata

    got = load_cube_metadata(str(PROJ), "bug_quality")
    legacy = {
        "cube_description": "缺陷质量：新增、完成、遗留",
        "measures": {"bug_count": "新增缺陷数", "finished_count": "已完成缺陷数"},
        "dimensions": {"severity": "严重程度"},
        "measure_expressions": {
            "bug_count": "COUNT(*)",
            "finished_count": "SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END)",
        },
        "dimension_expressions": {"severity": "severity"},
        "time_dimensions": {
            "mention_time": {"expression": "mention_time", "type": "date",
                             "levels": ["year", "quarter", "month", "week", "day"]},
            "complete_time": {"expression": "complete_time", "type": "date",
                              "levels": ["year", "quarter", "month", "week", "day"]},
        },
    }
    check({k: got.get(k) for k in legacy} == legacy,
          "既有六键（含值）与改动前**逐字相等**",
          f"diff={[k for k in legacy if got.get(k) != legacy[k]]}")
    check(sorted(got) == sorted([*legacy, "cube_name", "base_object"]),
          "键集合 = 既有六键 + cube_name + base_object（没有多也没有少）",
          str(sorted(got)))
    check(got.get("cube_name") == "bug_quality" and got.get("base_object") == "v_bug",
          "新键取值来自 YAML 声明（不是目录名）",
          f"{got.get('cube_name')!r} / {got.get('base_object')!r}")

    # 搬家后的 list_cubes 仍是同一份实现（attribution_analyze 的别名指向同一个对象）
    from agent.tools.attribution_analyze import _list_cubes
    from agent.utils import wren_call_extract

    check(_list_cubes is wren_call_extract.list_cubes,
          "`attribution_analyze._list_cubes` 与搬家后的实现是**同一个对象**（零行为变化）")
    check(_list_cubes(str(PROJ)) == ["bug_quality", "story_delivery"],
          "列 cube 目录：只列带 metadata.yml 的目录", str(_list_cubes(str(PROJ))))
    check(_list_cubes(str(PROJ_NOCUBES)) == [], "无 cubes 目录 ⇒ 空集（不抛）")


# ── ⑨ 接线闸 ────────────────────────────────────────────────

def t9_mounted() -> None:
    section("⑨ 接线闸：中间件真的挂在 nl2sql 子 agent 的链上")
    src = (_HERE.parents[1] / "src" / "agent" / "graphs" / "nl2sql_agent.py").read_text(
        encoding="utf-8"
    )
    found = False
    for node in ast.walk(ast.parse(src)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and node.args
            and isinstance(node.args[0], ast.Call)
            and getattr(node.args[0].func, "id", "") == "CubeRouteGateMiddleware"
        ):
            found = True
    # 文本/AST 级断言（不 import 该模块——它在 import 期就建模型、要连 provider）。
    # 只保证"接线没被摘掉"，行为由 ①~⑦ 的真图验证负责。
    check(found, "`_middleware.append(CubeRouteGateMiddleware())` 在 nl2sql_agent.py 里")


# ── ⑩ 开关 ──────────────────────────────────────────────────

def t10_switch() -> None:
    section("⑩ 开关：`NL2SQL_CUBE_ROUTE_GATE=off` ⇒ 正例也放行（一键回退）")
    from agent.middlewares.cube_route_gate import _ENV_GATE, _enabled

    check(_enabled(), f"默认开（未设 {_ENV_GATE}）")
    old = os.environ.get(_ENV_GATE)
    os.environ[_ENV_GATE] = "off"
    try:
        check(not _enabled(), "off ⇒ 关")
        res = run_case(f"wrenai_{MODELED}_run_sql", {"sql": TRACE_SQL})
        check(*executed(res, f"wrenai_{MODELED}_run_sql"),
              "**开关生效**：正例夹具在 off 下照常执行")
    finally:
        if old is None:
            os.environ.pop(_ENV_GATE, None)
        else:
            os.environ[_ENV_GATE] = old
    check(_enabled(), "恢复后重新生效（改动没有把环境变量改坏）")


def main() -> int:
    print(f"临时 AGENT_DATA_ROOT = {os.environ['AGENT_DATA_ROOT']}")
    seed()
    try:
        t0_preconditions()
        t1_positive()
        t2_multi_table()
        t3_detail()
        t4_cube_channel()
        t5_unmodeled()
        t6_once_per_thread()
        t7_fail_open()
        t8_additive_keys()
        t9_mounted()
        t10_switch()
    finally:
        restore()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
