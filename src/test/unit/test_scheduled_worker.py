"""定时任务 worker 单测（agent/scheduler/worker.py）。

覆盖面（方案 §3 调度器不变量 + §4 执行链路）：
- grace 计算（周期一半 clamp [120s, 2h]）
- due_action：grace 内保节奏补跑 / 超 grace 折叠快进但**仍跑一次**（hermes #33315）
- tick_once：fire 上限在认领前截、CAS 竞争跳过、在飞任务转 reconcile、
  FIRING 占位 lease 回收、心跳落盘、跨进程文件锁互斥
- _fire：threads.create([定时] 标题) + runs.create（_run_context_config 同款身份契约、
  db_name 必带）、触发前 grants 复验 fail-closed、失败 5min 回拨 ≤3 次后折叠回正常周期、
  once 失败不重试（预扣已耗尽）
- _reconcile：终态回写 complete_run / 在跑不动（看门狗兜底）
"""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from agent.scheduler import service, worker
from agent.scheduler.store import FIRING

TZ = "Asia/Shanghai"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-01-01T12:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    from agent.scheduler.store import ScheduledTaskStore

    return ScheduledTaskStore(tmp_path / "scheduled_tasks.sqlite")


def _row(store, **kw):
    base = dict(
        owner="u1", name="t", prompt="p", db_name="db1",
        sched_kind="interval", sched_expr="60", tz=TZ,
        next_run_at="2026-01-01T11:00:00+00:00",
    )
    base.update(kw)
    return store.insert_task(base)


class FakeClient:
    """假 langgraph_sdk client：记录调用，可注入失败/终态。"""

    def __init__(self, fail_create=None, runs_list=None):
        self.fail_create = fail_create
        self.runs_list = runs_list or []
        self.threads_created = []
        self.runs_created = []

        outer = self

        class _Threads:
            async def create(self, metadata=None):
                outer.threads_created.append(metadata or {})
                return {"thread_id": f"th-{len(outer.threads_created)}"}

        class _Runs:
            async def create(self, thread_id=None, assistant_id=None, input=None, config=None):
                if outer.fail_create:
                    raise outer.fail_create
                outer.runs_created.append(
                    {"thread_id": thread_id, "assistant_id": assistant_id,
                     "input": input, "config": config}
                )
                return {"run_id": f"run-{len(outer.runs_created)}"}

            async def list(self, thread_id=None, limit=None):
                return outer.runs_list

        self.threads = _Threads()
        self.runs = _Runs()


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(worker, "_make_client", lambda: client)
    monkeypatch.setattr(service, "recheck_db_access", lambda owner, db: True)
    return client


class TestGraceAndDueAction:
    def test_grace_clamped(self):
        assert worker.grace_seconds("interval", "1", TZ) == 120.0      # 60s/2 → 下限 120
        assert worker.grace_seconds("interval", "60", TZ) == 1800.0    # 周期一半
        assert worker.grace_seconds("cron", "0 9 * * *", TZ) == 7200.0  # 12h → 上限 2h
        assert worker.grace_seconds("once", "2099-01-01T00:00:00", TZ) == 120.0

    def test_within_grace_keeps_cadence(self):
        """迟到 10min ≤ grace 30min：下一次 = 原时间点 + 周期（保节奏），不折叠。"""
        row = {"id": "x", "sched_kind": "interval", "sched_expr": "60", "tz": TZ,
               "next_run_at": "2026-01-01T11:50:00+00:00"}
        new_next, folded = worker.due_action(row, NOW)
        assert folded is False
        assert new_next == "2026-01-01T12:50:00+00:00"

    def test_beyond_grace_folds_but_still_fires(self):
        """迟到 3h > grace：next 快进到 now 之后，folded=True —— 但仍会执行一次。"""
        row = {"id": "x", "sched_kind": "interval", "sched_expr": "60", "tz": TZ,
               "next_run_at": "2026-01-01T09:00:00+00:00"}
        new_next, folded = worker.due_action(row, NOW)
        assert folded is True
        assert new_next == "2026-01-01T13:00:00+00:00"  # next after NOW

    def test_cron_daily_beyond_grace_folds(self):
        row = {"id": "x", "sched_kind": "cron", "sched_expr": "0 9 * * *", "tz": TZ,
               "next_run_at": "2026-01-01T01:00:00+00:00"}  # 北京 09:00，迟到 3h? 11h
        new_next, folded = worker.due_action(row, NOW)
        # 迟到 11h > grace 2h → 折叠到下一次（明天北京 09:00 = UTC 01:00）
        assert folded is True
        assert new_next == "2026-01-02T01:00:00+00:00"

    def test_once_action_completes(self):
        row = {"id": "x", "sched_kind": "once", "sched_expr": "2026-01-01T19:00:00",
               "tz": TZ, "next_run_at": "2026-01-01T11:00:00+00:00"}
        new_next, folded = worker.due_action(row, NOW)
        assert new_next == ""  # once：claim 内落 completed

    def test_within_grace_next_still_past_fast_forwards(self, monkeypatch):
        """grace 下限(120s) > 周期(dev 放宽 1min)时：orig+周期仍在过去 → 持续快进到未来。"""
        monkeypatch.setenv("NL2SQL_SCHED_MIN_INTERVAL_MIN", "1")
        row = {"id": "x", "sched_kind": "interval", "sched_expr": "1", "tz": TZ,
               "next_run_at": "2026-01-01T11:59:30+00:00"}
        new_next, folded = worker.due_action(row, NOW)
        assert new_next > NOW_ISO  # 必须落在未来（不 crash-loop 追帧）


class TestTickOnce:
    def test_claims_and_submits_due(self, store, monkeypatch):
        submitted = []
        monkeypatch.setattr(worker, "submit_fire", submitted.append)
        tid = _row(store)
        out = worker.tick_once(store=store, now=NOW)
        assert out["fired"] == [tid]
        assert submitted == [tid]
        row = store.get_task(tid)
        assert row["next_run_at"] == "2026-01-01T13:00:00+00:00"  # 执行前已推进
        assert row["running_thread_id"] == FIRING

    def test_fire_cap_before_claim(self, store, monkeypatch):
        """上限外的 due 行**不认领**（保持 due，下轮自然再取）—— 零丢失。"""
        submitted = []
        monkeypatch.setattr(worker, "submit_fire", submitted.append)
        monkeypatch.setattr(worker, "FIRE_PER_TICK", 2)
        ids = [_row(store, next_run_at=f"2026-01-01T11:0{i}:00+00:00") for i in range(5)]
        out = worker.tick_once(store=store, now=NOW)
        assert out["fired"] == ids[:2]
        still_due = store.due_tasks(NOW_ISO)
        assert {r["id"] for r in still_due} == set(ids[2:])
        assert all(r["running_thread_id"] == "" for r in still_due)

    def test_cas_race_skipped(self, store, monkeypatch):
        """手动 run 抢先认领 → tick 的 CAS 失败，不重复提交。"""
        submitted = []
        monkeypatch.setattr(worker, "submit_fire", submitted.append)
        tid = _row(store)
        # 竞争者用同一个 expected 先认领成功
        assert store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        out = worker.tick_once(store=store, now=NOW)
        assert out["fired"] == []
        assert submitted == []

    def test_inflight_goes_to_reconcile(self, store, monkeypatch):
        submitted, reconciles = [], []
        monkeypatch.setattr(worker, "submit_fire", submitted.append)
        monkeypatch.setattr(worker, "submit_reconcile", reconciles.append)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        store.set_running_thread(tid, "th-real", "run-1")
        worker.tick_once(store=store, now=NOW)
        assert submitted == []
        assert reconciles == [tid]

    def test_firing_placeholder_stale_reclaimed(self, store, monkeypatch):
        """fire 中途进程死掉：占位超 lease → 回收（清在飞 + 记 error），任务回到可调度。"""
        monkeypatch.setattr(worker, "submit_fire", lambda tid: None)
        monkeypatch.setattr(worker, "submit_reconcile", lambda tid: None)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        fresh = store.get_task(tid)
        assert fresh["running_thread_id"] == FIRING
        worker.tick_once(store=store, now=NOW)
        assert store.get_task(tid)["running_thread_id"] == FIRING  # lease 内不动
        # 伪造超龄 updated_at
        old = (NOW - timedelta(seconds=worker.FIRING_LEASE_S + 60)).isoformat(timespec="seconds")
        store.update_task(tid, {"updated_at": old})
        worker.tick_once(store=store, now=NOW)
        row = store.get_task(tid)
        assert row["running_thread_id"] == ""
        assert "中断" in row["last_error"]

    def test_heartbeat_written(self, store, monkeypatch):
        monkeypatch.setattr(worker, "submit_fire", lambda tid: None)
        _row(store)
        worker.tick_once(store=store, now=NOW)
        hb = store.db_path.parent / "heartbeat"
        assert hb.exists()
        data = json.loads(hb.read_text(encoding="utf-8"))
        assert data["last_tick_at"].endswith("+00:00")

    def test_tick_lock_mutual_exclusion(self, store, tmp_path):
        lock_path = tmp_path / ".tick.lock"
        h1 = worker.acquire_tick_lock(lock_path)
        assert h1 is not None
        h2 = worker.acquire_tick_lock(lock_path)
        assert h2 is None  # 蓝绿双色/双进程：同一时刻只有一边 tick
        worker.release_tick_lock(h1)
        h3 = worker.acquire_tick_lock(lock_path)
        assert h3 is not None
        worker.release_tick_lock(h3)


class TestFire:
    def test_fire_creates_thread_and_run_with_identity(self, store, fake_client):
        tid = _row(store, name="每日工时", llm_route="weint", llm_model="qwen3.8-max")
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        asyncio.run(worker._fire(tid, store=store, now=NOW))
        # 线程：[定时] 标题 + scheduled_task_id（会话列表可见、可回溯）
        meta = fake_client.threads_created[0]
        assert meta["title"] == "[定时] 每日工时"
        assert meta["scheduled_task_id"] == tid
        # run：_run_context_config 逐字同款契约（db_name 必带，pinned 追加）
        rc = fake_client.runs_created[0]
        assert rc["assistant_id"] == "chat_agent"
        cfg = rc["config"]
        assert cfg["recursion_limit"] == 500
        assert cfg["configurable"]["langgraph_auth_user_id"] == "u1"
        assert cfg["configurable"]["user_id"] == "u1"
        assert cfg["configurable"]["db_name"] == "db1"
        assert cfg["configurable"]["llm_route"] == "weint"
        assert cfg["configurable"]["llm_model"] == "qwen3.8-max"
        # 定时 run 标记：主 agent 的 HITL 闸门据此跳过审批（后台 interrupt 无卡可点）
        assert cfg["configurable"]["scheduled_run"] is True
        assert "[定时任务]" in rc["input"]["messages"][0]["content"]
        assert "每日工时" in rc["input"]["messages"][0]["content"]
        # 落库：真实 thread 顶掉占位 + task_runs 流水
        row = store.get_task(tid)
        assert row["running_thread_id"] == "th-1"
        runs = store.list_runs(tid)
        assert runs[0]["status"] == "running" and runs[0]["run_id"] == "run-1"

    def test_fire_unpinned_omits_llm_keys(self, store, fake_client):
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        asyncio.run(worker._fire(tid, store=store, now=NOW))
        cfg = fake_client.runs_created[0]["config"]["configurable"]
        assert "llm_route" not in cfg and "llm_model" not in cfg

    def test_fire_passes_enable_thinking_to_rebuild_owner_model(self, store, fake_client):
        """必须带 enable_thinking，否则 ThinkingToggle 不重建模型。

        缺这个键时 `_maybe_swap` 走 `return request`，模型仍是 import 期建的
        模块级 `deepseek_model`（`create_model()` 不带 user_id ⇒ 读**全局** store）
        ⇒ 定时任务用别的账号的接入点/key，owner 配的 provider 被完全忽略；
        直连 DeepSeek 思考模式还会因回放消息缺 reasoning_content 直接 400
        （2026-10-27 生产实证）。值取 false：回放只剩写报告，不需要思考。
        """
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        asyncio.run(worker._fire(tid, store=store, now=NOW))
        cfg = fake_client.runs_created[0]["config"]["configurable"]
        assert cfg["enable_thinking"] == "false"

    def test_fire_db_revoked_fail_closed(self, store, fake_client, monkeypatch):
        """触发前 grants 复验不过 → 不建线程不发 run，记 error 排下一周期。"""
        monkeypatch.setattr(service, "recheck_db_access", lambda owner, db: False)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        asyncio.run(worker._fire(tid, store=store, now=NOW))
        assert fake_client.threads_created == []
        row = store.get_task(tid)
        assert row["running_thread_id"] == ""
        assert "无权" in row["last_error"]
        assert row["next_run_at"] == "2026-01-01T13:00:00+00:00"  # 原周期，不回拨

    def test_fire_failure_rolls_back_5min(self, store, monkeypatch):
        """runs.create 抛错（排空 503/槽满）→ now+5min 回拨，retry_count+1。"""
        client = FakeClient(fail_create=RuntimeError("503 draining"))
        monkeypatch.setattr(worker, "_make_client", lambda: client)
        monkeypatch.setattr(service, "recheck_db_access", lambda o, d: True)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        asyncio.run(worker._fire(tid, store=store, now=NOW))
        row = store.get_task(tid)
        assert row["next_run_at"] == "2026-01-01T12:05:00+00:00"
        assert row["retry_count"] == 1
        assert "503" in row["last_error"]
        assert row["running_thread_id"] == ""

    def test_fire_retry_exhausted_folds_to_cycle(self, store, monkeypatch):
        client = FakeClient(fail_create=RuntimeError("boom"))
        monkeypatch.setattr(worker, "_make_client", lambda: client)
        monkeypatch.setattr(service, "recheck_db_access", lambda o, d: True)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        store.update_task(tid, {"retry_count": worker.MAX_FIRE_RETRY})
        asyncio.run(worker._fire(tid, store=store, now=NOW))
        row = store.get_task(tid)
        assert row["retry_count"] == 0
        # 折叠回正常周期：next_run_after(now) = 13:00（claim 时已是 13:00，保持）
        assert row["next_run_at"] == "2026-01-01T13:00:00+00:00"
        assert "boom" in row["last_error"]

    def test_retry_exhaustion_through_claim_path(self, store, monkeypatch):
        """贯通（review I-1）：tick claim → fire 失败 ×4 —— claim 不得清零 retry_count。

        此前 claim 的 UPDATE 带 retry_count=0，回拨重试走的正是 due→claim→fire 通路，
        计数永远在 0→1 震荡 ⇒ MAX_FIRE_RETRY 的折叠路径生产不可达，永久性失败
        （assistant 配错/目标 API 恒 4xx）每 5 分钟无限重试。预置 retry_count 的
        单点测试恰好绕过 claim，掩盖了这个缺陷 —— 本测试走完整通路。
        """
        client = FakeClient(fail_create=RuntimeError("boom"))
        monkeypatch.setattr(worker, "_make_client", lambda: client)
        monkeypatch.setattr(service, "recheck_db_access", lambda o, d: True)
        monkeypatch.setattr(worker, "submit_fire", lambda tid: None)
        tid = _row(store)  # interval 60min，next 11:00
        now = NOW
        counts = []
        for _ in range(4):
            out = worker.tick_once(store=store, now=now)
            assert out["fired"] == [tid]
            asyncio.run(worker._fire(tid, store=store, now=now, client=client))
            row = store.get_task(tid)
            counts.append(row["retry_count"])
            now = datetime.fromisoformat(row["next_run_at"]) + timedelta(seconds=1)
        # 失败计数必须跨 claim 累积：1→2→3→耗尽折叠归 0
        assert counts == [1, 2, 3, 0]
        row = store.get_task(tid)
        # 第 4 次失败 retry_count=4 > 3 → 折叠回正常周期（now+60min），不再 +5min 回拨
        assert row["next_run_at"] == "2026-01-01T13:15:03+00:00"
        assert "boom" in row["last_error"]

    def test_fire_once_failure_no_retry(self, store, monkeypatch):
        """once 预扣已在 claim 时耗尽 → fire 失败不重试，state 保持 completed。"""
        client = FakeClient(fail_create=RuntimeError("boom"))
        monkeypatch.setattr(worker, "_make_client", lambda: client)
        monkeypatch.setattr(service, "recheck_db_access", lambda o, d: True)
        tid = _row(store, sched_kind="once", sched_expr="2026-01-01T19:00:00")
        store.claim(tid, "2026-01-01T11:00:00+00:00", "", NOW_ISO)
        asyncio.run(worker._fire(tid, store=store, now=NOW))
        row = store.get_task(tid)
        assert row["state"] == "completed"
        assert row["next_run_at"] == ""
        assert row["retry_count"] == 0
        assert "boom" in row["last_error"]

    def test_fire_deleted_task_noop(self, store, fake_client):
        asyncio.run(worker._fire("gone", store=store, now=NOW))
        assert fake_client.threads_created == []


class TestReconcile:
    def test_terminal_run_written_back(self, store, monkeypatch):
        client = FakeClient(runs_list=[
            {"run_id": "run-1", "status": "success", "updated_at": "2026-01-01T12:10:00+00:00"},
        ])
        monkeypatch.setattr(worker, "_make_client", lambda: client)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        store.set_running_thread(tid, "th-1", "run-1")
        store.record_run(tid, "th-1", "run-1", NOW_ISO)
        asyncio.run(worker._reconcile_task(tid, store=store, now=NOW))
        row = store.get_task(tid)
        assert row["last_status"] == "success"
        assert row["running_thread_id"] == ""
        assert store.list_runs(tid)[0]["status"] == "success"

    def test_error_terminal_records_status(self, store, monkeypatch):
        client = FakeClient(runs_list=[
            {"run_id": "run-1", "status": "timeout", "updated_at": "2026-01-01T12:10:00+00:00"},
        ])
        monkeypatch.setattr(worker, "_make_client", lambda: client)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        store.set_running_thread(tid, "th-1", "run-1")
        store.record_run(tid, "th-1", "run-1", NOW_ISO)
        asyncio.run(worker._reconcile_task(tid, store=store, now=NOW))
        assert store.get_task(tid)["last_status"] == "timeout"

    def test_running_untouched(self, store, monkeypatch):
        """run 还在跑 → 不收口（挂死由既有看门狗打终态，下轮 reconcile 照常收）。"""
        client = FakeClient(runs_list=[{"run_id": "run-1", "status": "running"}])
        monkeypatch.setattr(worker, "_make_client", lambda: client)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        store.set_running_thread(tid, "th-1", "run-1")
        asyncio.run(worker._reconcile_task(tid, store=store, now=NOW))
        row = store.get_task(tid)
        assert row["running_thread_id"] == "th-1"
        assert row["last_status"] == ""

    def test_reconcile_list_failure_keeps_inflight(self, store, monkeypatch):
        """runs.list 本身炸（API 抖动）→ 保持原状，下轮再收。"""
        class Boom(FakeClient):
            pass

        client = Boom()

        async def _boom(thread_id=None, limit=None):
            raise RuntimeError("api down")

        client.runs.list = _boom
        monkeypatch.setattr(worker, "_make_client", lambda: client)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        store.set_running_thread(tid, "th-1", "run-1")
        asyncio.run(worker._reconcile_task(tid, store=store, now=NOW))
        assert store.get_task(tid)["running_thread_id"] == "th-1"

    # ── C-1：真实 thread 在飞但 run 已丢失（重启清 inmem / thread 被删）的 give-up lease ──
    # 此前 reconcile 对空 runs / 404 / 非终态一律 return，due 选取又排除在飞行，
    # FIRING lease 只认占位符 —— 任务永久卡死。超龄（> 看门狗绝对上限 + 余量）必须收口。

    def _stale_updated_at(self, store, tid):
        old = (NOW - timedelta(seconds=worker.RECONCILE_GIVEUP_S + 60)).isoformat(timespec="seconds")
        store.update_task(tid, {"updated_at": old})

    def test_reconcile_lost_run_stale_gives_up(self, store, monkeypatch):
        """run 记录不存在（重启丢失）+ 在飞超龄 → 按 interrupted 收口，任务回到可调度。"""
        client = FakeClient(runs_list=[])
        monkeypatch.setattr(worker, "_make_client", lambda: client)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        store.set_running_thread(tid, "th-1", "run-1")
        store.record_run(tid, "th-1", "run-1", NOW_ISO)
        self._stale_updated_at(store, tid)
        asyncio.run(worker._reconcile_task(tid, store=store, now=NOW))
        row = store.get_task(tid)
        assert row["running_thread_id"] == ""
        assert row["last_status"] == "interrupted"
        assert "丢失" in row["last_error"]
        assert store.list_runs(tid)[0]["status"] == "interrupted"

    def test_reconcile_lost_run_fresh_waits(self, store, monkeypatch):
        """run 记录不存在但**未超龄**（可能刚 fire / 蓝绿另一色记录）→ 不收口，等下轮。"""
        client = FakeClient(runs_list=[])
        monkeypatch.setattr(worker, "_make_client", lambda: client)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        store.set_running_thread(tid, "th-1", "run-1")
        store.update_task(tid, {"updated_at": NOW_ISO})  # age=0
        asyncio.run(worker._reconcile_task(tid, store=store, now=NOW))
        assert store.get_task(tid)["running_thread_id"] == "th-1"

    def test_reconcile_list_error_stale_gives_up(self, store, monkeypatch):
        """runs.list 抛错（404 / thread 被删）+ 超龄 → 收口，不再无限 return。"""
        client = FakeClient()

        async def _boom(thread_id=None, limit=None):
            raise RuntimeError("404 thread not found")

        client.runs.list = _boom
        monkeypatch.setattr(worker, "_make_client", lambda: client)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        store.set_running_thread(tid, "th-1", "run-1")
        self._stale_updated_at(store, tid)
        asyncio.run(worker._reconcile_task(tid, store=store, now=NOW))
        assert store.get_task(tid)["running_thread_id"] == ""

    def test_reconcile_thread_deleted_gives_up_immediately(self, store, monkeypatch):
        """会话被用户删（runs.list 抛 NotFoundError）⇒ **立刻**收口，不等在飞超龄。

        语义：删会话只取消「这一次」run，下次定时照跑。而 `due_tasks` 只选
        `running_thread_id=''` 的行 —— 不收口的话 20 分钟周期的任务要连跳三轮
        （等 RECONCILE_GIVEUP_S=70min 超龄），恰恰违反这个语义。
        """

        class NotFoundError(Exception):
            pass

        client = FakeClient()

        async def _gone(thread_id=None, limit=None):
            raise NotFoundError(f"thread {thread_id} not found")

        client.runs.list = _gone
        monkeypatch.setattr(worker, "_make_client", lambda: client)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        store.set_running_thread(tid, "th-1", "run-1")
        store.record_run(tid, "th-1", "run-1", NOW_ISO)
        store.update_task(tid, {"updated_at": NOW_ISO})  # 新鲜：按超龄判据本会 return
        asyncio.run(worker._reconcile_task(tid, store=store, now=NOW))
        row = store.get_task(tid)
        assert row["running_thread_id"] == "", "线程被删必须立刻收口，否则后续定时轮次全被跳过"
        assert row["last_status"] == "interrupted"
        assert store.list_runs(tid)[0]["status"] == "interrupted"

    def test_reconcile_nonterminal_stale_gives_up(self, store, monkeypatch):
        """run 长期非终态（看门狗也失灵）+ 超龄 → 按 interrupted 收口，防永久卡死。"""
        client = FakeClient(runs_list=[{"run_id": "run-1", "status": "running"}])
        monkeypatch.setattr(worker, "_make_client", lambda: client)
        tid = _row(store)
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        store.set_running_thread(tid, "th-1", "run-1")
        self._stale_updated_at(store, tid)
        asyncio.run(worker._reconcile_task(tid, store=store, now=NOW))
        row = store.get_task(tid)
        assert row["running_thread_id"] == ""
        assert row["last_status"] == "interrupted"


class TestRetentionPurge:
    """review I-4：task_runs 90 天清理必须挂进 tick（purge_old_runs 不能是死代码）。"""

    def test_tick_purges_old_runs(self, store, monkeypatch):
        monkeypatch.setattr(worker, "submit_fire", lambda tid: None)
        monkeypatch.setattr(worker, "_last_purge_iso", "")
        tid = _row(store)
        old_fired = (NOW - timedelta(days=worker.RUNS_RETENTION_DAYS + 1)).isoformat(timespec="seconds")
        store.record_run(tid, "th-old", "run-old", old_fired)
        store.record_run(tid, "th-new", "run-new", NOW_ISO)
        out = worker.tick_once(store=store, now=NOW)
        assert out["purged"] == 1
        assert [r["thread_id"] for r in store.list_runs(tid)] == ["th-new"]
        assert worker._last_purge_iso != ""

    def test_purge_respects_interval(self, store, monkeypatch):
        """刚清过（< PURGE_INTERVAL_S）→ 本轮跳过，不每 tick 全表 DELETE。"""
        monkeypatch.setattr(worker, "submit_fire", lambda tid: None)
        monkeypatch.setattr(worker, "_last_purge_iso", NOW_ISO)
        tid = _row(store)
        store.record_run(
            tid, "th-old", "r", (NOW - timedelta(days=200)).isoformat(timespec="seconds")
        )
        out = worker.tick_once(store=store, now=NOW + timedelta(seconds=60))
        assert out["purged"] == 0
        assert len(store.list_runs(tid)) == 1


class TestLifecycle:
    def test_start_stop_idempotent(self, tmp_agent_data):
        from agent.scheduler.store import reset_store_for_tests

        reset_store_for_tests()
        try:
            worker.start_worker()
            worker.start_worker()
            assert worker.worker_alive() is True
            worker.stop_worker()
            worker.stop_worker()
            assert worker.worker_alive() is False
        finally:
            reset_store_for_tests()

    def test_submit_fire_enqueues(self, monkeypatch):
        # 换新队列再断言：全局 _QUEUE 若被某个残留的 sched-exec 线程消费（全量套件
        # 顺序 + 停机竞态曾让它偶发吃掉投递项），本测试不得再受其影响。
        import queue as _queue_mod

        q = _queue_mod.Queue()
        monkeypatch.setattr(worker, "_QUEUE", q)
        worker.submit_fire("t1")
        item = q.get_nowait()
        assert item == ("fire", "t1")

    def test_metrics_snapshot_keys(self, store, monkeypatch):
        monkeypatch.setattr(worker, "submit_fire", lambda tid: None)
        _row(store)
        worker.tick_once(store=store, now=NOW)
        snap = worker.metrics_snapshot(store=store)
        assert snap["tasks_enabled"] == 1
        assert snap["last_tick_at"].endswith("+00:00")
        assert snap["fires_ok"] >= 1


def _async(v):
    async def f(*a, **k):
        return v

    return f


class TestFirePinnedReplay:
    """`_fire` 的回放接线（计划 T21）。

    这三行（`pinned_replay=True` / `input=replay_input` / `record_run(pin_mode=…)`）
    是「模型不许重新取数」这条硬闸的唯一实现：被删掉不会报错，只会让回放退化成
    「再查一遍」。所以必须从 `_fire` 这一层钉住，而不只是测 `_try_pinned_fire`。
    """

    SPEC = json.dumps(
        {
            "v": 1,
            "channel": "sql",
            "db_name": "db1",
            "tz": TZ,
            "sql_template": "SELECT 1",
            "params": [],
            "expect_columns": ["a"],
            "row_limit": 5000,
            "semantic_version": "abc",
            "warnings": [],
        }
    )

    def _pinned(self, store):
        tid = _row(store)
        store.set_pin(
            tid,
            spec_json=self.SPEC,
            semantic_version="abc",
            source_thread_id="th",
            now_iso=NOW_ISO,
        )
        store.claim(tid, "2026-01-01T11:00:00+00:00", "2026-01-01T13:00:00+00:00", NOW_ISO)
        return tid

    def test_fire_pinned_sets_replay_flag_and_tool_input(
        self, store, fake_client, monkeypatch
    ):
        monkeypatch.setattr(worker.pin_service, "semantic_version_of", _async("abc"))

        class _Res:
            sql, cube_query, cube_args, cube_tool = "SELECT 1", "", {}, ""
            columns, rows, row_count, truncated = ["a"], [{"a": 1}], 1, False
            params = {}

        monkeypatch.setattr(worker, "run_pinned_spec", _async(_Res()))
        tid = self._pinned(store)
        asyncio.run(worker._fire(tid, store=store, now=NOW))

        rc = fake_client.runs_created[0]
        assert rc["config"]["configurable"]["pinned_replay"] is True
        # 回放载荷 = user + assistant(tool_calls) + tool 三段，模型看到的“结果”在最后
        assert rc["input"]["messages"][-1]["role"] == "tool"
        assert rc["input"]["messages"][-1]["name"] == "check_async_task"
        assert store.list_runs(tid)[0]["pin_mode"] == "replay"

    def test_fire_version_mismatch_uses_normal_input(
        self, store, fake_client, monkeypatch
    ):
        monkeypatch.setattr(worker.pin_service, "semantic_version_of", _async("zzz"))
        tid = self._pinned(store)
        asyncio.run(worker._fire(tid, store=store, now=NOW))

        rc = fake_client.runs_created[0]
        assert "pinned_replay" not in rc["config"]["configurable"]
        assert rc["input"]["messages"][-1]["role"] == "user"
        run = store.list_runs(tid)[0]
        assert run["pin_mode"] == "fallback"
        assert run["pin_reason"].startswith("semantic_version_changed")
        assert store.get_task(tid)["pin_status"] == "invalid"
