"""定时任务 SQLite 存储层单测（agent/scheduler/store.py）。

覆盖面（方案 §2/§3 的存储侧不变量）：
- CRUD + owner 过滤（store 层裸操作，owner 强制在 service 层）
- due 选取四条件：enabled=1 / state='scheduled' / next_run_at<=now / 不在飞
- CAS 认领互斥（tick vs 手动 run 双路）、执行前推进 next_run_at（at-most-once）
- one-shot / 有限次 预扣（claim 时 repeat_completed+1、耗尽即 completed）
- task_runs 流水：record（running）→ complete（终态回写任务行 + 清在飞）
- fire 失败回拨（retry_count 由调用方算，store 只落值）
"""
import pytest

NOW = "2026-01-01T12:00:00+00:00"
PAST = "2026-01-01T11:00:00+00:00"
FUTURE = "2026-01-01T13:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    from agent.scheduler.store import ScheduledTaskStore

    return ScheduledTaskStore(tmp_path / "scheduled_tasks.sqlite")


def _row(**kw):
    base = dict(
        owner="u1",
        name="每日工时报告",
        prompt="统计上月各部门工时",
        db_name="db1",
        sched_kind="interval",
        sched_expr="60",
        tz="Asia/Shanghai",
        next_run_at=PAST,
    )
    base.update(kw)
    return base


class TestCrud:
    def test_insert_and_get(self, store):
        tid = store.insert_task(_row())
        assert isinstance(tid, str) and len(tid) == 12
        row = store.get_task(tid)
        assert row["owner"] == "u1"
        assert row["name"] == "每日工时报告"
        assert row["state"] == "scheduled"
        assert row["enabled"] == 1
        assert row["running_thread_id"] == ""
        # 时间戳口径与 eval_queue 逐字一致（UTC ISO timespec=seconds）
        assert row["created_at"].endswith("+00:00")

    def test_get_missing_returns_none(self, store):
        assert store.get_task("nope") is None

    def test_list_tasks_owner_isolation(self, store):
        store.insert_task(_row(owner="u1"))
        store.insert_task(_row(owner="u2"))
        assert len(store.list_tasks(owner="u1")) == 1
        assert len(store.list_tasks(owner=None)) == 2

    def test_update_task_touches_updated_at(self, store):
        tid = store.insert_task(_row())
        before = store.get_task(tid)["updated_at"]
        assert store.update_task(tid, {"name": "新名", "prompt": "新问题"}) is True
        row = store.get_task(tid)
        assert row["name"] == "新名" and row["prompt"] == "新问题"
        assert row["updated_at"] >= before

    def test_update_task_rejects_unknown_column(self, store):
        tid = store.insert_task(_row())
        with pytest.raises(ValueError):
            store.update_task(tid, {"DROP TABLE x": "1"})

    def test_delete_task(self, store):
        tid = store.insert_task(_row())
        assert store.delete_task(tid) is True
        assert store.get_task(tid) is None
        assert store.delete_task(tid) is False

    def test_count_by_owner(self, store):
        store.insert_task(_row(owner="u1"))
        store.insert_task(_row(owner="u1"))
        store.insert_task(_row(owner="u2"))
        assert store.count_by_owner("u1") == 2
        assert store.count_by_owner("nobody") == 0


class TestDueAndClaim:
    def test_due_tasks_four_filters(self, store):
        due = store.insert_task(_row(next_run_at=PAST))
        store.insert_task(_row(next_run_at=FUTURE))          # 未到点
        store.insert_task(_row(next_run_at=PAST, state="paused"))   # 暂停
        store.insert_task(_row(next_run_at=PAST, enabled=0))        # 停用
        store.insert_task(_row(next_run_at=PAST, running_thread_id="th-x"))  # 在飞
        rows = store.due_tasks(NOW)
        assert [r["id"] for r in rows] == [due]

    def test_due_tasks_limit_and_order(self, store):
        a = store.insert_task(_row(next_run_at="2026-01-01T10:00:00+00:00"))
        b = store.insert_task(_row(next_run_at=PAST))
        rows = store.due_tasks(NOW, limit=1)
        assert [r["id"] for r in rows] == [a]  # next_run_at 升序
        assert len(store.due_tasks(NOW)) == 2

    def test_claim_cas_mutual_exclusion(self, store):
        """同 expected_next 只能认领一次 —— tick 与手动 run 双路互斥的根基。"""
        tid = store.insert_task(_row(next_run_at=PAST, repeat_times=3))
        assert store.claim(tid, PAST, FUTURE, NOW) is True
        row = store.get_task(tid)
        assert row["next_run_at"] == FUTURE          # 执行前先推进（at-most-once）
        assert row["running_thread_id"] != ""        # 在飞占位（防重入）
        assert row["repeat_completed"] == 1          # 预扣
        # 第二路用同一个旧值认领 → CAS 失败，预扣不重复
        assert store.claim(tid, PAST, FUTURE, NOW) is False
        assert store.get_task(tid)["repeat_completed"] == 1

    def test_claim_skips_inflight(self, store):
        tid = store.insert_task(_row(next_run_at=PAST, running_thread_id="th-old"))
        assert store.claim(tid, PAST, FUTURE, NOW) is False

    def test_claim_once_completes(self, store):
        tid = store.insert_task(_row(sched_kind="once", sched_expr=PAST))
        assert store.claim(tid, PAST, "", NOW) is True
        row = store.get_task(tid)
        assert row["state"] == "completed"
        assert row["next_run_at"] == ""

    def test_claim_last_repeat_completes(self, store):
        tid = store.insert_task(_row(next_run_at=PAST, repeat_times=1))
        assert store.claim(tid, PAST, FUTURE, NOW) is True
        row = store.get_task(tid)
        assert row["repeat_completed"] == 1
        assert row["state"] == "completed"  # 预扣后耗尽 → 不再排下一次


class TestRunLifecycle:
    def test_set_running_thread(self, store):
        tid = store.insert_task(_row(next_run_at=PAST))
        store.claim(tid, PAST, FUTURE, NOW)
        store.set_running_thread(tid, "th-real", "run-1")
        assert store.get_task(tid)["running_thread_id"] == "th-real"

    def test_inflight_tasks(self, store):
        tid = store.insert_task(_row(next_run_at=PAST))
        store.claim(tid, PAST, FUTURE, NOW)
        store.insert_task(_row(next_run_at=FUTURE))
        inflight = store.inflight_tasks()
        assert [r["id"] for r in inflight] == [tid]

    def test_record_and_complete_run(self, store):
        tid = store.insert_task(_row(next_run_at=PAST))
        store.claim(tid, PAST, FUTURE, NOW)
        store.set_running_thread(tid, "th-1", "run-1")
        rid = store.record_run(tid, "th-1", "run-1", NOW)
        assert isinstance(rid, int)
        store.complete_run(tid, "th-1", "success", "", "2026-01-01T12:05:00+00:00")
        row = store.get_task(tid)
        assert row["last_status"] == "success"
        assert row["last_run_at"] == "2026-01-01T12:05:00+00:00"
        assert row["running_thread_id"] == ""   # 收口清在飞
        assert row["retry_count"] == 0
        runs = store.list_runs(tid)
        assert len(runs) == 1
        assert runs[0]["status"] == "success"
        assert runs[0]["finished_at"] == "2026-01-01T12:05:00+00:00"

    def test_complete_run_error_records_message(self, store):
        tid = store.insert_task(_row(next_run_at=PAST))
        store.claim(tid, PAST, FUTURE, NOW)
        store.set_running_thread(tid, "th-2", "run-2")
        store.record_run(tid, "th-2", "run-2", NOW)
        store.complete_run(tid, "th-2", "error", "boom", "2026-01-01T12:06:00+00:00")
        row = store.get_task(tid)
        assert row["last_status"] == "error"
        assert row["last_error"] == "boom"
        assert row["running_thread_id"] == ""

    def test_fail_fire_rolls_back(self, store):
        """fire 失败：next_run_at 回拨、清占位、retry_count 由调用方给。"""
        tid = store.insert_task(_row(next_run_at=PAST))
        store.claim(tid, PAST, FUTURE, NOW)  # 占位已置
        store.fail_fire(tid, "2026-01-01T12:05:00+00:00", "run 槽满", retry_count=1)
        row = store.get_task(tid)
        assert row["next_run_at"] == "2026-01-01T12:05:00+00:00"
        assert row["running_thread_id"] == ""
        assert row["retry_count"] == 1
        assert row["last_error"] == "run 槽满"

    def test_reschedule_sets_next_and_state(self, store):
        """非 CAS 直设 next_run_at —— pause/resume/改周期专用（认领仍必须走 claim）。"""
        tid = store.insert_task(_row(next_run_at=PAST, state="paused", enabled=0))
        store.reschedule(tid, FUTURE, state="scheduled", enabled=1)
        row = store.get_task(tid)
        assert row["next_run_at"] == FUTURE
        assert row["state"] == "scheduled"
        assert row["enabled"] == 1

    def test_list_runs_order_and_limit(self, store):
        tid = store.insert_task(_row())
        for i in range(3):
            store.record_run(tid, f"th-{i}", f"run-{i}", NOW)
        runs = store.list_runs(tid, limit=2)
        assert len(runs) == 2
        assert runs[0]["thread_id"] == "th-2"  # 新在前

    def test_purge_old_runs(self, store):
        tid = store.insert_task(_row())
        store.record_run(tid, "th-old", "r1", "2025-01-01T00:00:00+00:00")
        store.record_run(tid, "th-new", "r2", NOW)
        n = store.purge_old_runs("2026-01-01T00:00:00+00:00")
        assert n == 1
        assert [r["thread_id"] for r in store.list_runs(tid)] == ["th-new"]


class TestSingletonIsolation:
    def test_get_store_isolated_from_frozen_workspace_manager(
        self, tmp_agent_data, monkeypatch
    ):
        """全量套件顺序回归：workspace_manager 的 data_root 是 **import 期冻结** 的
        （manager.py 模块级 `_DATA_ROOT`/`_SHARED_RESOURCES_DIR`，且 `.env` 的
        AGENT_DATA_ROOT=D:\\nl2sql_data 会被 load_dotenv 带进来）。套件里任何更早的
        模块（如 test_api_smoke）先 import 它，`tmp_agent_data` 的环境变量覆盖就被
        静默忽略 ⇒ 单例 store 落进真实数据根，测试任务残留污染下一轮
        （test_list_empty 在全量套件必炸的根因，2026-10-21 实证）。
        隔离必须钉死 store 落点在 tmp 根下，与 import 顺序无关。"""
        from pathlib import Path

        import agent.workspace_manager.manager as wm

        # 模拟「更早的模块已把 workspace_manager 冻结在别处」
        monkeypatch.setattr(wm, "_SHARED_RESOURCES_DIR", Path("D:/nl2sql_data/shared"))

        from agent.scheduler.store import get_store, reset_store_for_tests

        reset_store_for_tests()
        try:
            st = get_store()
            assert st.db_path.is_relative_to(tmp_agent_data), (
                f"store 逃逸到 {st.db_path}（tmp_agent_data 隔离失效）"
            )
        finally:
            reset_store_for_tests()
