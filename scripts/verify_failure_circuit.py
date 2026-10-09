# -*- coding: utf-8 -*-
"""失败熔断中间件验证（FailureCircuitMiddleware）。

复现的形态（trace ec484c693f6e9673a41a2b6487abc4d5，2026-10-09）：
    语义库里删掉 model `do_bug` 后，模型拿到 `table 'wren.public.do_bug' not found`
    没有收手，在 dry_run / run_sql / query_cube / dry_plan 之间改了 **18 次**，
    首败→末败 406.7s（整个 turn 才 433.9s），最终以 402 余额耗尽收场。

本脚本断言的正是「这 18 次本该在第 2 次就收手」所依赖的每一条口径：

    1/6 签名解析 —— (错误族, 归一化对象)；非对象错误必须**认不出**（负对照）
    2/6 计数口径 —— 跨工具、跨措辞都归一个签名；且只算**本次 run**（最后一条
                    HumanMessage 之后），线程复用不误伤
    3/6 升级三档 —— 第 1 次给信息、第 2 次叫停、第 3 次熔断
    4/6 硬闸 —— 已达 STOP 的对象再被引用则**不执行**；换对象名（do_bug_wide）
                不许误伤（负对照）
    5/6 失败预算 —— 跨对象总量封顶
    6/6 开关与健壮性 —— off 零动作、无签名原样透传、非法阈值回落默认、
                        非 ToolMessage 透传、异步路径同构

运行：
    uv run --no-sync python scripts/verify_failure_circuit.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="nl2sql-verify-fail-circuit-")
os.environ.setdefault("AGENT_DATA_ROOT", _TMP)
# 阈值必须由本脚本自己说了算（外面若设过会污染口径）
for _k in ("NL2SQL_FAIL_CIRCUIT", "NL2SQL_FAIL_CIRCUIT_WARN",
           "NL2SQL_FAIL_CIRCUIT_STOP", "NL2SQL_FAIL_CIRCUIT_TOTAL"):
    os.environ.pop(_k, None)

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from langchain_core.messages import HumanMessage, ToolMessage  # noqa: E402

from agent.middlewares.failure_circuit import (  # noqa: E402
    FailureCircuitMiddleware,
    _norm_object,
    _signature,
)

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []

# 同一个根因的四种措辞（全部必须映射到 ("table","do_bug")）
WREN_ERR = "Error during planning: table 'wren.public.do_bug' not found"
MYSQL_ERR = "Table 'wren.public.do_bug' doesn't exist"
COL_ERR = "Unknown column 'bug_status' in 'field list'"
NOT_OBJ = "Error: permission denied for read on /workspace/large_tool_results"


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


class _Runtime:
    def __init__(self, tool_call_id: str = "tc-1") -> None:
        self.tool_call_id = tool_call_id


class _Req:
    def __init__(self, messages, name="wrenai_run_sql", args=None, tool_call_id="tc-1"):
        self.state = {"messages": list(messages)}
        self.tool_call = {"name": name, "args": args if args is not None else {}}
        self.runtime = _Runtime(tool_call_id)


def _tm(text: str, name: str = "wrenai_run_sql", tid: str = "tc") -> ToolMessage:
    return ToolMessage(content=text, name=name, tool_call_id=tid, status="error")


def _run(mw: FailureCircuitMiddleware, req: _Req, ret: ToolMessage):
    """跑一次同步 wrap_tool_call；返回 (返回的消息, handler 是否被调用)。"""
    called = {"n": 0}

    def handler(_r):
        called["n"] += 1
        return ret

    return mw.wrap_tool_call(req, handler), called["n"]


def main() -> int:
    mw = FailureCircuitMiddleware()   # 默认阈值 2 / 3 / 6

    # ────────────────────────────────────────────────────────────
    print("\n=== 1/6 签名解析 ===")
    check(_signature(WREN_ERR) == ("table", "do_bug"), "wren 规划期措辞",
          f"{_signature(WREN_ERR)}")
    check(_signature(MYSQL_ERR) == ("table", "do_bug"), "MySQL 措辞归到**同一个**签名",
          f"{_signature(MYSQL_ERR)}")
    check(_signature(COL_ERR) == ("column", "bug_status"), "字段不存在单独一族",
          f"{_signature(COL_ERR)}")
    check(_signature(NOT_OBJ) is None,
          "★负对照：权限错误**认不出**（不属于「对象不存在」族，不许误伤）",
          f"{_signature(NOT_OBJ)}")
    check(_norm_object("`wren`.`public`.`do_bug`") == "do_bug", "反引号 + 三段式归一")
    # 标记优先：模型转述/中间件加壳后，签名仍稳定
    check(_signature("（见上）【fail-circuit:table:do_bug】换个说法") == ("table", "do_bug"),
          "机读标记优先于自由文本")

    # ────────────────────────────────────────────────────────────
    print("\n=== 2/6 计数口径：跨工具、跨措辞、只算本次 run ===")
    # 第一次：dry_run 撞 do_bug
    msgs = [_tm(WREN_ERR, name="wrenai_dry_run")]
    # 第二次：换成 run_sql、且**措辞不同**（MySQL 版）—— 必须仍算同一签名
    req = _Req(msgs, name="wrenai_run_sql", args={"sql": "SELECT 1 FROM do_bug"})
    out, n_called = _run(mw, req, _tm(MYSQL_ERR, name="wrenai_run_sql"))
    check(n_called == 1, "handler 正常执行（这是后处理，不是拦截）")
    check("第 2 次" in out.content,
          "★换工具 + 换措辞仍算第 2 次（证明签名**不含工具名**）",
          out.content.splitlines()[0][:60])
    check("【fail-circuit:table:do_bug】" in out.content, "改写后带机读标记")

    # 作用域：HumanMessage **之前**的失败不计入本次 run
    req2 = _Req(
        [_tm(WREN_ERR, name="a"), _tm(WREN_ERR, name="b"),
         HumanMessage(content="第二个问题"), _tm(WREN_ERR, name="c")],
        name="wrenai_dry_run",
    )
    out2, _ = _run(mw, req2, _tm(WREN_ERR))
    check("第 2 次" in out2.content and "第 3 次" not in out2.content,
          "★上一问的失败不计入（从最后一条 HumanMessage 起算）")
    check(out2.content.count("【fail-circuit:table:do_bug】") == 1, "标记只出现一次")

    # ────────────────────────────────────────────────────────────
    print("\n=== 3/6 升级三档 ===")
    out3, _ = _run(mw, _Req([], name="wrenai_dry_run"), _tm(WREN_ERR))
    check("如实向用户说明" in out3.content and "list_models" in out3.content,
          "第 1 次：给「去核实 / 如实说明」的可收手信息")
    check("第 2 次" not in out3.content, "第 1 次不提「第 N 次」")

    req4 = _Req([_tm(WREN_ERR)], name="wrenai_query_cube")
    out4, _ = _run(mw, req4, _tm(MYSQL_ERR))
    check("停止重试" in out4.content and "已被熔断" not in out4.content,
          "第 2 次：明确叫停（且还没到熔断档）")

    req5 = _Req([_tm(WREN_ERR), _tm(WREN_ERR), _tm(WREN_ERR)], name="wrenai_dry_plan")
    out5, _ = _run(mw, req5, _tm(WREN_ERR))
    check("已被熔断" in out5.content and "第 4 次" not in out5.content,
          "第 3 次起：终态熔断措辞",
          out5.content.splitlines()[0][:60])

    # ────────────────────────────────────────────────────────────
    print("\n=== 4/6 硬闸（已达 STOP 的对象再被引用则**不执行**）===")
    blocked_state = [_tm(WREN_ERR, name=n) for n in
                     ("wrenai_dry_run", "wrenai_run_sql", "wrenai_query_cube")]
    req6 = _Req(blocked_state, name="wrenai_run_sql",
                args={"sql": "SELECT count(*) FROM wren.public.do_bug"})
    out6, called6 = _run(mw, req6, _tm(WREN_ERR))
    check(called6 == 0, "硬闸真的拦在 handler 之前（工具没被执行）")
    check(out6.status == "error" and "已被熔断" in out6.content,
          "拒绝消息是 status=error 的 ToolMessage")
    check(out6.tool_call_id == "tc-1", "tool_call_id 回填（否则 LangGraph 配对错位）")

    req7 = _Req(blocked_state, name="wrenai_run_sql",
                args={"sql": "SELECT count(*) FROM do_bug_wide"})
    out7, called7 = _run(mw, req7, _tm(WREN_ERR))
    check(called7 == 1, "★负对照：do_bug_wide 是**另一个对象**，不许被 do_bug 的闸误伤")

    req8 = _Req(blocked_state, name="wrenai_run_sql", args={"sql": "SELECT 1"})
    _, called8 = _run(mw, req8, _tm(WREN_ERR))
    check(called8 == 1, "负对照：args 里没提到该对象则不拦")

    # ────────────────────────────────────────────────────────────
    print("\n=== 5/6 失败预算：跨对象总量封顶（只加措辞、不硬拦）===")
    five = [_tm(f"table 'wren.public.obj_{i}' not found") for i in range(5)]
    req9 = _Req(five, name="wrenai_dry_run", args={"sql": "SELECT 1"})
    out9, called9 = _run(mw, req9, _tm("table 'wren.public.obj_5' not found"))
    check(called9 == 1, "预算档**不拦截**（跨对象硬拦误伤面太大）")
    check("失败预算" in out9.content, "第 6 次（跨 6 个不同对象）触发预算提示",
          out9.content.strip().splitlines()[-1][:70])
    req10 = _Req(five[:2], name="wrenai_dry_run", args={"sql": "SELECT 1"})
    out10, _ = _run(mw, req10, _tm("table 'wren.public.obj_2' not found"))
    check("失败预算" not in out10.content, "只累计到 3 次时不提预算")

    # ────────────────────────────────────────────────────────────
    print("\n=== 6/6 开关与健壮性 ===")
    # ⑥-1 off：零动作
    os.environ["NL2SQL_FAIL_CIRCUIT"] = "off"
    try:
        raw = _tm(WREN_ERR)
        out11, _ = _run(mw, _Req([], name="wrenai_dry_run"), raw)
        check(out11 is raw and out11.content == WREN_ERR,
              "NL2SQL_FAIL_CIRCUIT=off ⇒ 原样返回（一键回退）")
    finally:
        os.environ.pop("NL2SQL_FAIL_CIRCUIT", None)

    # ⑥-2 无签名结果原样透传（连对象都不换）
    ok_msg = ToolMessage(content="Query OK, 12 rows", name="wrenai_run_sql",
                         tool_call_id="tc-9")
    out12, _ = _run(mw, _Req([], name="wrenai_run_sql"), ok_msg)
    check(out12 is ok_msg, "成功结果原样透传（同一个对象，未复制）")

    # ⑥-3 非 ToolMessage 透传
    out13 = mw._after(_Req([]), "not-a-message")
    check(out13 == "not-a-message", "非 ToolMessage 透传")

    # ⑥-4 非法阈值回落默认（不因配置写错而改变行为）
    os.environ["NL2SQL_FAIL_CIRCUIT_WARN"] = "abc"
    os.environ["NL2SQL_FAIL_CIRCUIT_STOP"] = "0"
    try:
        mw2 = FailureCircuitMiddleware()
        check(mw2._warn_at == 2 and mw2._stop_at == 3,
              "非法阈值回落默认（'abc'→2、'0'→3）",
              f"warn={mw2._warn_at} stop={mw2._stop_at}")
    finally:
        os.environ.pop("NL2SQL_FAIL_CIRCUIT_WARN", None)
        os.environ.pop("NL2SQL_FAIL_CIRCUIT_STOP", None)

    # ⑥-5 异步路径与同步同构
    async def _ascenario():
        req = _Req([_tm(WREN_ERR), _tm(WREN_ERR)], name="wrenai_run_sql",
                   args={"sql": "SELECT 1 FROM do_bug"})

        async def handler(_r):
            return _tm(WREN_ERR, name="wrenai_run_sql")

        return await mw.awrap_tool_call(req, handler)

    async_out = asyncio.run(_ascenario())
    check("已被熔断" in async_out.content,
          "awrap_tool_call 的计数/升级与同步一致（第 3 次）",
          async_out.content.splitlines()[0][:60])

    # ────────────────────────────────────────────────────────────
    bad = [label for ok, label in results if not ok]
    print(f"\n=== 结果：{len(results) - len(bad)}/{len(results)} 通过 ===")
    for label in bad:
        print(f"  ✗ {label}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
