"""POST /api/sso/login —— 工号直取身份（2026-10-28）。

背景：运营管理平台以 iframe 嵌入会话页，iframe src 上带 `?emp=Z064`，前端调本端点
换 HttpOnly Cookie。凭证形态与手工登录**完全一致**（复用 `_set_auth_cookie`），
既有鉴权路径零改动 —— 所以这里不测「新的一套鉴权」，只测：

1. 错误响应的契约（400 格式 / 403 管理员 / 400 非 JSON）与首次登录自动建号；
2. 成功时种下的 Cookie 能被 `verify_token` 认成同一个人（即与手工登录同一条链路）；
3. 中间件白名单 —— 换凭证的请求本身不能要求凭证，漏了这一条就是 401 死循环；
4. 建号入口的工号校验（`Z64` / `z064` 这类错号只会表现为「查无此人」，没人知道为什么）。
"""
import asyncio
import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import api.sso_routes as sso
import agent.auth.token as token_mod
import agent.auth.users as users


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def sso_env(tmp_path, monkeypatch):
    """隔离用户库与 HMAC 密钥：不碰真实 auth_users.json / auth_secret 文件。"""
    data_root = tmp_path / "agent_data"
    data_root.mkdir()
    monkeypatch.setenv("AGENT_DATA_ROOT", str(data_root))
    monkeypatch.setenv("NL2SQL_AUTH_SECRET", "test-secret")
    # conftest 默认开 dev 旁路（verify_token 直接返回 dev 用户），本文件要验真链路
    monkeypatch.delenv("NL2SQL_AUTH_DISABLED", raising=False)
    monkeypatch.setattr(users, "_users_cache", None)
    monkeypatch.setattr(token_mod, "_secret_cache", None)
    yield data_root
    users._users_cache = None


@pytest.fixture
def client():
    return TestClient(Starlette(routes=list(sso.routes)))


def _make_employee(emp_no="Z064", user_id="zhangsan", is_admin=False):
    return users.add_user(
        user_id,
        "password123",
        display_name="张三",
        is_admin=is_admin,
        must_change_password=False,
        emp_no=emp_no,
    )


def _set_cookie_header(resp) -> str:
    return resp.headers.get("set-cookie", "")


def _token_of(set_cookie: str) -> str:
    for part in set_cookie.split(";"):
        part = part.strip()
        if part.startswith("nl2sql_token="):
            return part[len("nl2sql_token="):].strip('"')
    return ""


# ── 成功路径 ────────────────────────────────────────────────────────


def test_known_employee_gets_cookie(sso_env, client):
    _make_employee()
    resp = client.post("/api/sso/login", json={"emp_no": "Z064"})

    assert resp.status_code == 200
    assert resp.json()["user"]["user_id"] == "zhangsan"
    assert resp.json()["user"]["display_name"] == "张三"

    cookie = _set_cookie_header(resp)
    assert cookie.startswith("nl2sql_token=")
    low = cookie.lower()
    assert "httponly" in low, "工号换来的凭证必须 HttpOnly（同手工登录）"
    assert "samesite=lax" in low
    assert "domain=" not in low, "host-only：写 Domain 会把 token 外发给所有子域"
    assert "secure" not in low, "http 源上加 Secure 会被浏览器直接丢弃"
    assert "max-age=86400" in low


def test_cookie_verifies_as_same_user(sso_env, client):
    """种下的 Cookie 走的就是手工登录那条 `verify_token` 链路 —— 同一串凭证、同一套校验。"""
    _make_employee()
    resp = client.post("/api/sso/login", json={"emp_no": "Z064"})
    token = _token_of(_set_cookie_header(resp))
    assert token

    user = token_mod.verify_token(token)
    assert user is not None
    assert user["user_id"] == "zhangsan"
    assert user["is_admin"] is False


def test_find_by_emp_no_falls_back_to_user_id(sso_env):
    """既有账号可以不补 `emp_no` 字段，直接把工号当 user_id（免迁移）。"""
    users.add_user("Z065", "password123", display_name="李四", must_change_password=False)

    assert users.find_by_emp_no("Z065")["user_id"] == "Z065"
    assert users.find_by_emp_no("Z999") is None
    assert users.find_by_emp_no("") is None


# ── 失败分支 ────────────────────────────────────────────────────────


# ── 自动建号（工号首次登录）──────────────────────────────────────────


def test_unknown_employee_is_auto_created(sso_env, client):
    """运营平台有账号即视为有身份：nl2sql 侧不再要求预先建号。"""
    assert users.find_by_emp_no("Z051") is None

    resp = client.post("/api/sso/login", json={"emp_no": "Z051"})

    assert resp.status_code == 200
    assert resp.json()["user"]["user_id"] == "Z051"
    assert "set-cookie" in resp.headers

    record = users.find_by_emp_no("Z051")
    assert record is not None
    assert record["emp_no"] == "Z051"
    assert record["is_admin"] is False


def test_auto_created_account_has_no_usable_password(sso_env, client):
    """SSO 专用账号：随机密码没人知道 ⇒ 密码登录这条路对它等同于不存在。"""
    client.post("/api/sso/login", json={"emp_no": "Z051"})
    record = users.find_by_emp_no("Z051")

    assert record["password_hash"].startswith("pbkdf2_sha256$")
    assert record["must_change_password"] is False, (
        "免登账号没有改密动作；标记 True 遇上强制改密开关就是登录成功却处处 403"
    )
    assert users.verify_password("Z051", "") is None
    assert users.verify_password("Z051", "password123") is None


def test_second_login_reuses_the_same_account(sso_env, client):
    first = client.post("/api/sso/login", json={"emp_no": "Z051"})
    second = client.post("/api/sso/login", json={"emp_no": "Z051"})

    assert first.json()["user"]["user_id"] == second.json()["user"]["user_id"] == "Z051"
    assert len([u for u in users.load_users() if u.get("emp_no") == "Z051"]) == 1


def test_existing_account_keeps_its_own_user_id(sso_env, client):
    """已建号的工号（user_id 不是工号）登录后仍是那个账号，不再多建一个 Z064。"""
    _make_employee(emp_no="Z064", user_id="zhangsan")

    resp = client.post("/api/sso/login", json={"emp_no": "Z064"})

    assert resp.json()["user"]["user_id"] == "zhangsan"
    assert users.find_user("Z064") is None


def test_invalid_format_does_not_create_account(sso_env, client):
    """格式闸排在建号之前：错号不能被「顺手建成账号」，否则号段被垃圾值灌满。"""
    resp = client.post("/api/sso/login", json={"emp_no": "z051"})

    assert resp.status_code == 400
    assert users.find_by_emp_no("z051") is None
    assert len(users.load_users()) == 1, "不该有除默认 admin 以外的记录"


def test_admin_is_refused(sso_env, client):
    """本端点免鉴权、8080 又对外 ⇒ 管理员免登等于「知道工号即可内网提权」。"""
    _make_employee(is_admin=True)
    resp = client.post("/api/sso/login", json={"emp_no": "Z064"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "admin requires password"


@pytest.mark.parametrize("emp_no", ["z064", "Z64", "Z0641", "Z06a", "", "  "])
def test_invalid_emp_no_format_is_400(sso_env, client, emp_no):
    resp = client.post("/api/sso/login", json={"emp_no": emp_no})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid emp_no"


def test_non_json_body_is_400(sso_env, client):
    resp = client.post(
        "/api/sso/login", content=b"not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid JSON body"


# ── 中间件白名单：换凭证的请求不能要求凭证 ──────────────────────────


def _run_through_auth_mw(path: str, headers=None):
    """把请求喂给真实 AuthMiddleware，返回 (是否放行到内层 app, 状态码)。"""
    from api.auth_middleware import AuthMiddleware

    passed = {"called": False}

    async def inner(scope, receive, send):
        passed["called"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    scope = {
        "type": "http",
        "path": path,
        "headers": headers or [],
        "client": ("1.2.3.4", 12345),  # 外部来源，不走 internal 旁路
        "state": {},
    }

    async def receive():
        return {"type": "http.request", "body": b""}

    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(AuthMiddleware(inner)(scope, receive, send))
    return passed["called"], sent[0]["status"]


def test_sso_login_passes_through_without_token(sso_env):
    passed, status = _run_through_auth_mw("/api/sso/login")
    assert passed, "白名单漏了 /api/sso/login ⇒ 换凭证先 401，前端永远登不进去"
    assert status == 200


def test_other_api_still_requires_token(sso_env):
    """对照组：白名单只放这一条，别把鉴权整体放开。"""
    passed, status = _run_through_auth_mw("/api/auth/me")
    assert not passed
    assert status == 401


# ── 建号入口的工号校验 ──────────────────────────────────────────────


@pytest.mark.parametrize("emp_no", ["z064", "Z64", "Z0641", "Z 064"])
def test_add_user_rejects_bad_emp_no(sso_env, emp_no):
    with pytest.raises(ValueError):
        users.add_user("u1", "password123", emp_no=emp_no)


def test_add_user_rejects_duplicate_emp_no(sso_env):
    _make_employee()
    with pytest.raises(ValueError):
        users.add_user("lisi", "password123", emp_no="Z064")


def test_add_user_emp_no_defaults_to_no_forced_password_change(sso_env):
    """免登账号没密码可改：must_change_password=True 遇上强制改密开关就是锁死。"""
    record = _make_employee()
    assert record["must_change_password"] is False
    assert record["emp_no"] == "Z064"


def test_add_user_without_emp_no_keeps_old_default(sso_env):
    """既有行为不变：不传 emp_no 的账号仍然默认要求首登改密。"""
    record = users.add_user("wangwu", "password123")
    assert record["must_change_password"] is True
    assert "emp_no" not in record


# ── 注册 ────────────────────────────────────────────────────────────


def test_route_registered_in_custom_app():
    from api.custom_app import ROUTES

    hit = [
        r
        for r in ROUTES
        if getattr(r, "path", "") == "/api/sso/login"
        and "POST" in (getattr(r, "methods", None) or set())
    ]
    assert hit, "新端点必须挂进 custom_app.ROUTES，否则前端拿到 405/404"
