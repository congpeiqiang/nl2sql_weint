"""终态保护 + interrupted 分诊（2026-10-16 生产事故回归）。

事故（64 环境会话 01a143da）：子 agent 正常跑了 600s 被看门狗误杀 →
watcher 落 `timeout` 终态 → 失败汇报续跑 run 里的 `check_async_task` 读到平台
run 状态 `interrupted`（cancel 的默认动作就是变成它），旧代码把它归一成
`running` + 「短暂暂停、会自动恢复」→ `_enhanced_build_check_command` 把
`running` 写回 async_tasks，**终态被打回**，卡片复活、用户被告知「会自动恢复」，
白等 35 分钟。

本文件钉住三件事：
1. `_reconcile_result_with_task` / `_enhanced_build_check_command`：条目已终态时
   结果不得是非终态（且不影响重派发 —— 重派发会先把条目写成 running）。
2. `_enhanced_build_check_result` 的 interrupted 分支：看门狗标记与时长熔断
   必须如实报 timeout，只有真正的瞬时暂停才允许说「会自动恢复」。
3. 判据缺失时 fail-open（宁可当成暂停，也不能凭一次读失败就杀任务）。
"""

import pytest

from agent.subagents import check_progress as cp


# ── 工具 ────────────────────────────────────────────────────────────


def _iso_ago(seconds: float) -> str:
    """`seconds` 秒之前的 ISO-8601 UTC 时间串（run.updated_at 的形状）。"""
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _task(status: str, **extra) -> dict:
    task = {
        "task_id": "01a143da-f67a-7000-8000-000000000001",
        "agent_name": "nl2sql",
        "thread_id": "01a143da-f67a-7000-8000-000000000001",
        "run_id": "01a143da-f68a-7fe1-99b7-937d6a0c5f33",
        "status": status,
        "created_at": "2026-10-16T08:36:25Z",
        "last_checked_at": "2026-10-16T08:46:29Z",
        "last_updated_at": "2026-10-16T08:46:29Z",
    }
    task.update(extra)
    return task


class TestReconcileResultWithTask:
    """结果与 async_tasks 条目对账：条目终态 ⇒ 结果不得非终态。"""

    def test_terminal_task_forces_terminal_result(self):
        result = {"status": "running", "note": "will resume automatically"}
        task = _task("timeout", error="看门狗超时")

        cp._reconcile_result_with_task(result, task)

        assert result["status"] == "timeout"
        # 「会自动恢复」这类乐观说明必须被丢弃，否则模型会照读给用户
        assert "note" not in result
        assert result["error"] == "看门狗超时"
        assert "will NOT resume" in result["state_note"]

    @pytest.mark.parametrize("terminal", ["success", "error", "timeout", "cancelled"])
    def test_every_terminal_status_is_honored(self, terminal):
        result = {"status": "running"}
        cp._reconcile_result_with_task(result, _task(terminal))
        assert result["status"] == terminal

    def test_terminal_result_is_left_alone(self):
        """两边都终态 → 不改判（终态之间不互相覆盖）。"""
        result = {"status": "success", "result": "答完了"}
        cp._reconcile_result_with_task(result, _task("success"))
        assert result["status"] == "success"
        assert "state_note" not in result

    def test_running_task_does_not_block_redispatch(self):
        """重派发不受影响：条目是 running（update_async_task 刚写的）→ 原样放行。"""
        result = {"status": "running"}
        cp._reconcile_result_with_task(result, _task("running"))
        assert result == {"status": "running"}

    def test_interrupted_task_is_not_treated_as_terminal(self):
        """条目停在 interrupted：它不是终态（wait 会自恢复），不做对账。"""
        result = {"status": "running"}
        cp._reconcile_result_with_task(result, _task("interrupted"))
        assert result == {"status": "running"}

    def test_missing_task_or_status_is_safe(self):
        result = {"status": "running"}
        cp._reconcile_result_with_task(result, None)
        cp._reconcile_result_with_task(result, {})
        assert result == {"status": "running"}

    def test_failed_result_keeps_its_own_error(self):
        """结果自带 error（如工作区写失败）时不被条目的 error 盖掉。"""
        result = {"status": "running", "error": "本轮的错"}
        cp._reconcile_result_with_task(result, _task("timeout", error="旧原因"))
        assert result["error"] == "本轮的错"
        assert result["error_kind"] == "timeout"


class TestBuildCheckCommandTerminalGate:
    """`_enhanced_build_check_command` 是 result.status 落 async_tasks 的唯一漏斗。"""

    def _cmd(self, result, task):
        return cp._enhanced_build_check_command(result, task, "call-1")

    def test_terminal_task_is_never_written_back_as_running(self):
        """硬闸：无论谁调用，终态都不许被写成中间态（本次事故的核心）。"""
        result = {"status": "running"}
        out = self._cmd(result, _task("timeout", error="无进展 600s"))

        entry = out.update["async_tasks"][_task("timeout")["task_id"]]
        assert entry["status"] == "timeout"
        # 终态未被刷新 ⇒ last_updated_at 保持 watcher 写入的那一刻
        assert entry["last_updated_at"] == "2026-10-16T08:46:29Z"
        assert entry["error"] == "无进展 600s"

    def test_redispatch_still_works(self):
        """重派发链不受闸门影响：条目 running + 结果 running 正常写入。"""
        task = _task("running")
        out = self._cmd({"status": "running"}, task)
        entry = out.update["async_tasks"][task["task_id"]]
        assert entry["status"] == "running"

    def test_terminal_result_updates_last_updated_at(self):
        """正常终态写入（条目 running → 结果 success）照旧刷新时间戳。"""
        out = self._cmd({"status": "success"}, _task("running"))
        entry = out.update["async_tasks"][_task("running")["task_id"]]
        assert entry["status"] == "success"
        assert entry["last_updated_at"] != "2026-10-16T08:46:29Z"

    def test_failure_reported_flag_survives(self):
        """去重标记是跨线程状态，重建整条时必须带上（否则同一失败反复汇报）。"""
        task = _task("running", failure_reported=True)
        out = self._cmd({"status": "running"}, task)
        entry = out.update["async_tasks"][task["task_id"]]
        assert entry["failure_reported"] is True


class TestInterruptedStuckSeconds:
    """interrupted 停留时长取 `updated_at`（不是 created_at）。"""

    def test_measures_from_updated_at(self):
        assert cp._interrupted_stuck_seconds({"updated_at": _iso_ago(300)}) == pytest.approx(
            300, abs=5
        )

    def test_long_running_query_interrupted_late_is_not_penalized(self):
        """长查询在第 10 分钟才被中断 ⇒ 时长按「刚中断」算，不按 run 总年龄。"""
        run = {"created_at": _iso_ago(600), "updated_at": _iso_ago(1)}
        assert cp._interrupted_stuck_seconds(run) < cp._INTERRUPTED_STUCK_GRACE_SECS

    def test_unparseable_returns_none(self):
        for bad in ({}, {"updated_at": ""}, {"updated_at": "not-a-date"}, None):
            assert cp._interrupted_stuck_seconds(bad) is None


class TestInterruptedTriage:
    """interrupted 有三个同形来源，分诊必须靠证据而不是猜。"""

    @pytest.fixture(autouse=True)
    def _process_has_been_up_for_an_hour(self, monkeypatch):
        """把「本进程启动时刻」钉在一小时前。

        本文件所有时间戳都是「本进程内发生的」（`_iso_ago`），语义上不该被判成
        「上一任进程留下的 run」。不钉锚点时，测试进程本身只活了几秒，
        `_iso_ago(180)` 会早于进程启动 ⇒ 被正确地判成重启中断 —— 但那是测试
        夹具的假象，不是被测行为。重启判据本身另见 test_restart_kill_state.py。
        """
        import time as _t

        from agent.subagents import sync_subagent_todos as sst

        monkeypatch.setattr(sst, "_PROCESS_STARTED_AT", _t.time() - 3600)

    @staticmethod
    def _result(run):
        return cp._enhanced_build_check_result(
            run, "sub-thread", {"messages": []}, full=False
        )

    @staticmethod
    def _run(**extra):
        run = {
            "run_id": "01a143da-f68a-7fe1-99b7-937d6a0c5f33",
            "status": "interrupted",
            "created_at": _iso_ago(900),
            "updated_at": _iso_ago(1),  # 刚被中断 ⇒ 看起来像瞬时暂停
        }
        run.update(extra)
        return run

    def test_watchdog_kill_is_reported_as_timeout(self, monkeypatch):
        """看门狗强杀：即使刚中断 1 秒，也必须如实报失败（事故里被说成「会自动恢复」）。"""
        monkeypatch.setattr(
            cp, "_watchdog_kill_reason", lambda run: "连续 600s 无任何进展"
        )
        result = self._result(self._run())

        assert result["status"] == "timeout"
        assert result["error_kind"] == "timeout"
        assert "will NOT resume" in result["note"]
        assert "automatically" not in result["note"].split("NOT")[0]

    def test_stuck_too_long_is_reported_as_timeout(self, monkeypatch):
        """无标记（进程重启丢了）也要靠时长熔断兜底。"""
        monkeypatch.setattr(cp, "_watchdog_kill_reason", lambda run: None)
        run = self._run(updated_at=_iso_ago(cp._INTERRUPTED_STUCK_GRACE_SECS + 60))
        result = self._result(run)

        assert result["status"] == "timeout"
        assert "will NOT resume" in result["note"]

    def test_genuine_brief_pause_keeps_resume_semantics(self, monkeypatch):
        """真正的上下文压缩暂停：维持旧语义，但补一句「数分钟后仍中断即为失败」。"""
        monkeypatch.setattr(cp, "_watchdog_kill_reason", lambda run: None)
        result = self._result(self._run(updated_at=_iso_ago(5)))

        assert result["status"] == "running"
        assert "resume automatically" in result["note"]
        assert "watchdog" in result["note"]

    def test_missing_marker_and_timestamp_fails_open(self, monkeypatch):
        """两条判据都取不到 ⇒ 不杀（宁可当暂停，也不能凭一次读失败判失败）。"""
        monkeypatch.setattr(cp, "_watchdog_kill_reason", lambda run: None)
        result = self._result(self._run(updated_at=""))

        assert result["status"] == "running"

    def test_kill_marker_lookup_is_wired_to_the_watcher_registry(self):
        """真实接线：watcher 登记的标记必须能被 check 侧读到（事故里两者互不相识）。"""
        from agent.subagents.sync_subagent_todos import (
            note_watchdog_kill,
            watchdog_kill_reason,
        )

        run_id = "test-run-wired-0001"
        note_watchdog_kill(run_id, "连续 600s 无任何进展")
        try:
            assert cp._watchdog_kill_reason({"run_id": run_id}) == "连续 600s 无任何进展"
            result = self._result(self._run(run_id=run_id))
            assert result["status"] == "timeout"
        finally:
            from agent.subagents import sync_subagent_todos as st

            st._WATCHDOG_KILLED_RUNS.pop(run_id, None)
