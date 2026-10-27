"""定时任务 REST API 集成冒烟（src/api/scheduled_tasks.py，经完整 app + TestClient）。

覆盖面（方案 §5）：六个端点、统一 `{"ok", "task"/"tasks"/"runs"}` / `{"ok":false,"error"}`
契约（错误必须「返回」不能「抛」—— 前端只认 `.error`）、校验 400、不存在 404、
run_now 与 tick 共用认领（在飞二次触发被拒）。
身份 = conftest 的 NL2SQL_AUTH_DISABLED dev 旁路（admin，user_id="dev"）。
"""
import os

import pytest
from starlette.testclient import TestClient


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    data_root = tmp_path_factory.mktemp("agent_data")
    for sub in ("shared", "workspace", "auth"):
        (data_root / sub).mkdir()
    os.environ["AGENT_DATA_ROOT"] = str(data_root)
    # env 不够：workspace_manager 的 data_root 在 import 期冻结（`.env` 的真实
    # AGENT_DATA_ROOT 被 load_dotenv 带进来），sqlite_paths.data_root() 又优先走它
    # ⇒ 全量套件里更早的模块先 import 时，本 fixture 的 tmp 根被静默忽略、store 逃逸
    # 到真实数据根（test_list_empty 只在全量套件炸的根因，2026-10-21 实证）。
    # 模块级 fixture 用不了 monkeypatch ⇒ 手动补丁 + finally 还原。
    import agent.utils.sqlite_paths as sqlite_paths
    from agent.scheduler.store import reset_store_for_tests

    orig_data_root = sqlite_paths.data_root
    sqlite_paths.data_root = lambda: data_root
    reset_store_for_tests()
    from api.custom_app import app

    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        # lifespan 停机应已 stop_worker；这里再兜底一次 —— 存活的 sched-exec 线程
        # 会在后续模块消费全局 _QUEUE（test_submit_fire_enqueues 曾因此偶发）。
        from agent.scheduler import worker as _sched_worker

        _sched_worker.stop_worker()
        reset_store_for_tests()
        sqlite_paths.data_root = orig_data_root
        os.environ.pop("AGENT_DATA_ROOT", None)


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


def test_list_empty(client):
    resp = client.get("/api/scheduled-tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["tasks"] == []


def test_create_and_list(client):
    resp = client.post("/api/scheduled-tasks", json=_payload())
    assert resp.status_code == 200
    task = resp.json()["task"]
    assert task["owner"] == "dev"
    assert task["state"] == "scheduled"
    assert task["next_run_at"].endswith("+00:00")
    assert task["sched_display"] == "每 60 分钟"

    listing = client.get("/api/scheduled-tasks").json()["tasks"]
    assert any(t["id"] == task["id"] for t in listing)


def test_create_validation_error_returns_not_raises(client):
    resp = client.post("/api/scheduled-tasks", json=_payload(sched_expr="1"))
    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert "分钟" in body["error"]  # 前端直接可展示


def test_create_bad_cron_400(client):
    resp = client.post("/api/scheduled-tasks", json=_payload(sched_kind="cron", sched_expr="每天九点"))
    assert resp.status_code == 400
    assert "cron" in resp.json()["error"]


def test_patch_pause_resume(client):
    tid = client.post("/api/scheduled-tasks", json=_payload(name="暂停恢复")).json()["task"]["id"]
    paused = client.patch(f"/api/scheduled-tasks/{tid}", json={"action": "pause"}).json()["task"]
    assert paused["state"] == "paused" and paused["enabled"] == 0
    resumed = client.patch(f"/api/scheduled-tasks/{tid}", json={"action": "resume"}).json()["task"]
    assert resumed["state"] == "scheduled" and resumed["enabled"] == 1
    assert resumed["next_run_at"] > paused["next_run_at"] or resumed["next_run_at"] != ""


def test_patch_update_fields(client):
    tid = client.post("/api/scheduled-tasks", json=_payload(name="改名前")).json()["task"]["id"]
    updated = client.patch(f"/api/scheduled-tasks/{tid}", json={"name": "改名后"}).json()["task"]
    assert updated["name"] == "改名后"


def test_patch_unknown_404(client):
    resp = client.patch("/api/scheduled-tasks/nonexistent", json={"name": "x"})
    assert resp.status_code == 404
    assert resp.json()["ok"] is False


def test_run_now_inflight_rejected(client, monkeypatch):
    from agent.scheduler import service

    fired = []
    monkeypatch.setattr(service, "submit_fire", fired.append)
    tid = client.post("/api/scheduled-tasks", json=_payload(name="立即运行")).json()["task"]["id"]
    out = client.post(f"/api/scheduled-tasks/{tid}/run")
    assert out.status_code == 200
    assert out.json()["task"]["running_thread_id"] != ""
    assert fired == [tid]
    # 在飞时二次触发 → 400 + .error（不抛 500）
    again = client.post(f"/api/scheduled-tasks/{tid}/run")
    assert again.status_code == 400
    assert "执行中" in again.json()["error"]
    # 收口：清在飞，避免影响后续用例
    from agent.scheduler.store import get_store

    get_store().complete_run(tid, out.json()["task"]["running_thread_id"], "success", "", "2026-01-01T12:00:00+00:00")


def test_runs_history_endpoint(client):
    tid = client.post("/api/scheduled-tasks", json=_payload(name="历史")).json()["task"]["id"]
    resp = client.get(f"/api/scheduled-tasks/{tid}/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body["runs"], list)


def test_delete_flow(client):
    tid = client.post("/api/scheduled-tasks", json=_payload(name="待删")).json()["task"]["id"]
    assert client.delete(f"/api/scheduled-tasks/{tid}").status_code == 200
    listing = client.get("/api/scheduled-tasks").json()["tasks"]
    assert all(t["id"] != tid for t in listing)
    assert client.delete(f"/api/scheduled-tasks/{tid}").status_code == 404


def test_worker_started_by_lifespan(client):
    """lifespan 已把调度 worker 拉起来（custom_app 接线生效的直接证据）。"""
    from agent.scheduler import worker

    assert worker.worker_alive() is True


def test_preview_next_runs(client):
    """创建表单实时预览（方案 §6 高级模式）：不落库、只算未来触发时间。"""
    resp = client.post(
        "/api/scheduled-tasks/preview",
        json={"sched_kind": "cron", "sched_expr": "0 9 * * *", "tz": "Asia/Shanghai", "count": 3},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert len(body["runs"]) == 3
    assert all(r.endswith("+00:00") for r in body["runs"])  # UTC ISO 口径
    assert body["runs"] == sorted(body["runs"])              # 递增
    assert "09:00" in body["sched_display"]                  # 人类可读一并返回
    # 预览不落库
    listing = client.get("/api/scheduled-tasks").json()["tasks"]
    assert all("0 9 * * *" != t.get("sched_expr") for t in listing)


def test_preview_invalid_expr_400(client):
    resp = client.post(
        "/api/scheduled-tasks/preview",
        json={"sched_kind": "cron", "sched_expr": "不是cron"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert "cron" in body["error"]
