"""sync watcher 的卡死判据与看门狗标记（2026-10-16 生产事故回归）。

事故里看门狗用**运行时长**（`STALE_RUN_TIMEOUT=600`）判卡死，把一条一直在推进
（todos 7/7、消息游标 44→46）的子 run 在 600.8s 上误杀 —— 而且杀的正是 caliber_gate
核验重写循环里**第三次、本该通过的那一次**，两个完整分析草稿全丢。
判据改成「无进展持续多久」后，本文件钉住：

1. `_progress_probe` 只在真有新消息/新 todo 状态时才算进展（耗时后缀改变不算）。
2. 读不到时返回 None —— 调用方不得据此重置停滞计时（否则抖动就能让卡死任务永生）。
3. 看门狗标记登记/查询的语义与容量边界。
4. 两个时限常量仍按「停滞 < 绝对上限」的关系存在。
"""

import asyncio

import pytest

from agent.subagents import sync_subagent_todos as st


class _FakeClient:
    """只实现 `threads.get_state` 的最小 client（探针也只调这一个）。"""

    def __init__(self, values=None, raises=None):
        self._values = values if values is not None else {}
        self._raises = raises
        self.calls = 0

    class _Threads:
        def __init__(self, outer):
            self._outer = outer

        async def get_state(self, thread_id):
            self._outer.calls += 1
            if self._outer._raises:
                raise self._outer._raises
            return {"values": self._outer._values}

    @property
    def threads(self):
        return self._Threads(self)


def _probe(values=None, raises=None):
    return asyncio.run(st._progress_probe(_FakeClient(values, raises), "sub-1"))


class TestProgressProbe:
    """进展指纹 = 消息数 + 末条消息 id + todos 状态序列。"""

    def test_same_state_yields_same_key(self):
        values = {
            "messages": [{"id": "m1", "role": "ai"}, {"id": "m2", "role": "tool"}],
            "todos": [{"content": "查询执行", "status": "in_progress"}],
        }
        assert _probe(values) == _probe(values)

    def test_new_message_is_progress(self):
        base = {"messages": [{"id": "m1"}]}
        grown = {"messages": [{"id": "m1"}, {"id": "m2"}]}
        assert _probe(base) != _probe(grown)

    def test_same_count_different_last_id_is_progress(self):
        """消息数不变但末条换了（如某轮被替换）也算进展，不能靠计数单打独斗。"""
        a = {"messages": [{"id": "m1"}, {"id": "m2"}]}
        b = {"messages": [{"id": "m1"}, {"id": "m3"}]}
        assert _probe(a) != _probe(b)

    def test_todo_status_change_is_progress(self):
        """write_todos 推进步骤（消息可能还没变）必须算进展。"""
        a = {"messages": [], "todos": [{"content": "A", "status": "pending"}]}
        b = {"messages": [], "todos": [{"content": "A", "status": "completed"}]}
        assert _probe(a) != _probe(b)

    def test_duration_suffix_churn_is_not_progress(self):
        """渲染后的 content 带耗时后缀会自己变；指纹只取 status ⇒ 不算进展。

        这是「有进展 vs 假进展」的分水岭：若把 content 计入指纹，
        卡死任务会因为计时器在跳而永远不被判超时。
        """
        a = {"messages": [], "todos": [{"content": "查询执行 (5s)", "status": "in_progress"}]}
        b = {"messages": [], "todos": [{"content": "查询执行 (37s)", "status": "in_progress"}]}
        assert _probe(a) == _probe(b)

    def test_read_failure_returns_none(self):
        """读失败 ⇒ None（调用方**不得**重置停滞计时）。"""
        assert _probe(raises=RuntimeError("boom")) is None

    def test_empty_state_is_a_valid_key(self):
        """空 state 不是「读不到」：它是「确实什么都没有」，可用于起算停滞。"""
        assert _probe({}) == (0, "", ())


class TestWatchdogKillRegistry:
    """看门狗标记：唯一能区分「被我们杀掉」与「上下文压缩暂停」的证据。"""

    def _clear(self):
        with st._WATCHDOG_KILLED_LOCK:
            st._WATCHDOG_KILLED_RUNS.clear()

    def test_round_trip(self):
        self._clear()
        st.note_watchdog_kill("run-a", "连续 600s 无任何进展")
        assert st.watchdog_kill_reason("run-a") == "连续 600s 无任何进展"
        self._clear()

    def test_unknown_run_returns_none(self):
        assert st.watchdog_kill_reason("never-registered") is None

    @pytest.mark.parametrize("bad", [None, ""])
    def test_none_safe(self, bad):
        st.note_watchdog_kill(bad, "x")  # 不抛
        assert st.watchdog_kill_reason(bad) is None

    def test_bounded_registry_evicts_oldest(self):
        """进程长跑时这张表必须有界（标记只服务近期轮询）。"""
        self._clear()
        for i in range(st._WATCHDOG_KILLED_MAX + 5):
            st.note_watchdog_kill(f"run-{i}", "reason")
        assert len(st._WATCHDOG_KILLED_RUNS) == st._WATCHDOG_KILLED_MAX
        assert st.watchdog_kill_reason("run-0") is None  # 最老的被淘汰
        assert st.watchdog_kill_reason(f"run-{st._WATCHDOG_KILLED_MAX + 4}") == "reason"
        self._clear()


class TestTimeoutConstants:
    """两个时限的关系：停滞判据必须严于绝对上限，否则绝对上限永远轮不到。"""

    def test_absolute_is_larger_than_stall(self):
        assert st.ABSOLUTE_RUN_TIMEOUT > st.STALE_RUN_TIMEOUT

    def test_probe_interval_is_not_every_cycle(self):
        """探针要 get_state，成本得克制（否则 2 Hz 轮询再翻一倍）。"""
        assert st.STALL_PROBE_EVERY_CYCLES > 1

    def test_failure_report_write_is_retried(self):
        """failure_reported 写在续跑 run in-flight 时 ⇒ 必须重试（事故里一次就放弃了）。"""
        assert st._FAILURE_REPORT_WRITE_RETRIES > 1
        assert st._FAILURE_REPORT_WRITE_BACKOFF_SECS > 0
