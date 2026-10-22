"""API 路由冒烟测试 — 验证所有端点注册且不炸。

不验证业务逻辑（那需要集成测试），只验证：
1. 路由注册成功（返回非 404）
2. 需要认证的端点在无 Cookie 时返回 401 或合理状态码
"""
import pytest
from starlette.testclient import TestClient


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """创建测试客户端（启动 app）。

    数据根隔离（2026-10-21 补）：lifespan 会 ``adopt_all_stores()`` + 起定时任务
    worker（tick/心跳/建库），裸跑 app 会把这些写进 ``.env`` 冻结的真实数据根
    （workspace_manager 的 data_root 是 import 期冻结，env 覆盖无效 —— 必须钉
    ``sqlite_paths.data_root``，与 test_scheduled_api fixture 同款）。冒烟测试不该
    碰真实数据根，更不该触发 adopt_legacy_store 去搬真实根上的老文件。
    """
    import os

    data_root = tmp_path_factory.mktemp("smoke_agent_data")
    for sub in ("shared", "workspace", "auth"):
        (data_root / sub).mkdir()
    os.environ["AGENT_DATA_ROOT"] = str(data_root)
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
        from agent.scheduler import worker as _sched_worker

        _sched_worker.stop_worker()
        reset_store_for_tests()
        sqlite_paths.data_root = orig_data_root
        os.environ.pop("AGENT_DATA_ROOT", None)


class TestApiSmoke:
    """所有 API 端点冒烟检查。"""

    def test_metrics_endpoint(self, client):
        resp = client.get("/metrics")
        assert resp.status_code in (200, 404)  # nginx 经 404 是已知行为

    def test_deployment_info_requires_auth(self, client):
        resp = client.get("/api/deployment-info")
        assert resp.status_code in (401, 200)  # dev 模式可能 200

    def test_db_config_list(self, client):
        resp = client.get("/api/db-configs")
        assert resp.status_code in (200, 401)

    def test_model_config_list(self, client):
        resp = client.get("/api/model-configs")
        assert resp.status_code in (200, 401)

    def test_thread_search(self, client):
        resp = client.get("/api/threads/fts?q=test")
        assert resp.status_code in (200, 401)

    def test_login_endpoint_exists(self, client):
        resp = client.post("/api/auth/login", json={"username": "test", "password": "wrong"})
        assert resp.status_code in (401, 400, 422)  # 拒绝但端点存在

    def test_auth_me_endpoint(self, client):
        resp = client.get("/api/auth/me")
        assert resp.status_code in (200, 401)

    def test_drain_status(self, client):
        resp = client.get("/api/admin/drain")
        assert resp.status_code in (200, 401, 403)

    def test_eval_flags(self, client):
        resp = client.get("/api/eval-flags")
        assert resp.status_code in (200, 401)
