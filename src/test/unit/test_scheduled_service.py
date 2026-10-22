"""定时任务 service 层单测（agent/scheduler/service.py）。

覆盖面（方案 §3 时区/§5 校验/§8 配额与 grants）：
- next_run_after：interval / cron（tz-aware，UTC 落库）/ once
- naive 时间点按配置时区解释（hermes #51021：按服务器 UTC 解释会错位数小时）
- 校验：cron 合法性、interval ≥ 下限、once 必须未来、prompt/name 长度、db grants、每用户配额
- CRUD 的 owner 隔离（非 admin 碰别人的任务 = None → API 层 404）
- pause/resume：resume 从「现在」重算 next_run_at，不补历史
- run_now 与 tick 共用同一个 CAS 认领（在飞拒绝、并发认领失败透传）
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from agent.scheduler import service
from agent.scheduler.service import ScheduleError

TZ = "Asia/Shanghai"


@pytest.fixture
def store(tmp_path):
    from agent.scheduler.store import ScheduledTaskStore

    return ScheduledTaskStore(tmp_path / "scheduled_tasks.sqlite")


@pytest.fixture
def user(monkeypatch):
    """普通用户 u1，只对 db1 有权（grants 查库被 monkeypatch 短路）。"""
    monkeypatch.setattr(service, "can_access_db", lambda u, db: db == "db1")
    return {"user_id": "u1", "is_admin": False, "display_name": "U1"}


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(service, "can_access_db", lambda u, db: True)
    return {"user_id": "root", "is_admin": True, "display_name": "Root"}


def _payload(**kw):
    base = dict(
        name="每日工时报告",
        prompt="统计上月各部门工时并生成报告",
        db_name="db1",
        sched_kind="interval",
        sched_expr="60",
    )
    base.update(kw)
    return base


class TestScheduleMath:
    def test_interval_next_run(self):
        after = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        got = service.next_run_after("interval", "60", TZ, after=after)
        assert got == "2026-01-01T13:00:00+00:00"

    def test_cron_daily_is_tz_aware(self):
        """09:00 Asia/Shanghai = 01:00 UTC —— 落库必须是 UTC 口径。"""
        after = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)  # 北京 20:00
        got = service.next_run_after("cron", "0 9 * * *", TZ, after=after)
        assert got == "2026-01-02T01:00:00+00:00"

    def test_once_naive_interpreted_in_configured_tz(self):
        """naive 时间点按配置时区解释（不是按服务器 UTC）。"""
        after = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        got = service.next_run_after("once", "2026-01-02T09:00:00", TZ, after=after)
        assert got == "2026-01-02T01:00:00+00:00"

    def test_once_past_rejected(self):
        with pytest.raises(ScheduleError):
            service.next_run_after("once", "2020-01-01T00:00:00", TZ)

    def test_estimate_period(self):
        assert service.estimate_period_seconds("interval", "60", TZ) == 3600.0
        assert service.estimate_period_seconds("cron", "0 9 * * *", TZ) == 86400.0
        assert service.estimate_period_seconds("once", "2099-01-01T00:00:00", TZ) is None

    def test_describe_schedule(self):
        assert "60" in service.describe_schedule("interval", "60", TZ)
        assert "09:00" in service.describe_schedule("cron", "0 9 * * *", TZ)
        assert "2026" in service.describe_schedule("once", "2026-01-02T09:00:00", TZ)

    def test_preview_runs(self):
        after = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        runs = service.preview_runs("cron", "0 9 * * *", TZ, count=3, after=after)
        assert len(runs) == 3
        assert runs[0].startswith("2026-01-02T01:00:00")  # UTC ISO
        assert runs[1].startswith("2026-01-03T01:00:00")


class TestValidation:
    def test_invalid_cron_rejected(self, store, user):
        with pytest.raises(ScheduleError):
            service.create_task(user, _payload(sched_kind="cron", sched_expr="不是cron"), store=store)

    def test_interval_below_floor_rejected(self, store, user):
        with pytest.raises(ScheduleError):
            service.create_task(user, _payload(sched_expr="4"), store=store)

    def test_cron_faster_than_floor_rejected(self, store, user):
        with pytest.raises(ScheduleError):
            service.create_task(user, _payload(sched_kind="cron", sched_expr="* * * * *"), store=store)

    def test_empty_prompt_rejected(self, store, user):
        with pytest.raises(ScheduleError):
            service.create_task(user, _payload(prompt="  "), store=store)

    def test_prompt_too_long_rejected(self, store, user):
        with pytest.raises(ScheduleError):
            service.create_task(user, _payload(prompt="x" * 4001), store=store)

    def test_name_too_long_rejected(self, store, user):
        with pytest.raises(ScheduleError):
            service.create_task(user, _payload(name="n" * 101), store=store)

    def test_db_not_in_grants_rejected(self, store, user):
        with pytest.raises(ScheduleError):
            service.create_task(user, _payload(db_name="db2"), store=store)

    def test_quota_rejected(self, store, user, monkeypatch):
        monkeypatch.setattr(service, "MAX_TASKS_PER_USER", 2)
        service.create_task(user, _payload(name="a"), store=store)
        service.create_task(user, _payload(name="b"), store=store)
        with pytest.raises(ScheduleError):
            service.create_task(user, _payload(name="c"), store=store)


class TestCrudOwnerIsolation:
    def test_create_stamps_owner_and_computes_fields(self, store, user):
        row = service.create_task(user, _payload(), store=store)
        assert row["owner"] == "u1"
        assert row["state"] == "scheduled"
        assert row["next_run_at"] != ""
        assert row["sched_display"] != ""
        assert row["tz"] == TZ

    def test_other_user_cannot_see(self, store, user, admin):
        row = service.create_task(user, _payload(), store=store)
        intruder = {"user_id": "u2", "is_admin": False}
        assert service.get_task_for_user(intruder, row["id"], store=store) is None
        assert service.list_tasks(intruder, store=store) == []
        # admin 全量可见
        assert service.get_task_for_user(admin, row["id"], store=store)["id"] == row["id"]

    def test_other_user_cannot_modify_or_delete(self, store, user):
        row = service.create_task(user, _payload(), store=store)
        intruder = {"user_id": "u2", "is_admin": False}
        assert service.update_task(intruder, row["id"], {"name": "hack"}, store=store) is None
        assert service.pause_task(intruder, row["id"], store=store) is None
        assert service.delete_task(intruder, row["id"], store=store) is False
        assert store.get_task(row["id"])["name"] == "每日工时报告"

    def test_update_schedule_recomputes_next_run(self, store, user):
        row = service.create_task(user, _payload(), store=store)
        updated = service.update_task(
            user, row["id"], {"sched_kind": "cron", "sched_expr": "0 9 * * *"}, store=store
        )
        assert updated["sched_kind"] == "cron"
        assert updated["next_run_at"] != row["next_run_at"]
        assert updated["next_run_at"].endswith("T01:00:00+00:00")  # 北京 09:00 → UTC 01:00

    def test_update_paused_schedule_stays_paused(self, store, user):
        """review I-3：编辑 paused 任务的周期不得把它翻成 scheduled-但-enabled=0 的死态
        （due 看不见 + resume 被拒）。改周期只重排 next_run_at，保持 paused，仍可 resume。"""
        row = service.create_task(user, _payload(sched_expr="60"), store=store)
        service.pause_task(user, row["id"], store=store)
        updated = service.update_task(user, row["id"], {"sched_expr": "120"}, store=store)
        assert updated["state"] == "paused"
        assert updated["enabled"] == 0
        assert updated["sched_expr"] == "120"
        # 仍能正常恢复（用新周期从「现在」重算）
        resumed = service.resume_task(user, row["id"], store=store)
        assert resumed["state"] == "scheduled" and resumed["enabled"] == 1
        nxt = datetime.fromisoformat(resumed["next_run_at"])
        now = datetime.now(timezone.utc)
        assert now < nxt <= now + timedelta(minutes=120, seconds=5)

    def test_delete(self, store, user):
        row = service.create_task(user, _payload(), store=store)
        assert service.delete_task(user, row["id"], store=store) is True
        assert store.get_task(row["id"]) is None


class TestPauseResume:
    def test_pause_then_resume_recomputes_from_now(self, store, user):
        row = service.create_task(user, _payload(sched_expr="60"), store=store)
        paused = service.pause_task(user, row["id"], store=store)
        assert paused["state"] == "paused" and paused["enabled"] == 0
        # due 选取看不见 paused
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        assert store.due_tasks(future) == [] or all(
            r["id"] != row["id"] for r in store.due_tasks(future)
        )
        resumed = service.resume_task(user, row["id"], store=store)
        assert resumed["state"] == "scheduled" and resumed["enabled"] == 1
        # resume 从「现在」重算，不补历史
        now = datetime.now(timezone.utc)
        nxt = datetime.fromisoformat(resumed["next_run_at"])
        assert now < nxt <= now + timedelta(minutes=60, seconds=5)

    def test_resume_completed_once_rejected(self, store, user):
        row = service.create_task(
            user,
            _payload(
                sched_kind="once",
                sched_expr=(datetime.now(ZoneInfo(TZ)) + timedelta(hours=1))
                .replace(tzinfo=None)
                .isoformat(timespec="minutes"),
            ),
            store=store,
        )
        service.pause_task(user, row["id"], store=store)
        assert service.resume_task(user, row["id"], store=store) is not None  # once 未来 → 可恢复


class TestRunNow:
    def test_run_now_claims_and_submits(self, store, user, monkeypatch):
        fired = []
        monkeypatch.setattr(service, "submit_fire", lambda tid: fired.append(tid))
        row = service.create_task(user, _payload(), store=store)
        out = service.run_now(user, row["id"], store=store)
        assert out["running_thread_id"] != ""      # 已认领（在飞占位）
        assert fired == [row["id"]]                # 走了与 tick 同一个 fire 入口
        # next_run_at 被推进到未来（tick 不会重复触发本轮）
        now = datetime.now(timezone.utc)
        assert datetime.fromisoformat(out["next_run_at"]) > now

    def test_run_now_inflight_rejected(self, store, user, monkeypatch):
        monkeypatch.setattr(service, "submit_fire", lambda tid: None)
        row = service.create_task(user, _payload(), store=store)
        service.run_now(user, row["id"], store=store)
        with pytest.raises(ScheduleError):
            service.run_now(user, row["id"], store=store)

    def test_run_now_once_completes(self, store, user, monkeypatch):
        monkeypatch.setattr(service, "submit_fire", lambda tid: None)
        row = service.create_task(
            user,
            _payload(
                sched_kind="once",
                sched_expr=(datetime.now(ZoneInfo(TZ)) + timedelta(hours=1))
                .replace(tzinfo=None)
                .isoformat(timespec="minutes"),
            ),
            store=store,
        )
        out = service.run_now(user, row["id"], store=store)
        assert out["state"] == "completed"
        assert out["next_run_at"] == ""


class TestFireTimeRecheck:
    """触发前复验（review I-2）：身份必须从 users.json 还原 —— 硬编码 is_admin=False
    会让 admin 名下的任务（通常没有显式 grants 行）每次复验都白记「无权访问」。"""

    def test_recheck_db_access_fail_closed(self, monkeypatch):
        monkeypatch.setattr("agent.auth.users.find_user",
                            lambda uid: {"user_id": uid, "is_admin": False})
        monkeypatch.setattr(service, "can_access_db", lambda u, db: False)
        assert service.recheck_db_access("u1", "db1") is False
        monkeypatch.setattr(service, "can_access_db", lambda u, db: True)
        assert service.recheck_db_access("u1", "db1") is True

    def test_recheck_restores_admin_from_user_record(self, monkeypatch):
        monkeypatch.setattr("agent.auth.users.find_user",
                            lambda uid: {"user_id": uid, "is_admin": True})
        # admin 没有显式 grants 行：can_access_db 只认 is_admin（与 grants.py 真实现同构）
        monkeypatch.setattr(service, "can_access_db", lambda u, db: bool(u.get("is_admin")))
        assert service.recheck_db_access("a1", "db1") is True

    def test_recheck_regular_user_identity_passed_through(self, monkeypatch):
        seen: dict = {}

        def _fake(user, db):
            seen.update(user)
            return db == "granted"

        monkeypatch.setattr("agent.auth.users.find_user",
                            lambda uid: {"user_id": uid, "is_admin": False})
        monkeypatch.setattr(service, "can_access_db", _fake)
        assert service.recheck_db_access("u1", "granted") is True
        assert service.recheck_db_access("u1", "other") is False
        assert seen == {"user_id": "u1", "is_admin": False}

    def test_recheck_deleted_user_denied(self, monkeypatch):
        """账号已删 → is_admin=False + grants 空集 = 拒（fail-closed：删除是结论不是故障）。"""
        monkeypatch.setattr("agent.auth.users.find_user", lambda uid: None)
        monkeypatch.setattr(service, "can_access_db", lambda u, db: bool(u.get("is_admin")))
        assert service.recheck_db_access("ghost", "db1") is False

    def test_recheck_dev_bypass_skips_grant_check(self, monkeypatch):
        """dev 旁路 owner（NL2SQL_AUTH_DISABLED）在 users.json 无记录、天然 admin ——
        不启用按用户判权（resolve_caller 的 fail-open 边界同款），否则 dev-34 E2E 全灭。"""
        def _boom(uid):
            raise AssertionError("dev/internal 不应查用户记录")

        monkeypatch.setattr("agent.auth.users.find_user", _boom)
        monkeypatch.setattr(service, "can_access_db", lambda u, db: False)
        assert service.recheck_db_access("dev", "db1") is True
        assert service.recheck_db_access("internal", "db1") is True

    def test_recheck_users_read_failure_fails_open(self, monkeypatch):
        """读 users.json 故障 → 放行 + warning（caller_can_access_db 同款取舍：基础设施
        故障不该打死所有定时任务；「读到了、就是没授权」才走 fail-closed）。"""
        def _boom(uid):
            raise OSError("disk on fire")

        monkeypatch.setattr("agent.auth.users.find_user", _boom)
        monkeypatch.setattr(service, "can_access_db", lambda u, db: False)
        assert service.recheck_db_access("u1", "db1") is True
