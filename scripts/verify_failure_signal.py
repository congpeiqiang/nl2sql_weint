# -*- coding: utf-8 -*-
"""终局失败信号验证（离线：无后端、无数据库、无网络）。

**要解决的**（2026-09-28 生产事故，trace `3dcc9a66…` / 子 run `01a0e695`）：
`ModelTimeoutMiddleware` 把超时吞成一条**不带任何标记**的友好 `AIMessage`（设计初衷是
"界面不卡"），于是下游三处各自猜错：

1. `CaliberGateMiddleware` 把它当「终稿缺 `## 业务口径` 块」→ `jump_to="model"` 打回 →
   图又发起**第二次** 240s 模型调用（用户白等 234s，整轮 620.8s，一 SQL 未执行）；
2. `check_async_task` 只看 `run.status` ⇒ 主 agent 读到 `status:"success"` + 一段超时
   文案，只能在自己的推理里写「marked as success, but the result is actually a timeout」；
3. watcher 的强制 timeout 与 run 自己的 success 各记一套账（同一条任务两套终态）。

修法＝`agent/utils/failure_signal.py`：给这类消息盖一个**只有代码能盖**的戳，所有终态
消费者改成读戳。

本脚本验七段（每段都带**负对照**——"无标记＝今天的行为逐字不变"是本次的核心不变量）：
  ① **标记工具**：不改正文/返回同一对象/dict 与对象两条读路径/只看末条 AI/值域只许
     `error|timeout`/可 JSON 序列化（进 checkpoint 的前提）。
  ② **三个生产点**：真跑中间件（超时 / 额度耗尽 / 未配模型）→ **文案一字符未改**
     且 kind 正确；超时**仍不重试**（P3-4 策略没被这次改造动过）。
  ③ **CaliberGate 放行**（事故回归闸）：取过知识料 + 末条带戳 → 必须**不打回**；
     **负对照**＝同样的消息去掉戳 → 必须打回（证明断言不是因为函数恒返回 None）。
  ④ **终态降级**：`run=success` + 末条带戳 → `status` 改判 + `error_kind` + 无 `result`；
     **负对照**＝无戳仍 success 且带 result；`running` + 带戳 → 绝不降级。
  ⑤ **权威进度源**：`state.todos` 优先（卡片口径），过时的 messages 回声不许覆盖；
     **负对照**＝拿不到 `state.todos` → 退回原反扫，行为与今天一致。
  ⑥ **watcher 改判 + 分流**（旗舰段，跑真 `_async_sync_loop`）：子 run success + 末条带戳
     → `async_tasks` 落 `timeout` + 原因、发失败汇报、**不发**「已完成」续跑；
     **负对照**＝无戳 → 仍 success + 触发续跑（`verify_pending_terminal` 既有断言的前提）。
  ⑦ **卡死预算时钟**：`created_at` 700s 前 → 首轮即判卡死；**负对照**＝10s 前不判。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-sync python scripts/verify_failure_signal.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import sys
import tempfile

# ⚠️ 必须在 import 业务模块之前：数据落点由 AGENT_DATA_ROOT 推导
_WORK = pathlib.Path(tempfile.mkdtemp(prefix="nl2sql-verify-failsig-"))
os.environ["AGENT_DATA_ROOT"] = str(_WORK)
os.environ.pop("LANGGRAPH_API_URL", None)
os.environ.pop("NL2SQL_AUTH_DISABLED", None)
os.environ.pop("LANGFUSE_ENABLE", None)

results: list[tuple[bool, str]] = []


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


def check(cond, label: str, extra: str = "") -> None:
    results.append((bool(cond), label))
    tail = f"  | {extra}" if extra else ""
    print(f"  {'[OK]  ' if cond else '[FAIL]'} {label}{tail}")


def _model_request():
    """真实 ModelRequest（中间件不读它的字段，但用真的更接近生产）。"""
    from langchain.agents.middleware import ModelRequest

    return ModelRequest(
        model=None, messages=[], system_message=None, tool_choice=None,
        tools=[], response_format=None, state={}, runtime=None, model_settings=None,
    )


# ── ① 标记工具 ─────────────────────────────────────────────────────
def verify_signal_tools() -> None:
    section("① 标记工具（failure_signal）")
    from langchain_core.messages import AIMessage, ToolMessage

    from agent.subagents.sync_subagent_todos import _RUN_DONE_STATUSES
    from agent.utils.failure_signal import (
        FAILURE_KEY,
        KIND_MODEL_REQUIRED,
        KIND_MODEL_TIMEOUT,
        KIND_QUOTA_EXHAUSTED,
        detail_of,
        failed_mark,
        is_failed,
        last_failed_mark,
        mark_failed,
        run_status_for,
    )

    msg = AIMessage(content="原文不动")
    same = mark_failed(msg, KIND_MODEL_TIMEOUT, "原因")
    mark = failed_mark(msg) or {}
    check(same is msg and msg.content == "原文不动",
          "mark_failed 返回同一对象且**不改正文**（既有按内容比较的断言不受影响）")
    check(mark.get("kind") == KIND_MODEL_TIMEOUT
          and msg.additional_kwargs.get(FAILURE_KEY, {}).get("kind") == KIND_MODEL_TIMEOUT,
          "戳写在 additional_kwargs[FAILURE_KEY] 里")
    check(detail_of(mark) == "原因", "detail 可读（下游当失败原因用）")
    try:
        dumped = json.dumps(msg.additional_kwargs[FAILURE_KEY], ensure_ascii=False)
        ok_json = bool(dumped)
    except (TypeError, ValueError):
        ok_json = False
    check(ok_json, "标记可 JSON 序列化（消息要过 checkpoint 与 /threads/{tid}/state）")

    # dict 形态（API 侧从 state 拿到的是 dict，不是 BaseMessage）
    d = {"type": "ai", "content": "x"}
    mark_failed(d, KIND_QUOTA_EXHAUSTED, "额度")
    check((failed_mark(d) or {}).get("kind") == KIND_QUOTA_EXHAUSTED
          and detail_of(failed_mark(d)) == "额度",
          "dict 形态的消息也能打戳/读戳（两条读路径都通）")

    # 负对照：没有戳 = 今天的行为
    check(not is_failed(AIMessage(content="正常回答")), "负对照：普通 AI 消息没有戳")
    check(last_failed_mark([AIMessage(content="正常回答")]) is None,
          "负对照：无戳 → None（下游维持成功语义）")

    # 只看**末条 AI**：不往回翻旧账
    old = mark_failed(AIMessage("上一轮超时"), KIND_MODEL_TIMEOUT, "旧")
    tail_msgs = [old, AIMessage("这一轮真答完了"), ToolMessage("t", tool_call_id="c1")]
    check(last_failed_mark(tail_msgs) is None,
          "末条 AI 是正常回答 → None（尾部的 tool 消息被跳过，不翻旧账）")
    check((last_failed_mark([AIMessage("正常回答"), old]) or {}).get("kind") == KIND_MODEL_TIMEOUT,
          "末条 AI 有戳 → 命中")

    # 值域：新终态串必须落进 watcher/卡片的既有状态集，否则卡片永远停在「执行中」
    statuses = {run_status_for(k) for k in
                (KIND_MODEL_TIMEOUT, KIND_QUOTA_EXHAUSTED, KIND_MODEL_REQUIRED)}
    check(statuses <= {"error", "timeout"},
          "run_status_for 只产出 error/timeout（写错新状态串会让卡片永不结束）", str(statuses))
    check(statuses <= set(_RUN_DONE_STATUSES),
          "产出的终态都在 _RUN_DONE_STATUSES 值域里", str(_RUN_DONE_STATUSES))
    check(run_status_for("") == "error" and run_status_for("不存在的kind") == "error",
          "未知/空 kind 兜底 error（绝不产出空串）")

    long_mark = failed_mark(mark_failed(AIMessage("x"), KIND_MODEL_TIMEOUT, "d" * 2000)) or {}
    check(len(detail_of(long_mark)) == 500,
          "detail 截断到 500（消息要进 checkpoint/前端轮询/Langfuse，不能无界）")


# ── ② 三个生产点 ───────────────────────────────────────────────────
def verify_producers() -> None:
    section("② 三个生产点：超时 / 额度耗尽 / 未配模型（文案都不许改）")
    from agent.middlewares.model_required import NO_MODEL_MESSAGE, ModelRequiredMiddleware
    from agent.middlewares.model_timeout import MODEL_TIMEOUT_MESSAGE, ModelTimeoutMiddleware
    from agent.middlewares.quota_error import QUOTA_EXHAUSTED_MESSAGE, QuotaErrorMiddleware
    from agent.utils.failure_signal import (
        KIND_MODEL_REQUIRED,
        KIND_MODEL_TIMEOUT,
        KIND_QUOTA_EXHAUSTED,
        failed_mark,
    )

    req = _model_request()

    # ── 超时（异步＝生产路径）──
    calls = {"n": 0}

    async def timeout_handler(_req):
        calls["n"] += 1
        raise TimeoutError("Request timed out.")

    r = asyncio.run(ModelTimeoutMiddleware().awrap_model_call(req, timeout_handler))
    msg = r.result[0]
    check(msg.content == MODEL_TIMEOUT_MESSAGE, "超时文案一字未改（P2-3 语义没被动过）")
    check(calls["n"] == 1, "**超时仍然不重试**（P3-4 策略不变：SDK 已 4×60s）", f"calls={calls['n']}")
    check((failed_mark(msg) or {}).get("kind") == KIND_MODEL_TIMEOUT,
          "超时消息带 model_timeout 戳")

    # ── 超时（同步路径）──
    def sync_timeout_handler(_req):
        raise TimeoutError("Request timed out.")

    r_sync = ModelTimeoutMiddleware().wrap_model_call(req, sync_timeout_handler)
    check(r_sync.result[0].content == MODEL_TIMEOUT_MESSAGE
          and (failed_mark(r_sync.result[0]) or {}).get("kind") == KIND_MODEL_TIMEOUT,
          "同步路径同样：文案不变 + 带戳")

    # ── 额度耗尽 ──
    def quota_handler(_req):
        raise Exception("Error code: 402 - Free quota exhausted.")

    rq = QuotaErrorMiddleware().wrap_model_call(req, quota_handler)
    check(rq.result[0].content == QUOTA_EXHAUSTED_MESSAGE,
          "额度耗尽文案一字未改")
    check((failed_mark(rq.result[0]) or {}).get("kind") == KIND_QUOTA_EXHAUSTED,
          "额度耗尽消息带 quota_exhausted 戳")

    # ── 未配模型（拦在 handler 之前，这里直接驱动它的构造点）──
    mw_req = ModelRequiredMiddleware()
    mw_req.is_blocked = lambda: True  # 判据本身已由 verify_user_model_config 覆盖
    rn = mw_req.wrap_model_call(req, lambda _r: (_ for _ in ()).throw(AssertionError("不该调 handler")))
    check(rn.result[0].content == NO_MODEL_MESSAGE, "未配模型文案一字未改")
    check((failed_mark(rn.result[0]) or {}).get("kind") == KIND_MODEL_REQUIRED,
          "未配模型消息带 model_required 戳")


# ── ③ CaliberGate 放行（事故回归闸）────────────────────────────────
def _caliber_state(last_ai) -> dict:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    return {
        "messages": [
            HumanMessage(content="查一下丛培强本月工时"),
            AIMessage(content="", tool_calls=[
                {"name": "wrenai_witops_get_instructions", "args": {}, "id": "c1"},
            ]),
            ToolMessage("口径原文…", tool_call_id="c1"),
            last_ai,
        ]
    }


def verify_caliber_gate() -> None:
    section("③ CaliberGate：失败终局不打回（消灭 234s 空窗的那条）")
    from langchain_core.messages import AIMessage

    from agent.middlewares.caliber_gate import CaliberGateMiddleware
    from agent.middlewares.model_timeout import MODEL_TIMEOUT_MESSAGE
    from agent.utils.failure_signal import KIND_MODEL_TIMEOUT, mark_failed

    gate = CaliberGateMiddleware()
    marked = mark_failed(AIMessage(content=MODEL_TIMEOUT_MESSAGE), KIND_MODEL_TIMEOUT,
                         MODEL_TIMEOUT_MESSAGE)
    out = gate._after(_caliber_state(marked), None)
    check(out is None,
          "末条是带戳的失败终局 → **不放行就出事**：这里必须 return None（回合结束）",
          repr(out))

    # 负对照：**同样的消息去掉戳** → 必须打回（证明上面不是"函数恒返回 None"）
    plain = AIMessage(content=MODEL_TIMEOUT_MESSAGE)
    out2 = gate._after(_caliber_state(plain), None)
    check(isinstance(out2, dict) and out2.get("jump_to") == "model",
          "负对照：同样的文案但**没有戳** → 仍然打回（这正是事故里白烧第二个 240s 的那条路）",
          str(out2)[:80])

    # 负对照：普通终稿（有口径块）不受影响
    from langchain_core.messages import HumanMessage

    good = AIMessage(content="结论如下。\n\n## 业务口径\n- 在职人数 | 知识库 | 在职\n")
    st = {"messages": [HumanMessage(content="问"), good]}
    check(gate._after(st, None) is None, "负对照：正常终稿（无取料）仍不被干预")


# ── ④ 终态降级 ─────────────────────────────────────────────────────
def _build_check_result(run: dict, messages: list, todos=None) -> dict:
    from agent.subagents import check_progress as cp

    cp.apply_patch()
    from deepagents.middleware import async_subagents as A

    return A._build_check_result(
        run, "sub-1", {"messages": messages, "todos": list(todos or [])}
    )


def verify_terminal_downgrade() -> None:
    section("④ check_async_task：run=success 但末条带戳 → 改判")
    from agent.subagents import check_progress as cp
    from agent.middlewares.model_timeout import MODEL_TIMEOUT_MESSAGE
    from agent.utils.failure_signal import KIND_MODEL_TIMEOUT, mark_failed

    marked = mark_failed({"type": "ai", "content": MODEL_TIMEOUT_MESSAGE},
                         KIND_MODEL_TIMEOUT, MODEL_TIMEOUT_MESSAGE)
    res = _build_check_result({"status": "success"}, [marked])
    check(res.get("status") == "timeout",
          "run=success + 带戳 → status 改判 timeout（主 agent 不再读到自相矛盾的 success）",
          f"status={res.get('status')}")
    check(res.get("error_kind") == KIND_MODEL_TIMEOUT, "附 error_kind 机读字段")
    check(MODEL_TIMEOUT_MESSAGE[:12] in str(res.get("error") or ""),
          "附可读的 error（失败原因不再丢失）", str(res.get("error"))[:40])
    check("result" not in res,
          "不带 result（超时文案不是「结果」，不能再被报告装配当答案）")

    # 负对照 A：无戳 → 今天的行为逐字不变
    res_ok = _build_check_result({"status": "success"},
                                [{"type": "ai", "content": "本月工时合计 186 小时。"}])
    check(res_ok.get("status") == "success" and "result" in res_ok,
          "负对照：无戳 → 仍 success 且带 result（今天语义不变）",
          f"status={res_ok.get('status')}")

    # 负对照 B：running 时绝不降级（戳可能是上一轮遗留的）
    res_run = _build_check_result({"status": "running"}, [marked])
    check(res_run.get("status") == "running",
          "负对照：run 还在 running → 绝不降级（否则把在跑的任务判死）",
          f"status={res_run.get('status')}")

    # 额度耗尽的 kind 落到 error（借道既有 error 分支）
    from agent.utils.failure_signal import KIND_QUOTA_EXHAUSTED

    q = mark_failed({"type": "ai", "content": "模型额度已用完…"},
                    KIND_QUOTA_EXHAUSTED, "模型额度已用完…")
    res_q = _build_check_result({"status": "success"}, [q])
    check(res_q.get("status") == "error" and "额度" in str(res_q.get("error") or ""),
          "quota_exhausted → 改判 error 且带原因", f"status={res_q.get('status')}")

    # 命令层：watcher 写的 error/error_kind/failure_reported 不许被一次轮询抹掉
    # （async_tasks 是整条替换的 reducer，本函数重建 updated_task → 不显式带上就丢）
    cp.apply_patch()
    from deepagents.middleware import async_subagents as A

    task = {
        "task_id": "sub-1", "agent_name": "nl2sql", "thread_id": "sub-1",
        "run_id": "run-1", "status": "running", "created_at": "2026-01-01T00:00:00Z",
        "last_updated_at": "2026-01-01T00:00:00Z", "description": "查一下工时",
        "error": MODEL_TIMEOUT_MESSAGE, "error_kind": KIND_MODEL_TIMEOUT,
        "failure_reported": True,
    }
    cmd = A._build_check_command({"status": "timeout", "error": MODEL_TIMEOUT_MESSAGE},
                                 task, "tc-1")
    upd = ((getattr(cmd, "update", None) or {}).get("async_tasks") or {}).get("sub-1") or {}
    check(upd.get("error") == MODEL_TIMEOUT_MESSAGE and upd.get("error_kind") == KIND_MODEL_TIMEOUT,
          "轮询不改写/不抹掉 watcher 写的失败原因", str(upd.get("error"))[:40])
    check(upd.get("failure_reported") is True,
          "轮询保留 failure_reported（抹掉会让同一条失败被再汇报一次）")
    check(upd.get("description") == "查一下工时", "description 仍保留（M-T5c 既有行为）")
    check(upd.get("status") == "timeout", "status 用本次 check 的结论")

    # 负对照：payload 里没有这些键 → 不凭空冒出
    bare = {k: v for k, v in task.items()
            if k not in ("error", "error_kind", "failure_reported")}
    cmd2 = A._build_check_command({"status": "running"}, bare, "tc-2")
    upd2 = ((getattr(cmd2, "update", None) or {}).get("async_tasks") or {}).get("sub-1") or {}
    check("error" not in upd2 and "failure_reported" not in upd2,
          "负对照：payload 没有这些键 → 不凭空写入", str(sorted(upd2))[:80])


# ── ⑤ 权威进度源 ───────────────────────────────────────────────────
def verify_progress_source() -> None:
    section("⑤ check_async_task 进度：state.todos 优先（卡片与模型口径一致）")
    from agent.subagents.check_progress import _todos_from_state, _extract_progress

    todos = [
        {"content": "理解建模-清晰度与知识（四路取料）", "status": "completed"},
        {"content": "Schema 提取与裁剪", "status": "in_progress"},
        {"content": "SQL 生成", "status": "pending"},
        {"content": "SQL 执行", "status": "pending"},
        {"content": "结果核验", "status": "pending"},
        {"content": "结果呈现", "status": "pending"},
    ]
    # messages 里塞一条**过时**的 write_todos 回声（旧反扫会给出 0/6 + 第 1 步）
    stale = {"type": "tool", "name": "write_todos", "content": str(
        [{"content": t["content"], "status": "pending"} for t in todos]
    )}
    result = {"thread_id": "sub-1"}
    _extract_progress(result, [stale], {"todos": todos, "messages": [stale]})
    check(result.get("progress") == "1/6 (17%)",
          "进度取 state.todos（卡片口径）而不是过时的 messages 回声",
          str(result.get("progress")))
    check(result.get("current_step") == "Schema 提取与裁剪",
          "current_step 与卡片一致（第 2 步）", str(result.get("current_step")))
    check(len(result.get("steps") or []) == 6 and (result.get("steps") or [""])[0].startswith("✅"),
          "steps 渲染完整", str(result.get("steps"))[:60])

    # 负对照：拿不到 state.todos → 退回 messages 反扫（今天的行为）
    result2 = {"thread_id": "sub-1"}
    _extract_progress(result2, [stale], {"messages": [stale]})
    check(result2.get("progress") == "0/6 (0%)",
          "负对照：无 state.todos → 退回 messages 反扫，行为与今天一致",
          str(result2.get("progress")))
    result3 = {"thread_id": "sub-1"}
    _extract_progress(result3, [])
    check("0/?" in str(result3.get("progress")), "负对照：两边都没有 → 初始化态")

    # 非法项不许抛
    check(_todos_from_state({"todos": [None, {"content": "  "}, "x"]}) == [],
          "非法/空白 todos 项被跳过，不抛异常")
    check(_todos_from_state(None) == [] and _todos_from_state({}) == [],
          "拿不到 thread_values / 无 todos 键 → 空列表")


# ── ⑥ watcher 改判 + 分流（旗舰段）─────────────────────────────────
class _FakeClient:
    """假 langgraph_sdk client：主/子线程 state + run 列表都可控。"""

    def __init__(self, main_values: dict, sub_values: dict, run_map: dict):
        self.main_values = main_values
        self.sub_values = sub_values
        self.run_map = run_map
        self.created_runs: list[dict] = []
        self.cancelled: list[dict] = []
        outer = self

        class _Threads:
            async def get_state(self, thread_id: str = "", **kw):
                vals = outer.main_values if thread_id == "main-1" else outer.sub_values
                return {"values": dict(vals), "tasks": []}

            async def update_state(self, thread_id: str = "", values=None, as_node=None):
                outer.apply(values)
                return {"checkpoint": {}}

        class _Runs:
            async def list(self, thread_id: str = "", limit: int = 1, **kw):
                return list(outer.run_map.get(thread_id, []))[:limit]

            async def get(self, thread_id: str = "", run_id: str = "", **kw):
                for r in outer.run_map.get(thread_id, []):
                    if r.get("run_id") == run_id:
                        return r
                return {}

            async def cancel(self, thread_id: str = "", run_id: str = "", **kw):
                outer.cancelled.append({"thread_id": thread_id, "run_id": run_id})
                return {}

            async def create(self, thread_id: str = "", **kw):
                outer.created_runs.append({"thread_id": thread_id, **kw})
                return {"run_id": f"run-created-{len(outer.created_runs)}"}

        self.threads = _Threads()
        self.runs = _Runs()

    def apply(self, values: dict | None) -> None:
        for k, v in (values or {}).items():
            cur = self.main_values.get(k)
            if isinstance(v, dict) and isinstance(cur, dict):
                self.main_values[k] = {**cur, **v}
            else:
                self.main_values[k] = v


_STORE_SEQ = 0


def _run_watcher(sub_messages: list, run_status: str = "success",
                 sub_created_at: str = "", wait_secs: float = 8.0) -> _FakeClient:
    """跑真 `_async_sync_loop`：子 run 按参数给定终态，主线程写入总是成功。"""
    from agent.subagents import pending_terminal as pt
    from agent.subagents import sync_subagent_todos as sst
    import langgraph_sdk

    task = {
        "task_id": "sub-1",
        "agent_name": "nl2sql",
        "run_id": "run-1",
        "created_at": "2026-01-01T00:00:00Z",
        "description": "查一下工时",
    }
    sub_run = {"run_id": "run-1", "status": run_status}
    if sub_created_at:
        sub_run["created_at"] = sub_created_at
    fake = _FakeClient(
        main_values={
            "async_tasks": {}, "active_queries": {}, "subagent_steps_map": {},
            "todos": [], "messages": [{"role": "user", "content": "查一下工时"}],
        },
        sub_values={
            "todos": [{"content": "取数", "status": "completed"}],
            "messages": list(sub_messages),
        },
        run_map={"sub-1": [sub_run], "main-1": [{"run_id": "run-main", "status": "success"}]},
    )
    # 真 store（独立库）：写入全成功时不该有任何登记行
    global _STORE_SEQ
    _STORE_SEQ += 1
    pt._STORE = pt.PendingTerminalStore(_WORK / f"pt-{_STORE_SEQ}.sqlite")

    orig_get_client = langgraph_sdk.get_client
    orig_update = sst._sync_update_state
    orig_sleep = asyncio.sleep

    def _fake_update_state(thread_id, values):
        fake.apply(values)

    async def _fast_sleep(secs, *a, **k):
        await orig_sleep(0.001 if secs else 0)

    langgraph_sdk.get_client = lambda **kw: fake
    sst._sync_update_state = _fake_update_state
    asyncio.sleep = _fast_sleep
    try:
        asyncio.run(asyncio.wait_for(
            sst._async_sync_loop("main-1", "sub-1", "nl2sql", task), timeout=wait_secs
        ))
    except (asyncio.TimeoutError, TimeoutError):
        pass
    finally:
        langgraph_sdk.get_client = orig_get_client
        sst._sync_update_state = orig_update
        asyncio.sleep = orig_sleep
    return fake


def _created_texts(fake: _FakeClient) -> str:
    return json.dumps(fake.created_runs, ensure_ascii=False)


def verify_watcher_downgrade() -> None:
    section("⑥ watcher：子 run success + 末条带戳 → 改判 timeout + 失败汇报")
    from agent.middlewares.model_timeout import MODEL_TIMEOUT_MESSAGE
    from agent.utils.failure_signal import KIND_MODEL_TIMEOUT, mark_failed

    marked = mark_failed(
        {"type": "ai", "content": MODEL_TIMEOUT_MESSAGE}, KIND_MODEL_TIMEOUT, MODEL_TIMEOUT_MESSAGE
    )
    fake = _run_watcher([marked])
    entry = (fake.main_values.get("async_tasks") or {}).get("sub-1") or {}
    check(entry.get("status") == "timeout",
          "async_tasks 落 timeout（不再与 runs.get 的 success 各记一套账）",
          f"status={entry.get('status')}")
    check(MODEL_TIMEOUT_MESSAGE[:12] in str(entry.get("error") or ""),
          "失败原因来自戳的 detail（侧边栏不再只有「执行失败」四个字）",
          str(entry.get("error"))[:40])
    check((fake.main_values.get("active_queries") or {}).get("sub-1") is False,
          "active_queries 翻 false（卡片收尾）")
    texts = _created_texts(fake)
    check("[系统自动通知]" in texts and "执行超时" in texts,
          "发了失败汇报 run（[系统自动通知] + 超时标签）", texts[:120])
    check("已完成查询任务" not in texts,
          "**没有**发「已完成」续跑（否则主 agent 会去催一个已经死掉的子任务）")

    # 负对照：无戳 → 今天的行为逐字不变（verify_pending_terminal 既有断言的前提）
    fake2 = _run_watcher([{"type": "ai", "content": "本月工时合计 186 小时。"}])
    entry2 = (fake2.main_values.get("async_tasks") or {}).get("sub-1") or {}
    check(entry2.get("status") == "success",
          "负对照：无戳 → 仍落 success", f"status={entry2.get('status')}")
    texts2 = _created_texts(fake2)
    check("已完成查询任务" in texts2 and "[系统自动通知]" not in texts2,
          "负对照：无戳 → 仍触发成功续跑、不发失败汇报", texts2[:120])

    # fail-open：读子线程 state 炸了 → 按成功处理（绝不把成功判成失败）
    import asyncio as _aio

    from agent.subagents.sync_subagent_todos import _last_failed_mark_of

    class _Boom:
        class threads:  # noqa: N801
            @staticmethod
            async def get_state(thread_id: str = "", **kw):
                raise RuntimeError("boom")

    got = _aio.run(_last_failed_mark_of(_Boom(), "sub-1"))
    check(got is None, "读 state 抛异常 → fail-open 返回 None（按成功收尾）")


# ── ⑦ 卡死预算时钟 ─────────────────────────────────────────────────
def verify_stale_clock() -> None:
    section("⑦ 卡死预算：按 run 自己的 created_at 起算")
    import time as _t
    from datetime import UTC, datetime, timedelta

    from agent.subagents.sync_subagent_todos import _monotonic_from_iso

    now = datetime.now(UTC)
    iso_700 = (now - timedelta(seconds=700)).isoformat().replace("+00:00", "Z")
    iso_10 = (now - timedelta(seconds=10)).isoformat()
    age_700 = _t.monotonic() - _monotonic_from_iso(iso_700)
    age_10 = _t.monotonic() - _monotonic_from_iso(iso_10)
    check(690 < age_700 < 720, "700s 前的 created_at → 换算出的时长 ≈700s（会被判卡死）",
          f"{age_700:.1f}s")
    check(0 <= age_10 < 60, "10s 前的 created_at → 换算出的时长 < 60s（不判卡死）",
          f"{age_10:.1f}s")
    check(_monotonic_from_iso("") is None and _monotonic_from_iso("不是时间") is None
          and _monotonic_from_iso(None) is None,
          "空/非法时间串 → None（调用方退回 loop_start = 旧行为）")

    # 跑真 loop：created_at 700s 前 → 首轮即判卡死
    fake = _run_watcher([{"type": "ai", "content": "（还在跑）"}],
                        run_status="running", sub_created_at=iso_700, wait_secs=5.0)
    entry = (fake.main_values.get("async_tasks") or {}).get("sub-1") or {}
    check(entry.get("status") == "timeout",
          "run 已跑超预算（created_at 700s 前）→ 判卡死 timeout",
          f"status={entry.get('status')}")
    check(bool(fake.cancelled), "顺手真取消了卡死的子 run", str(fake.cancelled[:1]))

    # 负对照：created_at 10s 前 → 不许判死
    fake2 = _run_watcher([{"type": "ai", "content": "（还在跑）"}],
                         run_status="running", sub_created_at=iso_10, wait_secs=3.0)
    entry2 = (fake2.main_values.get("async_tasks") or {}).get("sub-1") or {}
    check(entry2.get("status") != "timeout" and not fake2.cancelled,
          "负对照：created_at 10s 前 → 不判卡死（不误杀合法长查询）",
          f"status={entry2.get('status')}")


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)
    print("终局失败信号（failure_signal）验证")
    verify_signal_tools()
    verify_producers()
    verify_caliber_gate()
    verify_terminal_downgrade()
    verify_progress_source()
    verify_watcher_downgrade()
    verify_stale_clock()

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 60}")
    for ok, label in results:
        if not ok:
            print(f"  [FAIL] {label}")
    print(f"{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
