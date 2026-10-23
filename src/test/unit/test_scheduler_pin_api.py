"""定时任务固化的三个端点（只测路由/权限/契约，业务在 pin_service 里测）。"""

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import api.scheduled_tasks as api_module
from agent.scheduler.store import ScheduledTaskStore


@pytest.fixture
def store(tmp_path):
    s = ScheduledTaskStore(db_path=tmp_path / "s.sqlite")
    yield s
    s.close()


class _Svc:
    """service 的最小替身：只提供这两个 handler 用到的函数。"""

    def __init__(self, store):
        self.store = store
        self.db_ok = True

    def get_task_for_user(self, user, task_id):
        row = self.store.get_task(task_id)
        if row is None or row["owner"] != user.get("user_id"):
            return None
        return row

    def recheck_db_access(self, owner, db_name):
        return self.db_ok


@pytest.fixture
def svc(store, monkeypatch):
    s = _Svc(store)
    monkeypatch.setattr(api_module, "service", s)
    monkeypatch.setattr(
        api_module, "get_user", lambda request: {"user_id": "u", "is_admin": False}
    )
    return s


@pytest.fixture
def client(svc):
    with TestClient(Starlette(routes=api_module.routes)) as c:
        yield c


def _mk(store, tid, owner):
    store.insert_task(
        {
            "id": tid,
            "owner": owner,
            "name": "n",
            "prompt": "p",
            "db_name": "d",
            "sched_kind": "interval",
            "sched_expr": "5",
            "created_at": "x",
            "updated_at": "x",
        }
    )
    return tid


@pytest.fixture
def seeded_task(store):
    return _mk(store, "t1", "u")


@pytest.fixture
def other_user_task(store):
    return _mk(store, "t2", "v")


def test_pin_ok(client, seeded_task, monkeypatch):
    async def fake_pin(task, **kw):
        return {
            "pin_status": "active",
            "pin": {
                "channel": "sql",
                "preview_sql": "SELECT 1",
                "params": {"month_start": "2026-10-01"},
                "warnings": [],
            },
        }

    monkeypatch.setattr(api_module.pin_service, "pin_task", fake_pin)
    r = client.post(f"/api/scheduled-tasks/{seeded_task}/pin", json={})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["pin"]["preview_sql"] == "SELECT 1"


def test_pin_other_owner_404(client, other_user_task):
    r = client.post(f"/api/scheduled-tasks/{other_user_task}/pin", json={})
    assert r.status_code == 404
    # 断言是我们自己的 404（走 `.error` 契约），而不是「路由不存在」的默认 404
    assert r.json()["error"] == "任务不存在"


def test_pin_without_db_access_403(client, seeded_task, svc, monkeypatch):
    svc.db_ok = False

    async def fake_pin(task, **kw):  # pragma: no cover - 不该被调用
        raise AssertionError("无库权限时不许执行查询")

    monkeypatch.setattr(api_module.pin_service, "pin_task", fake_pin)
    r = client.post(f"/api/scheduled-tasks/{seeded_task}/pin", json={})
    assert r.status_code == 403
    assert "无权访问" in r.json()["error"]


def test_pin_ambiguous_returns_400(client, seeded_task, monkeypatch):
    from agent.scheduler.pin_service import PinError

    async def fake_pin(task, **kw):
        raise PinError("ambiguous_date", "2026-11-01: ['month_start','today']")

    monkeypatch.setattr(api_module.pin_service, "pin_task", fake_pin)
    r = client.post(f"/api/scheduled-tasks/{seeded_task}/pin", json={})
    assert r.status_code == 400
    assert "歧义" in r.json()["error"] or "ambiguous" in r.json()["error"]


def test_unpin_clears(client, seeded_task, monkeypatch):
    called = {}

    async def fake_clear(task_id, reason, **kw):
        called.update(task_id=task_id, reason=reason)

    monkeypatch.setattr(api_module.pin_service, "clear", fake_clear)
    r = client.delete(f"/api/scheduled-tasks/{seeded_task}/pin")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert called["task_id"] == seeded_task
    assert called["reason"].startswith("user_unpin")


def test_get_pin_returns_null_when_absent(client, seeded_task):
    r = client.get(f"/api/scheduled-tasks/{seeded_task}/pin")
    assert r.status_code == 200 and r.json()["pin"] is None


def test_get_pin_returns_spec_when_active(client, store, seeded_task):
    store.set_pin(
        "t1",
        spec_json='{"v":1,"channel":"sql","params":["month_start"]}',
        semantic_version="abc",
        source_thread_id="th",
        now_iso="2026-10-23T00:00:00Z",
    )
    r = client.get(f"/api/scheduled-tasks/{seeded_task}/pin")
    pin = r.json()["pin"]
    assert pin["status"] == "active" and pin["semantic_version"] == "abc"
    assert pin["spec"]["channel"] == "sql"


def test_get_pin_other_owner_404(client, other_user_task):
    r = client.get(f"/api/scheduled-tasks/{other_user_task}/pin")
    assert r.status_code == 404
    assert r.json()["error"] == "任务不存在"
