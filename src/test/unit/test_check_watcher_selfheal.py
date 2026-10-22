"""`check_async_task` 的 sync watcher 自愈（2026-10-22 dev-34 事故）。

背景：watcher（`sync_subagent_todos.launch_sync`）起的是**派发进程内的 daemon
线程**——发版换容器 / 进程重启会把它连同主 run 一起杀掉，而子 run 会在新容器里
被孤儿回收后继续跑（`Background worker expired, adding to queue`，实测等了 403s）。
于是没人再写 `subagent_steps_map`：前端卡片永久停在旧快照（事故里是
「└ Schema 提取与裁剪 (2s...)」），**即使子 run 后来 success** 也刷不回来。

修法：主 agent 每次 `check_async_task` 顺手探一次——state 说任务还在 running、
但本进程没有 watcher，就按 `_relaunch_sync_after_redispatch` 同款重建。
watcher 启动自愈（P1-9）会让它自己收敛到终态：子 run 已结束 → 立刻补写最终
steps 再退出；还在跑 → 正常接着盯。
"""

import asyncio
import types

TASK = {
    "task_id": "sub-1",
    "agent_name": "nl2sql",
    "thread_id": "sub-1",
    "run_id": "run-1",
    "status": "running",
    "description": "查前一天工时",
}


class _Runtime:
    """最小 ToolRuntime 替身：config 里的 thread_id 就是主线程 id。"""

    def __init__(self, thread_id="main-1", state=None):
        self.config = {"configurable": {"thread_id": thread_id}} if thread_id else {}
        self.execution_info = None
        self.state = state if state is not None else {"async_tasks": {"sub-1": dict(TASK)}}
        self.tool_call_id = "call-1"


def _patch_sync(monkeypatch, alive=False):
    """替换 sync 模块的存活探测与拉起，收集 launch_sync 调用。"""
    from agent.subagents import sync_subagent_todos as sst

    calls = []
    monkeypatch.setattr(sst, "is_sync_alive", lambda tid: alive)
    monkeypatch.setattr(sst, "launch_sync", lambda *a, **kw: calls.append((a, kw)))
    return calls


# ── 判据本身 ────────────────────────────────────────────────────


def test_relaunches_when_running_and_watcher_dead(monkeypatch):
    calls = _patch_sync(monkeypatch, alive=False)
    from agent.subagents.check_progress import _ensure_sync_watcher

    assert _ensure_sync_watcher(dict(TASK), _Runtime()) is True

    assert len(calls) == 1, "watcher 失联 + 任务 running ⇒ 必须重建"
    args, kwargs = calls[0]
    assert args[:3] == ("main-1", "sub-1", "nl2sql")
    assert args[3]["run_id"] == "run-1", "要把 async_tasks 条目交给 watcher（含 description）"
    assert kwargs.get("pin_run_id", False) is False, "盯最新 run，不钉死旧 run_id"


def test_skips_when_watcher_alive(monkeypatch):
    calls = _patch_sync(monkeypatch, alive=True)
    from agent.subagents.check_progress import _ensure_sync_watcher

    assert _ensure_sync_watcher(dict(TASK), _Runtime()) is False
    assert calls == [], "launch_sync 非幂等：watcher 活着时不许再起一个"


def test_skips_when_task_already_terminal(monkeypatch):
    calls = _patch_sync(monkeypatch, alive=False)
    from agent.subagents.check_progress import _ensure_sync_watcher

    for status in ("success", "error", "timeout", "cancelled"):
        task = dict(TASK, status=status)
        assert _ensure_sync_watcher(task, _Runtime()) is False, status
    assert calls == [], "卡片已终态就没有要追平的进度"


def test_skips_without_main_thread_id(monkeypatch):
    """取不到主线程 id 时只能放过——但绝不能抛（check 工具不能因此报错）。"""
    calls = _patch_sync(monkeypatch, alive=False)
    from agent.subagents.check_progress import _ensure_sync_watcher

    assert _ensure_sync_watcher(dict(TASK), _Runtime(thread_id=None)) is False
    assert calls == []


def test_skips_non_dict_or_missing_task_id(monkeypatch):
    calls = _patch_sync(monkeypatch, alive=False)
    from agent.subagents.check_progress import _ensure_sync_watcher

    assert _ensure_sync_watcher(None, _Runtime()) is False
    assert _ensure_sync_watcher("not-a-task", _Runtime()) is False
    assert _ensure_sync_watcher({"status": "running"}, _Runtime()) is False
    assert calls == []


# ── 接线：两条 check 路径都要走到自愈 ─────────────────────────────


def _fake_mod():
    return types.SimpleNamespace(
        _resolve_tracked_task=lambda task_id, runtime: dict(TASK),
        _build_check_result=lambda run, tid, values, full=False: {
            "status": "running",
            "messages": [],
        },
        _build_check_command=lambda result, task, call_id: ("CMD", result["status"]),
    )


def _build_tool(monkeypatch):
    from agent.subagents import check_progress as cp

    monkeypatch.setattr(cp, "_add_incremental", lambda *a, **kw: None)
    monkeypatch.setattr(cp, "_reconcile_result_with_task", lambda *a, **kw: None)
    return cp._make_patched_build_check_tool(_fake_mod(), lambda clients: types.SimpleNamespace())


def test_check_tool_relaunches_on_sync_path(monkeypatch):
    calls = _patch_sync(monkeypatch, alive=False)
    clients = types.SimpleNamespace(
        get_sync=lambda name: types.SimpleNamespace(
            runs=types.SimpleNamespace(get=lambda thread_id, run_id: {"status": "running"}),
            threads=types.SimpleNamespace(get_state=lambda thread_id: {"values": {}}),
        )
    )
    tool = _build_tool(monkeypatch)(clients)

    assert tool.func("sub-1", _Runtime()) == ("CMD", "running")
    assert len(calls) == 1, "sync check 路径必须走到自愈"


def test_check_tool_relaunches_on_async_path(monkeypatch):
    from agent.subagents import check_progress as cp

    calls = _patch_sync(monkeypatch, alive=False)

    async def _offload(fn, *a, **kw):
        return fn(*a, **kw)

    monkeypatch.setattr(cp, "offload", _offload)

    class _Runs:
        async def get(self, thread_id, run_id):
            return {"status": "running"}

    class _Threads:
        async def get_state(self, thread_id):
            return {"values": {}}

    clients = types.SimpleNamespace(
        get_async=lambda name: types.SimpleNamespace(runs=_Runs(), threads=_Threads())
    )
    tool = _build_tool(monkeypatch)(clients)

    out = asyncio.run(tool.coroutine("sub-1", _Runtime()))
    assert out == ("CMD", "running")
    assert len(calls) == 1, "async check 路径必须走到自愈"
