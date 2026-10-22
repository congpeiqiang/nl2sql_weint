"""「用户点停止」与「发版重启把 run 带走」必须能分开（2026-10-22 用户要求）。

三种来源在平台 run 状态上**完全同形**（都是 `interrupted`）：

1. 用户点前端「停止」→ `POST /api/threads/{task_id}/cancel`（`api/task_cancel.py`
   已把 `async_tasks[task].status` 写成 `cancelled`，watcher 走取消分支收尾）；
2. deepagents 上下文压缩的瞬时暂停 → 几秒内自己回到 running/success；
3. **发版/重启换进程**：上一任进程手里的 run 被留下，新进程从没跑过它。

1 有自己的一手证据（cancelled 已落库）；2 与 3 只能靠时间分：
**run 最后一次状态变化（`updated_at`）早于本进程启动 ⇒ 它只可能死在上一任手里**。
这条判据不需要等 120s 的 `INTERRUPTED_STUCK_TIMEOUT`，观测到即成立（证据不会
随时间变强），且必须在 120s 兜底之前判——否则重启遗留会被错报成「执行超时」。

展示侧改动（用户可见文案）：
- `async_tasks[task]["terminal_cause"] = "service_restart"`（机器可读）
- `async_tasks[task]["error"]` = 「因服务重启中断（不是用户取消），可重试」
- 步骤头 / 失败汇报文案换成重启语义，不再与「已被取消」混为一谈
"""

from datetime import datetime, timezone

import pytest

from agent.subagents import sync_subagent_todos as sst

PROC_START = datetime(2026, 10, 22, 5, 31, 5, tzinfo=timezone.utc).timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _run(status: str = "interrupted", updated_at=None, **extra):
    rec = {"run_id": "run-1", "status": status}
    if updated_at is not None:
        rec["updated_at"] = updated_at
    rec.update(extra)
    return rec


# ── 判据：interrupted 且早于本进程启动 ⇒ 重启带走 ──────────────────


def test_interrupted_run_before_process_start_is_restart_kill():
    """上一任进程留下的 interrupted run（早于本进程启动）⇒ 判重启。"""
    rec = _run(updated_at=_iso(PROC_START - 60))
    assert sst._killed_by_restart(rec, process_started_at=PROC_START) is True


def test_interrupted_run_updated_in_this_process_is_not_restart_kill():
    """本进程内才变成 interrupted 的（上下文压缩瞬时暂停）⇒ 不许判重启。"""
    rec = _run(updated_at=_iso(PROC_START + 10))
    assert sst._killed_by_restart(rec, process_started_at=PROC_START) is False


def test_skew_tolerance_keeps_borderline_run_alive():
    """起进程/落 updated_at 之间有秒级抖动，贴着启动时刻的不算重启。"""
    skew = sst._RESTART_SKEW_SECONDS
    just_inside = _run(updated_at=_iso(PROC_START - skew / 2))
    just_outside = _run(updated_at=_iso(PROC_START - skew - 1))
    assert sst._killed_by_restart(just_inside, process_started_at=PROC_START) is False
    assert sst._killed_by_restart(just_outside, process_started_at=PROC_START) is True


@pytest.mark.parametrize("status", ["running", "success", "error", "timeout", "pending"])
def test_only_interrupted_status_can_be_restart_kill(status):
    """只有 interrupted 才可能是「被重启留下」；其它状态各有自己的收尾路径。"""
    rec = _run(status=status, updated_at=_iso(PROC_START - 600))
    assert sst._killed_by_restart(rec, process_started_at=PROC_START) is False


@pytest.mark.parametrize(
    "rec",
    [
        None,
        {},
        _run(updated_at=None),                     # 平台没给时间
        _run(updated_at="not-a-timestamp"),        # 解析不了
        _run(updated_at=""),                       # 空串
    ],
)
def test_unknown_timestamp_is_fail_open(rec):
    """读不到时间就**不许**断言重启（fail-open：宁可等 120s 兜底，也不误报）。"""
    assert sst._killed_by_restart(rec, process_started_at=PROC_START) is False


def test_z_suffix_timestamp_is_parsed():
    """平台时间戳可能带 Z 后缀，不能因此判成「解析不了」。"""
    raw = _iso(PROC_START - 30).replace("+00:00", "Z")
    assert sst._killed_by_restart(_run(updated_at=raw), process_started_at=PROC_START) is True


# ── 展示文案：重启 ≠ 取消 ────────────────────────────────────────


def test_failure_label_distinguishes_restart_from_user_cancel():
    assert sst._failure_label("cancelled") == "已被取消"
    assert sst._failure_label("error") == "执行失败"
    assert sst._failure_label("timeout") == "执行超时"
    _restart = sst._failure_label("error", cause=sst.CAUSE_SERVICE_RESTART)
    assert "重启" in _restart, _restart
    assert _restart != sst._failure_label("cancelled"), "重启文案不能与「已被取消」同形"
    assert _restart == sst.RESTART_KILLED_TEXT


def test_failure_notice_for_restart_says_retryable_and_not_user_cancel():
    text = sst._failure_notice(
        "nl2sql", "error", cause=sst.CAUSE_SERVICE_RESTART, err="ignored"
    )
    assert text.startswith("[系统自动通知]")  # langfuse/反馈过滤依赖该前缀
    assert sst.RESTART_KILLED_TEXT in text
    assert "不是用户点了停止" in text
    assert "ignored" not in text, "重启语义自带文案，别再拼 run.error"


def test_failure_notice_for_user_cancel_is_unchanged():
    """老路径逐字不变（已发版文案，不许漂移）。"""
    assert sst._failure_notice("nl2sql", "cancelled") == (
        "[系统自动通知] 子任务 nl2sql 已被取消。请向用户说明失败原因与建议。"
    )
    assert sst._failure_notice("nl2sql", "error", err="boom") == (
        "[系统自动通知] 子任务 nl2sql 执行失败：boom。请向用户说明失败原因与建议。"
    )


def test_done_label_for_restart_does_not_say_completed_or_failed():
    assert sst._done_label("error", cause=sst.CAUSE_SERVICE_RESTART) == "服务重启中断"
    assert sst._done_label("error") == "执行失败"
    assert sst._done_label("cancelled") == "已取消"
    # 未知状态仍然回落到「已完成」老语义（render_final_steps 的老契约）
    assert sst._done_label("something-new") == "已完成"


def test_render_final_steps_header_carries_restart_cause():
    steps = sst.render_final_steps(
        [{"content": "结果汇总", "status": "completed"}],
        "nl2sql",
        "01a16218-0000-0000-0000-000000000000",
        "error",
        cause=sst.CAUSE_SERVICE_RESTART,
    )
    assert "服务重启中断" in steps[0]["content"]
    assert "执行失败" not in steps[0]["content"]


def test_check_result_reports_restart_kill_as_failure_not_paused():
    """check_progress 的 interrupted 分支：重启遗留不许再报「短暂暂停会自动恢复」。"""
    from agent.subagents import check_progress as cp

    run = {
        "run_id": "run-restart",
        "status": "interrupted",
        "updated_at": _iso(sst._PROCESS_STARTED_AT - 600),
    }
    res = cp._enhanced_build_check_result(run, "thread-1", None)
    assert res["status"] == "error", res
    assert res.get("error_kind") == "service_restart", res
    assert "restart" in str(res.get("note", "")).lower(), res


def test_check_result_keeps_pause_wording_for_fresh_interrupt():
    """本进程内的瞬时暂停仍然报「会自动恢复」（老行为不许漂移）。"""
    from agent.subagents import check_progress as cp

    run = {
        "run_id": "run-pause",
        "status": "interrupted",
        "updated_at": _iso(sst._PROCESS_STARTED_AT + 5),
    }
    res = cp._enhanced_build_check_result(run, "thread-1", None)
    assert res["status"] == "running", res
    assert "resume automatically" in str(res.get("note", ""))


def test_check_rewrite_preserves_terminal_cause():
    """check 重建 async_tasks 整条时必须带上 terminal_cause（否则前端只剩 error 文案）。"""
    from agent.subagents import check_progress as cp

    task = {
        "task_id": "t1",
        "agent_name": "nl2sql",
        "thread_id": "th1",
        "run_id": "r1",
        "status": "error",
        "created_at": "2026-10-22T00:00:00Z",
        "last_checked_at": "2026-10-22T00:00:00Z",
        "last_updated_at": "2026-10-22T00:00:00Z",
        "error": sst.RESTART_KILLED_TEXT,
        "terminal_cause": sst.CAUSE_SERVICE_RESTART,
    }
    result = {"status": "error", "thread_id": "th1"}
    cmd = cp._enhanced_build_check_command(result, dict(task), "call-1")
    entry = (cmd.update or {})["async_tasks"]["t1"]
    assert entry.get("terminal_cause") == sst.CAUSE_SERVICE_RESTART, entry


# ── 用户点「停止」时的回写：已死的 run 如实记死因 ──────────────────


def test_stop_click_on_restart_dead_run_records_restart_not_cancelled():
    """刷新后点「停止」清理一张卡死的卡：写 error/服务重启中断，不是「已被取消」。"""
    from api import task_cancel

    run = _run(
        "interrupted", updated_at=_iso(sst._PROCESS_STARTED_AT - 600)
    )
    status, extra = task_cancel._terminal_override("interrupted", run)
    assert status == "error", (status, extra)
    assert extra.get("terminal_cause") == sst.CAUSE_SERVICE_RESTART, extra
    assert extra.get("error") == sst.RESTART_KILLED_TEXT, extra


def test_stop_click_on_live_interrupted_run_keeps_cancel_semantics():
    """本进程内才中断的 run：用户这次点击就是取消 ⇒ 仍写 cancelled（老语义）。"""
    from api import task_cancel

    run = _run(
        "interrupted", updated_at=_iso(sst._PROCESS_STARTED_AT + 3)
    )
    assert task_cancel._terminal_override("interrupted", run) == ("cancelled", {})
    # 读不到 run 记录 ⇒ fail-open，按取消处理（不许误报重启）
    assert task_cancel._terminal_override("interrupted", None) == ("cancelled", {})


@pytest.mark.parametrize("status", ["success", "error", "timeout", "cancelled"])
def test_stop_click_on_other_terminal_run_writes_actual_status(status):
    from api import task_cancel

    run = _run(status, updated_at=_iso(sst._PROCESS_STARTED_AT - 600))
    assert task_cancel._terminal_override(status, run) == (status, {})


# ── 重启成因要能穿过 watcher 自愈/再拉起 ──────────────────────────


def test_boot_cause_survives_watcher_relaunch():
    """watcher 重启后从启动方条目接上成因：否则会把重启中断说成「执行失败」。"""
    assert sst._boot_terminal_cause(
        {"status": "error", "terminal_cause": sst.CAUSE_SERVICE_RESTART}
    ) == sst.CAUSE_SERVICE_RESTART
    assert sst._failure_label(
        "error", sst._boot_terminal_cause({"terminal_cause": "service_restart"})
    ) == sst.RESTART_KILLED_TEXT


@pytest.mark.parametrize(
    "task", [None, {}, {"status": "error"}, {"terminal_cause": ""}, {"terminal_cause": 3}]
)
def test_boot_cause_absent_or_bogus_is_none(task):
    assert sst._boot_terminal_cause(task) is None


def test_process_start_anchor_is_sane_and_powers_default_judgement():
    """锚点是真实进程启动时刻：不晚于现在，且能被默认参数（不显式传）用上。"""
    import time

    assert isinstance(sst._PROCESS_STARTED_AT, float)
    assert sst._PROCESS_STARTED_AT <= time.time()
    assert sst._killed_by_restart(
        _run(updated_at=_iso(sst._PROCESS_STARTED_AT - 30))
    ) is True
    assert sst._killed_by_restart(
        _run(updated_at=_iso(sst._PROCESS_STARTED_AT + 30))
    ) is False
