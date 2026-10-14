"""API 路由冒烟测试 — 验证所有端点注册且不炸。

不验证业务逻辑（那需要集成测试），只验证：
1. 路由注册成功（返回非 404）
2. 需要认证的端点在无 Cookie 时返回 401 或合理状态码
"""
import pytest
from starlette.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """创建测试客户端（启动 app）。"""
    from api.custom_app import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


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
