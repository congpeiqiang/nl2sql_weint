# -*- coding: utf-8 -*-
"""P2-4 子任务终态待补写 + 补写器验证（离线，无需后端/数据库/网络）。

**要解决的**（清单原文）：修 `sync_subagent_todos` 的 300s 天花板导致的
`active_queries` 永久 true。

**真正的机制**（本脚本照着它打）：LangGraph 的 `threads.update_state` 在主线程还有
pending/running 的 run 时是硬 409（`langgraph_api/grpc/ops/threads.py`：`run_count > 0`
→ "Thread is busy with a running job."）。主线程忙是常态（另一子任务的自动续跑 run、
用户消息、**并发压满 10 个 run 槽时排在 pending 的 run**）。watcher 等满
`COMPLETE_WRITE_MAX_SECONDS=300` 就 `break` 走人 → `active_queries` 永久 true（卡片永久
「执行中」）+ `async_tasks` 终态缺失（**前端自动续跑不触发 → 用户的图表/报告丢失**）。

本脚本验十段（尽量跑被测对象真身，不 mock 掉要验的那一层）：
  ① **登记表**：写/读/幂等去重/清行/放弃/到期清理/换库幂等建表 + 锁已换成计量的。
  ② **「主线程忙」判据**：真 `ConflictError(409)`、真 409 文案、历史文案 "in-flight"
     都判为可重试；**负对照**＝普通异常与 400 必须**不**判成 busy（否则会无脑重试）。
  ③ **watcher 放手 → 登记**（旗舰段，跑真 `_async_sync_loop`）：主线程恒 409 →
     300s 上限一到必须落一行待补写（含 main/sub/status/watched_run_id/最终步骤）；
     **负对照**＝写入成功时**一行都不能有**（证明断言不是恒真）。
  ④ **补写 happy path**：真补一行 → 断言写下去的载荷（async_tasks 终态 / active_queries
     =false / subagent_steps_map / error）与实际写入次数，行被删，success 触发续跑。
  ⑤ **三层安全判定**（都不许写）：被重派发（run_id 变了）/ 子线程最新 run 非终态 /
     主线程已是终态 → 只丢行不写；每层都有「写入次数=0」的断言。
  ⑥ **失败汇报分流**：error 终态补写成功后触发失败汇报、且**不**触发续跑。
  ⑦ **仍忙**：补写时 409 → 保留行 + attempts+1 + 不触发下游；**负对照**＝非 busy 的
     异常归 failed 档（与 busy 分开计）。
  ⑧ **超龄放弃**：登记时间早于上限 → 只置 abandoned、不写不删（留痕）。
  ⑨ **补写器生命周期**：`start_reaper` 幂等、启动即跑一轮（残留行落地）、`stop_reaper`
     能停干净；**接线**＝真跑 `custom_app._lifespan`，断言补写器被起过也被停过
     （P2-3 那个 `UnboundLocalError` 就是这么炸的，只验文本会一路绿灯）。
  ⑩ **CLI**：`--status` / `--replay --dry-run` 真退出码 0 且输出 JSON。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_pending_terminal.py

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
import time

# ⚠️ 必须在 import 业务模块之前：登记表落点由 AGENT_DATA_ROOT 推导
_WORK = pathlib.Path(tempfile.mkdtemp(prefix="nl2sql-verify-p24-"))
os.environ["AGENT_DATA_ROOT"] = str(_WORK)
os.environ.pop("LANGGRAPH_API_URL", None)
os.environ.pop("NL2SQL_AUTH_DISABLED", None)
os.environ.pop("NL2SQL_FORCE_PASSWORD_CHANGE", None)
os.environ.pop("LANGFUSE_ENABLE", None)
# ⑨ 要真跑 `custom_app._lifespan`（它 import api.drain → langgraph_api.*）：与
# start_server.py 同款的环境前置，缺了会在 import 期报 Config 'REDIS_URI' is missing
os.environ.setdefault("ALLOW_PRIVATE_NETWORK", "true")
os.environ.setdefault("N_JOBS_PER_WORKER", "10")
os.environ.setdefault("DATABASE_URI", ":memory:")
os.environ.setdefault("REDIS_URI", "fake")
os.environ.setdefault("MIGRATIONS_PATH", "__inmem")
os.environ.setdefault("LANGGRAPH_RUNTIME_EDITION", "inmem")
os.environ.setdefault("LANGGRAPH_ALLOW_BLOCKING", "true")
os.environ.setdefault("LANGSERVE_GRAPHS", "{}")

_HERE = pathlib.Path(__file__).resolve()
_REPO = _HERE.parents[1]
_SRC = _REPO / "src"
for cand in (_SRC, pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

import httpx  # noqa: E402
from langgraph_sdk.errors import ConflictError  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")


def _conflict(msg: str = "Thread is busy with a running job. Cannot update state.") -> ConflictError:
    resp = httpx.Response(409, request=httpx.Request("POST", "http://x/threads/t/state"))
    return ConflictError(msg, response=resp, body=None)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:  # noqa: BLE001
            pass

    def take(self) -> list[str]:
        lines, self.lines = self.lines, []
        return lines


# ── 假 SDK client（只实现被测代码真正用到的那几个面）──────────────────
class _FakeClient:
    """假 langgraph_sdk client：主/子线程 state + run 列表都可控。

    ⚠️ 写入必须**落到 fake.main_values**（按键合并，模拟 reducer）：否则回退守护会读到
    "async_tasks 没写、active_queries 还是 true" 而判定终态被回退 → 一路重写，
    把「写入成功」的负对照污染成「也登记了待补写行」。
    """

    def __init__(self, main_values: dict | None = None, sub_values: dict | None = None):
        self.main_values = main_values if main_values is not None else {}
        self.sub_values = sub_values if sub_values is not None else {}
        self.run_map: dict[str, list[dict]] = {}
        self.state_reads = 0
        self.created_runs: list[dict] = []
        self.update_raises: BaseException | None = None
        self.writes: list[dict] = []
        outer = self

        class _Threads:
            async def get_state(self, thread_id: str = "", **kw):
                outer.state_reads += 1
                vals = outer.main_values if thread_id == "main-1" else outer.sub_values
                return {"values": dict(vals), "tasks": []}

            async def get(self, thread_id: str = "", **kw):
                return {"values": dict(outer.sub_values)}

            async def update_state(self, thread_id: str = "", values=None, as_node=None):
                if outer.update_raises is not None:
                    raise outer.update_raises
                outer.writes.append({"thread_id": thread_id, "values": values, "as_node": as_node})
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
                return {}

            async def create(self, thread_id: str = "", **kw):
                outer.created_runs.append({"thread_id": thread_id, **kw})
                return {"run_id": f"run-created-{len(outer.created_runs)}"}

        self.threads = _Threads()
        self.runs = _Runs()

    def apply(self, values: dict | None) -> None:
        """按键合并进主线程 state（模拟 keyed reducer 的效果）。"""
        for k, v in (values or {}).items():
            cur = self.main_values.get(k)
            if isinstance(v, dict) and isinstance(cur, dict):
                self.main_values[k] = {**cur, **v}
            else:
                self.main_values[k] = v


def _row_values(writes: list[dict]) -> dict:
    """把若干次 update_state 的 values 合并起来看（补写器只写一次）。"""
    merged: dict = {}
    for w in writes:
        merged.update(w.get("values") or {})
    return merged


# ── ① 登记表 ───────────────────────────────────────────────────────
def verify_store() -> None:
    section("① 登记表（pending_terminal）")
    from agent.subagents import pending_terminal as pt
    from agent.utils.prom_metrics import MeteredLock

    st = pt.PendingTerminalStore(_WORK / "t1.sqlite")
    check(isinstance(pt._LOCK, MeteredLock), "登记表锁已换成计量锁（P2-3 等锁埋点）")
    check(
        str(st.path).endswith("t1.sqlite"),
        "库路径可注入（默认走 <AGENT_DATA_ROOT>/pending_terminal/pending_terminal.sqlite）",
        str(st.path),
    )
    check(pt.PendingTerminalStore(_WORK / "t1.sqlite") is not None, "同库重复构造幂等建表不报错")

    rid = st.record("main-1", "sub-1", "nl2sql", "success", "run-1", steps=[{"id": "s"}],
                    task={"task_id": "sub-1", "created_at": "2026-01-01T00:00:00Z"}, error="")
    check(rid is not None, "登记一条待补写终态", f"row={rid}")
    rid2 = st.record("main-1", "sub-1", "nl2sql", "error", "run-1")
    rows = st.rows()
    check(rid2 == rid and len(rows) == 1, "同 (子线程, run) 幂等去重，只更新载荷",
          f"rows={len(rows)}")
    check(rows[0]["run_status"] == "error" and rows[0]["steps_json"] == "",
          "去重时载荷被替换（run_status/steps_json 都换新）")

    # 不同 run → 独立行（重派发后是新任务）
    st.record("main-1", "sub-1", "nl2sql", "success", "run-2")
    check(st.count() == 2, "换 run_id（重派发）= 新行，不与旧行合并", f"count={st.count()}")

    n = st.bump_attempt(rid, "409 busy: x")
    check(n == 1 and st.rows()[0]["last_error"].startswith("409 busy"),
          "attempts 累加且留 last_error")
    check(st.count("abandoned") == 0, "bump 不改状态（仍 pending）")
    st.mark_abandoned(rid, "超龄")
    check(st.count() == 1 and st.count("abandoned") == 1, "置 abandoned 后不再计入 pending")
    st.clear(rid + 1)
    check(st.count("abandoned") == 1, "clear 只删指定行")
    st.clear(rid)
    check(st.count("abandoned") == 0, "clear 能删 abandoned 行（运维清理用）")

    # 到期清理
    st.record("main-1", "sub-9", "nl2sql", "success", "run-9")
    st.record("main-1", "sub-8", "nl2sql", "success", "run-8")
    with pt._LOCK:
        st._conn.execute(
            "UPDATE pending_terminal SET created_at='2020-01-01T00:00:00+00:00' WHERE sub_thread_id='sub-8'"
        )
        st._conn.commit()
    purged = st.purge_old(keep_seconds=86400)
    check(purged == 1 and st.count() == 1, "purge_old 按建行时间清理（只删过期的）",
          f"purged={purged}")

    check(pt.status_summary().get("pending") is not None, "status_summary 可读")


# ── ② 「主线程忙」判据 ──────────────────────────────────────────────
def verify_busy_classifier() -> None:
    section("② 「主线程忙」判据（可自愈 vs 不），这是分流重试的关键")
    from agent.subagents.pending_terminal import _is_thread_busy

    check(_is_thread_busy(_conflict()), "真 ConflictError(409) → busy（要保留重试）")
    check(
        _is_thread_busy(RuntimeError("Thread is busy with a running job. Cannot update state.")),
        "上游 409 原文（仅文案）→ busy",
    )
    check(
        _is_thread_busy(RuntimeError("Thread 019ff has in-flight runs")),
        "历史文案 in-flight → busy（版本差异兜底）",
    )
    # 负对照：不许把普通错误当 busy（否则永不放弃、白白重试）
    check(not _is_thread_busy(ValueError("boom")), "负对照：普通异常 → 不 busy")
    check(
        not _is_thread_busy(ConnectionError("connection refused")),
        "负对照：连接错误 → 不 busy（归 failed 档，也要保留行）",
    )


# ── ③ watcher 放手 → 登记（旗舰段）────────────────────────────────
async def _run_real_watcher(
    write_raises: BaseException | None, store_name: str = "t3.sqlite"
) -> tuple[dict, _FakeClient]:
    """跑真 `_async_sync_loop`：子 run 已 success，主线程写入按参数成功/恒 409。"""
    from agent.subagents import pending_terminal as pt
    from agent.subagents import sync_subagent_todos as sst
    from langgraph_sdk import get_client as _real_get_client

    task = {
        "task_id": "sub-1",
        "agent_name": "nl2sql",
        "run_id": "run-1",
        "created_at": "2026-01-01T00:00:00Z",
        "description": "查一下销售额",
    }
    main_values = {
        "async_tasks": {},
        "active_queries": {},
        "subagent_steps_map": {},
        "todos": [],
        "messages": [{"role": "user", "content": "查一下销售额"}],
    }
    sub_values = {
        "todos": [{"content": "连库", "status": "completed"}, {"content": "取数", "status": "completed"}],
        "messages": [],
    }
    fake = _FakeClient(main_values, sub_values)
    fake.run_map = {
        "sub-1": [{"run_id": "run-1", "status": "success", "created_at": "2026-01-01T00:00:01Z"}],
        "main-1": [{"run_id": "run-main", "status": "success"}],
    }

    # 真 store（独立库）→ 挂成单例，供 _handoff_terminal_write 落行
    pt._STORE = pt.PendingTerminalStore(_WORK / store_name)

    orig_get_client = _real_get_client
    orig_update = sst._sync_update_state
    orig_sleep = asyncio.sleep
    orig_ceiling = sst.COMPLETE_WRITE_MAX_SECONDS

    def _fake_update_state(thread_id, values):
        if write_raises is not None:
            raise write_raises
        fake.writes.append({"thread_id": thread_id, "values": values})
        fake.apply(values)

    async def _fast_sleep(secs, *a, **k):
        await orig_sleep(0.001 if secs else 0)

    import langgraph_sdk

    langgraph_sdk.get_client = lambda **kw: fake
    sst._sync_update_state = _fake_update_state
    sst.COMPLETE_WRITE_MAX_SECONDS = 0.3   # 天花板压到毫秒级
    asyncio.sleep = _fast_sleep
    try:
        await asyncio.wait_for(
            sst._async_sync_loop("main-1", "sub-1", "nl2sql", task), timeout=30
        )
    except asyncio.TimeoutError:
        pass
    finally:
        langgraph_sdk.get_client = orig_get_client
        sst._sync_update_state = orig_update
        sst.COMPLETE_WRITE_MAX_SECONDS = orig_ceiling
        asyncio.sleep = orig_sleep

    rows = pt.get_store().rows()
    return ({"rows": rows, "writes": fake.writes}, fake)


def verify_watcher_handoff() -> None:
    section("③ watcher 放手 → 登记待补写（跑真 _async_sync_loop）")
    from agent.subagents import pending_terminal as pt

    # 主线程恒 409：watcher 必须放手并把终态登记出去，而不是静默 break
    out, _fake = asyncio.run(_run_real_watcher(_conflict()))
    rows = out["rows"]
    check(len(rows) == 1, "主线程恒 409 → 恰好登记 1 行待补写", f"rows={len(rows)}")
    check(bool(rows) and rows[0]["run_status"] == "success", "行里带正确终态",
          rows[0]["run_status"] if rows else "-")
    check(
        bool(rows) and rows[0]["main_thread_id"] == "main-1" and rows[0]["sub_thread_id"] == "sub-1",
        "行里带正确的 主/子 线程 id",
    )
    check(bool(rows) and rows[0]["watched_run_id"] == "run-1",
          "行里带 watcher 盯的 run_id（补写前的三层判定要靠它）")
    check(bool(rows) and json.loads(rows[0]["steps_json"] or "[]"),
          "行里带最终步骤快照（免得补写时卡片没步骤）")
    check(bool(rows) and "查一下销售额" in rows[0]["task_json"],
          "行里带原始 task 字典（补写时沿用它，不重建）")
    check(
        all("async_tasks" not in (w.get("values") or {}) for w in out["writes"]),
        "放手时**没有**残留的半截终态写入（async_tasks 一次都没写进去）",
    )

    # 负对照：写入成功 → 一行都不许有（证明上面的断言不是恒真）
    # ⚠️ 必须换库名：`_run_real_watcher` 自己会挂一条新 store，先在外部赋值会被它覆盖，
    # 上一轮（恒 409）留下的行就会被当成"负对照也登记了"——这是本段最初的红灯。
    out2, _ = asyncio.run(_run_real_watcher(None, store_name="t3b.sqlite"))
    merged = _row_values(out2["writes"])
    check(len(out2["rows"]) == 0, "负对照：写入成功 → 不登记任何行")
    check(
        (merged.get("async_tasks") or {}).get("sub-1", {}).get("status") == "success"
        and (merged.get("active_queries") or {}).get("sub-1") is False,
        "负对照：正常路径仍按老行为写 async_tasks + active_queries=false",
    )


# ── ④⑥⑦⑧ 补写器：真补一行 ─────────────────────────────────────────
def _seed_row(status: str = "success", watched: str = "run-1", steps=None, age_h: float = 0.0) -> int:
    """登记一行（error 留空：与 watcher 真实登记一致 —— 错误详情由补写时现算）。"""
    from agent.subagents import pending_terminal as pt
    st = pt.get_store()
    rid = st.record(
        "main-1", "sub-1", "nl2sql", status, watched,
        steps=steps, task={"task_id": "sub-1", "created_at": "2026-01-01T00:00:00Z"},
        error="",
    )
    if age_h:
        import datetime as _dt

        old = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=age_h)
        ).isoformat(timespec="seconds")
        with pt._LOCK:
            st._conn.execute(
                "UPDATE pending_terminal SET created_at=? WHERE id=?", (old, rid)
            )
            st._conn.commit()
    return rid


def _replay_with(
    fake: _FakeClient,
    *,
    update_raises: BaseException | None = None,
    monkeypatch_notify: bool = True,
) -> tuple[dict, list[str]]:
    """跑真 `replay_once()`，client/写入/下游通知都换成可控的。"""
    from agent.subagents import pending_terminal as pt
    from agent.subagents import sync_subagent_todos as sst
    import langgraph_sdk

    fake.update_raises = update_raises
    calls: list[str] = []
    orig_get_client = langgraph_sdk.get_client
    orig_update = sst._sync_update_state
    orig_cont = sst._notify_main_agent_continue
    orig_fail = sst._maybe_report_failure

    langgraph_sdk.get_client = lambda **kw: fake

    def _fake_update_state(thread_id, values):
        if update_raises is not None:
            raise update_raises
        fake.writes.append({"thread_id": thread_id, "values": values})
        fake.apply(values)

    async def _cont(client, main_thread_id, agent_name):
        calls.append("continue")

    async def _fail(client, main_thread_id, sub_thread_id, agent_name, run_status):
        calls.append(f"report:{run_status}")

    sst._sync_update_state = _fake_update_state
    if monkeypatch_notify:
        sst._notify_main_agent_continue = _cont
        sst._maybe_report_failure = _fail
    try:
        res = asyncio.run(pt.replay_once())
    finally:
        langgraph_sdk.get_client = orig_get_client
        sst._sync_update_state = orig_update
        sst._notify_main_agent_continue = orig_cont
        sst._maybe_report_failure = orig_fail
    return res, calls


def _fake_for_row(sub_runs: list[dict], main_values: dict) -> _FakeClient:
    f = _FakeClient(
        main_values if main_values.get("subagent_steps_map") is not None
        else {**main_values, "subagent_steps_map": {}},
        {"todos": [{"content": "取数", "status": "completed"}], "messages": []},
    )
    f.run_map = {"sub-1": sub_runs, "main-1": [{"run_id": "run-main", "status": "success"}]}
    return f


def verify_replay_happy() -> None:
    section("④ 补写 happy path（真写一次，载荷要对）")
    from agent.subagents import pending_terminal as pt

    pt._STORE = pt.PendingTerminalStore(_WORK / "t4.sqlite")
    _seed_row("success")
    fake = _fake_for_row(
        [{"run_id": "run-1", "status": "success"}],
        {"async_tasks": {}, "active_queries": {"sub-1": True}, "subagent_steps_map": {}},
    )
    res, calls = _replay_with(fake)
    check(res["written"] == 1 and res["dropped"] == 0, "补写成功 1 行", json.dumps(res))
    check(len(fake.writes) == 1, "只写一次 update_state（不重复写）", f"writes={len(fake.writes)}")
    w = _row_values(fake.writes)
    entry = (w.get("async_tasks") or {}).get("sub-1") or {}
    check(entry.get("status") == "success", "async_tasks[task].status = 终态")
    check((w.get("active_queries") or {}).get("sub-1") is False, "active_queries[task] 清零")
    check(bool((w.get("subagent_steps_map") or {}).get("sub-1")),
          "subagent_steps_map 写入最终步骤（步骤快照为空时按子线程重算）")
    check(entry.get("last_updated_at") and entry.get("last_checked_at"),
          "终态条目带 last_updated_at/last_checked_at（前端据此渲染）")
    check(entry.get("task_id") == "sub-1" and entry.get("agent_name") == "nl2sql",
          "终态条目保留 task_id/agent_name")
    check(pt.get_store().count() == 0, "补写成功后删行")
    check(calls == ["continue"], "success 终态补写成功 → 触发主 agent 续跑（正好一次）", str(calls))


def verify_replay_guards() -> None:
    section("⑤ 三层安全判定：这三种情况只丢行、绝不写")
    from agent.subagents import pending_terminal as pt

    # (a) 被 update_async_task 重派发：子线程最新 run 已经是另一个
    pt._STORE = pt.PendingTerminalStore(_WORK / "t5a.sqlite")
    _seed_row("success", watched="run-1")
    fake = _fake_for_row(
        [{"run_id": "run-2", "status": "running"}], {"async_tasks": {}, "active_queries": {}}
    )
    res, calls = _replay_with(fake)
    check(res["dropped"] == 1 and res["written"] == 0, "(a) 重派发 → 只丢行")
    check(len(fake.writes) == 0, "(a) **没有**任何写入（不许把活的任务打成终态）")
    check(calls == [], "(a) 不触发续跑/汇报")
    check(pt.get_store().count() == 0, "(a) 行被删（交棒给新 watcher）")

    # (b) 子线程最新 run 就是盯的那个、但仍非终态
    pt._STORE = pt.PendingTerminalStore(_WORK / "t5b.sqlite")
    _seed_row("success", watched="run-1")
    fake = _fake_for_row(
        [{"run_id": "run-1", "status": "running"}], {"async_tasks": {}, "active_queries": {}}
    )
    res, _ = _replay_with(fake)
    check(res["dropped"] == 1 and len(fake.writes) == 0, "(b) 子 run 非终态 → 只丢行不写")

    # (c) 主线程 async_tasks 已是终态（别处写过了）→ 幂等丢行
    pt._STORE = pt.PendingTerminalStore(_WORK / "t5c.sqlite")
    _seed_row("success", watched="run-1")
    fake = _fake_for_row(
        [{"run_id": "run-1", "status": "success"}],
        {"async_tasks": {"sub-1": {"status": "success"}}, "active_queries": {"sub-1": True}},
    )
    res, calls = _replay_with(fake)
    check(res["dropped"] == 1 and len(fake.writes) == 0, "(c) 已是终态 → 只丢行不写（幂等）")
    check(calls == [], "(c) 不重复触发续跑（防重复通知）")


def verify_replay_failure_path() -> None:
    section("⑥ 失败终态：补写成功后走失败汇报，不走续跑")
    from agent.subagents import pending_terminal as pt

    pt._STORE = pt.PendingTerminalStore(_WORK / "t6.sqlite")
    _seed_row("error", watched="run-1")
    fake = _fake_for_row(
        [{"run_id": "run-1", "status": "error", "error": "APITimeoutError: timed out"}],
        {"async_tasks": {}, "active_queries": {"sub-1": True}},
    )
    res, calls = _replay_with(fake)
    check(res["written"] == 1, "error 终态补写成功")
    w = _row_values(fake.writes)
    check((w.get("async_tasks") or {}).get("sub-1", {}).get("status") == "error",
          "写下去的是 error 终态")
    check("APITimeoutError" in str((w.get("async_tasks") or {}).get("sub-1", {}).get("error", "")),
          "错误详情一并补写（前端能看到原因）")
    check(calls == ["report:error"], "触发失败汇报且**不**触发续跑", str(calls))


def verify_replay_busy_and_abandon() -> None:
    section("⑦⑧ 仍忙 / 超龄放弃")
    from agent.subagents import pending_terminal as pt

    # ⑦ 仍忙：保留行 + attempts+1 + 不触发下游
    pt._STORE = pt.PendingTerminalStore(_WORK / "t7.sqlite")
    rid = _seed_row("success", watched="run-1")
    fake = _fake_for_row(
        [{"run_id": "run-1", "status": "success"}], {"async_tasks": {}, "active_queries": {}}
    )
    res, calls = _replay_with(fake, update_raises=_conflict())
    check(res["busy"] == 1 and res["written"] == 0, "409 → 归 busy 档")
    rows = pt.get_store().rows()
    check(len(rows) == 1 and rows[0]["attempts"] == 1, "行保留 + attempts=1",
          f"attempts={rows[0]['attempts'] if rows else '-'}")
    check(calls == [], "仍忙时不触发续跑/汇报")
    check(len(fake.writes) == 0, "仍忙时没有写入")

    # 负对照：非 busy 的错误归 failed 档（与 busy 分开，便于运维区分）
    res2, _ = _replay_with(fake, update_raises=ValueError("schema mismatch"))
    check(res2["failed"] == 1 and res2["busy"] == 0, "负对照：普通错误归 failed 档")
    check(pt.get_store().count() == 1, "负对照：普通错误也保留行（可重试）")

    # ⑧ 超龄 → abandoned，不写不删
    pt._STORE = pt.PendingTerminalStore(_WORK / "t8.sqlite")
    _seed_row("success", watched="run-1", age_h=8)
    fake = _fake_for_row(
        [{"run_id": "run-1", "status": "success"}], {"async_tasks": {}, "active_queries": {}}
    )
    res3, calls = _replay_with(fake)
    check(res3["abandoned"] == 1 and res3["written"] == 0, "超龄（>6h）→ 放弃不写")
    check(len(fake.writes) == 0 and calls == [], "放弃时不写、不触发下游")
    left = pt.get_store().rows("abandoned")
    check(len(left) == 1 and pt.get_store().count() == 0, "行留痕为 abandoned（可查，不再重试）")

    # max_age=0 = 不放弃（与 P2-3 的「0=关闭」约定一致）
    pt._STORE = pt.PendingTerminalStore(_WORK / "t8b.sqlite")
    _seed_row("success", watched="run-1", age_h=8)
    os.environ["NL2SQL_PENDING_TERMINAL_MAX_AGE_SECS"] = "0"
    try:
        fake2 = _fake_for_row(
            [{"run_id": "run-1", "status": "success"}], {"async_tasks": {}, "active_queries": {}}
        )
        res4, _ = _replay_with(fake2)
    finally:
        os.environ.pop("NL2SQL_PENDING_TERMINAL_MAX_AGE_SECS", None)
    check(res4["written"] == 1, "NL2SQL_PENDING_TERMINAL_MAX_AGE_SECS=0 → 永不放弃（照写）")

    # 非数字阈值 → 退回默认，不静默当 0
    os.environ["NL2SQL_PENDING_TERMINAL_MAX_AGE_SECS"] = "abc"
    try:
        got = pt._env_float("NL2SQL_PENDING_TERMINAL_MAX_AGE_SECS", 21600.0)
    finally:
        os.environ.pop("NL2SQL_PENDING_TERMINAL_MAX_AGE_SECS", None)
    check(got == 21600.0, "负对照：阈值填非数字 → 退回默认（绝不静默变 0=永不放弃/关闭）")


# ── ⑨ 补写器生命周期 + 接线 ────────────────────────────────────────
def verify_reaper_and_wiring() -> None:
    section("⑨ 补写器生命周期 + lifespan 接线")
    from agent.subagents import pending_terminal as pt
    from agent.subagents import sync_subagent_todos as sst
    import langgraph_sdk

    # 启动即跑一轮：残留行（模拟进程重启前留下的）必须自己落地
    pt._STORE = pt.PendingTerminalStore(_WORK / "t9.sqlite")
    _seed_row("success", watched="run-1")
    fake = _fake_for_row(
        [{"run_id": "run-1", "status": "success"}], {"async_tasks": {}, "active_queries": {}}
    )
    orig_get_client = langgraph_sdk.get_client
    orig_update = sst._sync_update_state
    orig_cont = sst._notify_main_agent_continue
    orig_interval = os.environ.get("NL2SQL_PENDING_TERMINAL_INTERVAL_SECS")
    os.environ["NL2SQL_PENDING_TERMINAL_INTERVAL_SECS"] = "300"  # 只靠"启动立即一轮"

    async def _cont(client, main_thread_id, agent_name):
        return None

    langgraph_sdk.get_client = lambda **kw: fake

    def _fake_update_state(thread_id, values):
        fake.writes.append({"thread_id": thread_id, "values": values})
        fake.apply(values)

    sst._sync_update_state = _fake_update_state
    sst._notify_main_agent_continue = _cont
    try:
        first = pt.start_reaper()
        second = pt.start_reaper()
        check(first is True and second is False, "start_reaper 幂等（第二次不再起线程）")
        # 等**第一轮补写真的落地**（看写入，不看行数）：线程起跑本身有调度延迟，
        # 若在这里等"行没了"会立刻超时退出，紧接着 stop_reaper 会在补写线程**还没进
        # 循环体**时置 _STOP —— 于是 `while not _STOP` 直接为假、一轮都没跑，
        # 表现为"启动即补写"假红灯（曾把这条断言误读成补写器没工作）。
        deadline = time.monotonic() + 15
        while not fake.writes and time.monotonic() < deadline:
            time.sleep(0.05)
        check(len(fake.writes) == 1 and pt.get_store().count() == 0,
              "启动即补写一轮：重启前后残留的行自己落地（不等人来问）",
              f"writes={len(fake.writes)} pending={pt.get_store().count()}")
        pt.stop_reaper(timeout=5)
        check(pt._REAPER is None or not pt._REAPER.is_alive(), "stop_reaper 能停干净")
        again = pt.start_reaper()
        alive = pt._REAPER is not None and pt._REAPER.is_alive()
        pt.stop_reaper(timeout=5)
        check(again is True and alive, "停了还能再起（lifespan 反复进入不炸）")
    finally:
        pt.stop_reaper(timeout=5)
        langgraph_sdk.get_client = orig_get_client
        sst._sync_update_state = orig_update
        sst._notify_main_agent_continue = orig_cont
        if orig_interval is None:
            os.environ.pop("NL2SQL_PENDING_TERMINAL_INTERVAL_SECS", None)
        else:
            os.environ["NL2SQL_PENDING_TERMINAL_INTERVAL_SECS"] = orig_interval

    # 接线：真跑 custom_app._lifespan（P2-3 的 UnboundLocalError 只在这种跑法下暴露）
    import api.custom_app as custom_app

    calls: list[str] = []
    orig_start, orig_stop = pt.start_reaper, pt.stop_reaper
    pt.start_reaper = lambda: (calls.append("start"), True)[1]
    pt.stop_reaper = lambda timeout=2.0: calls.append("stop")
    try:
        async def _drive():
            async with custom_app._lifespan(None):
                calls.append("inside")

        asyncio.run(asyncio.wait_for(_drive(), timeout=60))
    finally:
        pt.start_reaper, pt.stop_reaper = orig_start, orig_stop
    check(calls[0] == "start" and "inside" in calls and calls[-1] == "stop",
          "真 lifespan：起补写器 → 服务运行 → 停机时停补写器", str(calls))
    check("start" in calls and calls.index("start") < calls.index("inside"),
          "补写器在 yield **之前**启动（启动即可补上一轮的残留行）")

    src = (_SRC / "agent" / "subagents" / "sync_subagent_todos.py").read_text(encoding="utf-8")
    check(src.count("_handoff_terminal_write(") >= 3,
          "两个放弃点（300s 上限 / 回退守护超限）都调了 _handoff_terminal_write（含定义）",
          f"出现 {src.count('_handoff_terminal_write(')} 次")
    check("render_final_steps(" in src and src.count("render_final_steps(") >= 2,
          "最终步骤渲染与补写器共用（render_final_steps），没有第二份逻辑")


# ── ⑩ CLI ─────────────────────────────────────────────────────────
def verify_cli() -> None:
    section("⑩ CLI（运维手动补写）")
    import subprocess

    from agent.subagents import pending_terminal as pt
    pt._STORE = pt.PendingTerminalStore(_WORK / "t10.sqlite")
    _seed_row("success", watched="run-1")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC)
    env["PYTHONIOENCODING"] = "utf-8"
    env["AGENT_DATA_ROOT"] = str(_WORK)
    # CLI 走独立进程：注册表在另一个库里，故用 --status 验退出码与输出形态
    r = subprocess.run(
        [sys.executable, "-m", "agent.subagents.pending_terminal", "--status"],
        cwd=str(_REPO), env=env, capture_output=True, text=True, timeout=60,
    )
    check(r.returncode == 0, "`--status` 退出码 0", (r.stderr or "")[-120:])
    check('"pending"' in (r.stdout or ""), "`--status` 输出 JSON（含 pending 计数）",
          (r.stdout or "").strip()[:80])


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)
    print("P2-4 子任务终态待补写 + 补写器验证")
    verify_store()
    verify_busy_classifier()
    verify_watcher_handoff()
    verify_replay_happy()
    verify_replay_guards()
    verify_replay_failure_path()
    verify_replay_busy_and_abandon()
    verify_reaper_and_wiring()
    verify_cli()

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
