# -*- coding: utf-8 -*-
"""P1-1 验证：TraceRecorder 的事件必须落在**当前 run 自己**的 thread 上。

复现的并发形态（旧实现的必错窗口）：
    中间件是进程内单例 → 模型调用时把 thread_id 存进实例属性 → 工具调用时读回来。
    「模型调用」与「工具调用」之间隔着**整个 LLM 推理时长**，这期间别的 run 的
    模型调用会覆盖那个属性。于是工具事件挂到最后发起模型调用的那个会话名下。

本脚本按最坏顺序构造（完全串行，不需要真并发就能证伪）：
    1. run A 的模型调用
    2. run B 的模型调用      ← 旧实现会在这里覆盖 _cached_thread_id
    3. run A 的工具调用      ← 旧实现会把它记到 B 名下
断言工具事件落在 A。

运行：
    uv run --no-project python scripts/verify_trace_thread_scope.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="nl2sql-verify-trace-scope-")
os.environ.setdefault("AGENT_DATA_ROOT", _TMP)

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from agent.middlewares.trace_recorder import (  # noqa: E402
    TraceRecorderMiddleware,
    _get_tool_name,
    _summarize_args,
)

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []

THREAD_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
THREAD_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


class _Exec:
    def __init__(self, tid: str) -> None:
        self.thread_id = tid


class _Runtime:
    def __init__(self, tid: str) -> None:
        self.execution_info = _Exec(tid)


class _Req:
    """同时充当 ModelRequest / ToolCallRequest（两条路径都只读 runtime）。"""

    def __init__(self, tid: str, tool_call: dict | None = None) -> None:
        self.runtime = _Runtime(tid)
        if tool_call is not None:
            self.tool_call = tool_call


def main() -> int:
    db = os.path.join(_TMP, "traces.sqlite")
    mw = TraceRecorderMiddleware(db_path=db, agent_type="chat_agent")

    print("\n=== 1/4 属性级回归：实例上不该再有线程 id 缓存 ===")
    check(not hasattr(mw, "_cached_thread_id"),
          "中间件实例不再持有 _cached_thread_id（防回退）")
    check(not hasattr(mw, "_cached_parent_thread_id"),
          "中间件实例不再持有 _cached_parent_thread_id")

    print("\n=== 2/4 工具事件的 thread 归属（交错顺序）===")

    async def model_call(tid: str) -> None:
        async def handler(_req):
            return "ok"

        await mw.awrap_model_call(_Req(tid), handler)

    async def tool_call(tid: str) -> None:
        async def handler(_req):
            return "tool-ok"

        await mw.awrap_tool_call(
            _Req(tid, tool_call={"name": "read_file", "args": {"path": "/workspace/x"}}),
            handler,
        )

    async def scenario() -> None:
        await model_call(THREAD_A)   # 1. A 的模型调用
        await model_call(THREAD_B)   # 2. B 的模型调用（旧实现覆盖缓存）
        await tool_call(THREAD_A)    # 3. A 的工具调用（旧实现会记到 B）

    asyncio.run(scenario())

    a_events = mw._store.query_events(thread_id=THREAD_A, limit=100)
    b_events = mw._store.query_events(thread_id=THREAD_B, limit=100)
    a_tools = [e for e in a_events if str(e.get("event_type", "")).find("tool") >= 0]
    b_tools = [e for e in b_events if str(e.get("event_type", "")).find("tool") >= 0]

    check(bool(a_tools), "A 的事件里**有**工具事件", f"A: {len(a_events)} 条事件 / 工具 {len(a_tools)} 条")
    check(not b_tools, "B 的事件里**没有**工具事件（没被张冠李戴）",
          f"B: {len(b_events)} 条事件 / 工具 {len(b_tools)} 条")

    print("\n=== 3/4 参数摘要不再恒为空（_tc_field 形态归一）===")
    req = _Req(THREAD_A, tool_call={"name": "read_file", "args": {"path": "/workspace/x"}})
    check(_get_tool_name(req) == "read_file", "dict 形态 tool_call 能取到工具名",
          f"name={_get_tool_name(req)!r}")
    check(_summarize_args(req) == "{'path': '/workspace/x'}", "dict 形态能取到入参摘要",
          f"args={_summarize_args(req)!r}")
    if a_tools:
        payload = a_tools[0].get("data") or {}
        check(payload.get("args_summary") not in (None, "", "{}"),
              "落库的工具事件真的带上了入参摘要", f"args_summary={payload.get('args_summary')!r}")

    print("\n=== 4/4 兜底：execution_info 缺失时用 configurable.thread_id ===")
    import langgraph.config as _lgcfg

    class _NoRuntimeReq:
        tool_call = {"name": "noop", "args": {}}

    orig = _lgcfg.get_config
    try:
        _lgcfg.get_config = lambda: {"configurable": {"thread_id": THREAD_B}}
        got = mw._get_thread_id(_NoRuntimeReq())
    finally:
        _lgcfg.get_config = orig
    check(got == THREAD_B, "无 runtime 时回落到 configurable.thread_id", f"got={got!r}")

    bad = [label for ok, label in results if not ok]
    print(f"\n=== 结果：{len(results) - len(bad)}/{len(results)} 通过 ===")
    for label in bad:
        print(f"  ✗ {label}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
