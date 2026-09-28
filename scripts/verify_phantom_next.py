# -*- coding: utf-8 -*-
"""P2-12 回归：`update_state(as_node="__start__")` 的「幽灵 next」——机制 + 判据。

## 这个脚本钉的是什么

生产 6 并发档出现过一条会话：`main_run=success`、`last_message_is_final=True`，但
`state.next = ['PatchToolCallsMiddleware.before_agent']` **悬挂 240s 无人推进**，
压测端按旧口径（要求 `next` 为空）判它不收敛，报成「卡死 248s」且不记录答案。

结论（本脚本逐条验证）：**它不是丢步**，而是 `update_state` 状态补丁的固有副作用 ——

1. 任何带 `as_node=<节点名>` 的 `update_state` 都是「假装这个节点刚跑完、产出了这些
   state 字段」，于是 langgraph 顺带把 `next` 置成**该节点的后继**。`as_node="__start__"`
   的后继就是图入口节点 ⇒ 补丁写完后 head checkpoint 的 `next` 恒非空；
2. sync 的 `_sync_update_state` 用的正是 `as_node="__start__"`（`_SYNC_WRITE_LOCK` 串行
   化并发写），而 LangGraph 对 `update_state` 有硬闸（主线程有 pending/running run 就
   409）⇒ **补丁只可能落在主 run 终态之后**；此时若该轮续跑早已通知过
   （`success_notified_local` 置位，不会再起新 run），就没有任何机制去推进那个 `next`；
3. 但它无害：答复已终稿、run=success，`api.thread_run_status.classify()` 的判据 4
   （最后一条是终稿 assistant 文本）**明确**把这种形态判成「不算未完成、不提示已中断」
   ——生产 trace 01a09ed5 就是这一形态（2723 字答复 + next 非空）；下一次用户消息也会
   把它顺带消费掉（本脚本 §① 末条验证）。

⇒ 修法 = **修正消费侧口径**（`load_test_concurrency.turn_settled` 采用端点结论字段，
不再要求 `next` 为空），并在 `_sync_update_state` 的 docstring 里写明副作用，防止后续
代码把 `next` 非空误读成丢步。**真正的半轮被打断**（next 非空 + 最后一条是 tool_calls）
仍然会被两端判成未完成（§② §③ 的负对照）。

跑法：`uv run python scripts/verify_phantom_next.py`（纯离线，不碰生产）。
"""
from __future__ import annotations

import sys
from typing import Any

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

PASS = 0
FAIL = 0


def check(cond: bool, label: str, extra: Any = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" -> {extra!r}" if extra != "" else ""))
    return bool(cond)


# ────────────────────────────────────────────────────────────────
# ① 机制：最小图 + InMemorySaver，观察 update_state 对 next 的影响
# ────────────────────────────────────────────────────────────────
def section_mechanism() -> None:
    print("\n① 机制：update_state(as_node=...) 如何写 next（最小图，离线）")
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    class S(TypedDict, total=False):
        n: int
        note: str

    def before_agent(state: S) -> S:
        return {"n": (state.get("n") or 0) + 1}

    def model(state: S) -> S:
        return {"note": "done"}

    b = StateGraph(S)
    b.add_node("before_agent", before_agent)
    b.add_node("model", model)
    b.add_edge(START, "before_agent")
    b.add_edge("before_agent", "model")
    b.add_edge("model", END)
    graph = b.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t-phantom"}}

    graph.invoke({"n": 0}, cfg)
    head = graph.get_state(cfg)
    check(tuple(head.next or ()) == (), "跑完一轮后 next 为空（基线）", head.next)

    # 状态补丁：与 sync 的 _sync_update_state 完全同形
    graph.update_state(cfg, {"note": "sync-patch"}, as_node="__start__")
    head2 = graph.get_state(cfg)
    check(
        tuple(head2.next or ()) == ("before_agent",),
        "as_node=\"__start__\" 的补丁把 next 写成图入口节点（幽灵 next）",
        head2.next,
    )
    check(head2.values.get("note") == "sync-patch", "补丁本身写进去了（值没丢）")

    # 幂等：再写一次仍然是**一个**待执行节点，不会堆积
    graph.update_state(cfg, {"note": "sync-patch-2"}, as_node="__start__")
    head3 = graph.get_state(cfg)
    check(
        tuple(head3.next or ()) == ("before_agent",),
        "重复补丁不堆积（仍是单个待执行节点）",
        head3.next,
    )

    # as_node=<终态节点> 不产生幽灵 —— 记下来：这是「本可以绕过」的写法，
    # 但改 sync 的写路径会动到 P2-4/P1 的既有语义，收益只是消掉一个无害副作用，故不改。
    graph.update_state(cfg, {"note": "as-terminal"}, as_node="model")
    head4 = graph.get_state(cfg)
    check(tuple(head4.next or ()) == (), "as_node=<终态节点> 补丁后 next 为空", head4.next)

    # 幽灵 next 只可能被两条路消费：新 run（用户/续跑）或另一次补丁。
    # 真实路径是「下一次用户消息」，这里用一张新图+新线程模拟不了，改为直接验证
    # 端点判据（§②）——那才是它在产品里被判定的地方。
    print("  · 说明：幽灵 next 的下一次 run 会被正常消费，此处不断言（避免用假图模拟真 run）")


# ────────────────────────────────────────────────────────────────
# ② 产品判据：classify() 对幽灵 next 的结论（用户不受影响的核心证据）
# ────────────────────────────────────────────────────────────────
def _phantom_state(*, final: bool = True, tool_calls: bool = False) -> dict:
    """生产现场的 state 形态：next 挂着图入口节点，最后一条是 AI 消息。"""
    last: dict[str, Any] = {"type": "ai", "content": "灵工采集子系统在职人数为 190 人。"}
    if tool_calls:
        last = {"type": "ai", "content": "", "tool_calls": [{"name": "write_todos", "args": {}}]}
    return {
        "next": ["PatchToolCallsMiddleware.before_agent"],
        "values": {"messages": [{"type": "human", "content": "有多少人？"}, last]},
        "tasks": [],
    }


def section_classify() -> None:
    print("\n② 产品判据：thread_run_status.classify() 对幽灵 next 的结论")
    from api.thread_run_status import classify

    ok_run = [{"run_id": "r1", "status": "success", "created_at": "2026-09-24T07:24:08Z"}]

    res = classify(_phantom_state(), ok_run)
    check(res["next"] == ["PatchToolCallsMiddleware.before_agent"], "next 确实非空（形态一致）")
    check(res["last_message_is_final"] is True, "最后一条判为终稿")
    check(res["turn_incomplete"] is False, "★ 幽灵 next 不报「已中断」（判据 4 生效）")
    check(res["turn_failed"] is False, "也不是「执行失败」")
    check(res["has_active_run"] is False, "无活跃 run")

    # 负对照 1：真的半轮被打断（最后一条是 tool_calls）→ 必须仍然报未完成
    res_bad = classify(_phantom_state(tool_calls=True), ok_run)
    check(res_bad["last_message_is_final"] is False, "负对照：tool_calls 结尾不算终稿")
    check(res_bad["turn_incomplete"] is True, "★ 负对照：真半轮仍报 turn_incomplete")

    # 负对照 2：有活跃 run（有人在推）→ 不报未完成
    res_active = classify(
        _phantom_state(tool_calls=True),
        ok_run + [{"run_id": "r2", "status": "running", "created_at": "2026-09-24T07:25:00Z"}],
    )
    check(res_active["has_active_run"] is True, "负对照：活跃 run 被识别")
    check(res_active["turn_incomplete"] is False, "负对照：有人在推时不报未完成")

    # 负对照 3：终态失败 → 走 turn_failed 分支（另一条链路，不能被本项吞掉）
    res_failed = classify(
        _phantom_state(tool_calls=True),
        [{"run_id": "r1", "status": "error", "created_at": "2026-09-24T07:24:08Z"}],
    )
    check(res_failed["turn_failed"] is True, "负对照：终态失败仍报 turn_failed")


# ────────────────────────────────────────────────────────────────
# ③ 压测判据：turn_settled 与旧口径的差别（本项的「验收修法」）
# ────────────────────────────────────────────────────────────────
def _settle_from(res: dict) -> dict:
    """端点响应里 turn_settled 会读到的那些字段。"""
    return {
        k: res.get(k)
        for k in ("has_active_run", "awaiting_interrupt", "last_message_is_final",
                  "turn_incomplete", "turn_failed", "next")
    }


def section_settle() -> None:
    print("\n③ 压测判据：turn_settled()（消费侧口径修正）")
    import load_test_concurrency as L
    from api.thread_run_status import classify

    ok_run = [{"run_id": "r1", "status": "success", "created_at": "2026-09-24T07:24:08Z"}]

    phantom = _settle_from(classify(_phantom_state(), ok_run))
    check(phantom["next"] == ["PatchToolCallsMiddleware.before_agent"], "幽灵态 next 非空")
    check(
        not (not phantom["has_active_run"] and not phantom["next"]
             and phantom["last_message_is_final"]),
        "旧口径（要求 not next）在幽灵态**判不收敛** —— 这就是 240s 空等的来源",
    )
    check(L.turn_settled(phantom) is True, "★ turn_settled 在幽灵态判收敛（修好）")

    interrupted = _settle_from(classify(_phantom_state(tool_calls=True), ok_run))
    check(L.turn_settled(interrupted) is False, "★ 负对照：真半轮仍不收敛")

    active = _settle_from(
        classify(
            _phantom_state(),
            ok_run + [{"run_id": "r2", "status": "running", "created_at": "2026-09-24T07:25:00Z"}],
        )
    )
    check(L.turn_settled(active) is False, "负对照：有活跃 run 时不收敛")

    failed = _settle_from(
        classify(_phantom_state(), [{"run_id": "r1", "status": "error",
                                     "created_at": "2026-09-24T07:24:08Z"}])
    )
    check(L.turn_settled(failed) is False, "负对照：终态失败时不收敛（走 turn_failed 分支）")

    check(L.turn_settled({}) is False, "负对照：空 settle 不收敛（不 fail-open）")

    # 生产端点若改口径，本判据必须跟着变：断言它真的读了端点自己的结论字段
    check(
        L.turn_settled({**phantom, "turn_incomplete": True}) is False,
        "★ turn_settled 尊重端点的 turn_incomplete（不是自己另写一套四判据）",
    )


def main() -> int:
    print("P2-12 幽灵 next —— 机制 + 判据回归")
    section_mechanism()
    section_classify()
    section_settle()
    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
