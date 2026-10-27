"""DELETE /api/threads/{thread_id} —— 删会话 = 取消这一次 run + 删注册表（2026-10-22）。

背景：前端原来直接调 langgraph SDK 的 `client.threads.delete()`，而
`langgraph_api/api/threads.py:delete_thread` **只删注册表行，不 cancel 任何 run**
（实读源码 + dev-34 实测：03:30:21 删会话，03:30:46 主 run 还派发了一个子任务，
sync watcher 随后 4 分钟刷了 6682 行 404）。删会话的语义应该是：

1. 取消这次在飞的 run（主 run + async_tasks 里在跑的子 run，后者占着全局 10 个 run 槽）
2. 再删会话
3. **不影响下一次定时触发**（`next_run_at` 早已在 claim 时推进）

测试用假 httpx 客户端，只验控制流/顺序/契约（错误必须「返回」不能「抛」——
前端只认 `.error`）。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.exceptions import HTTPException

import api.thread_delete as td

MAIN = "01a161a8-c7cb-78e1-ac8c-7b1297734316"
SUB = "01a161a9-4856-78e1-ac8c-7b1297734317"
DONE_SUB = "01a161a9-4856-78e1-ac8c-7b1297734399"


class FakeRequest:
    def __init__(self, thread_id=MAIN, cookie="sid=1"):
        self.path_params = {"thread_id": thread_id}
        self.headers = {"cookie": cookie}
        self.state = SimpleNamespace(user={"user_id": "u1"})


class FakeResp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data
        self.text = ""

    def json(self):
        return self._data


class FakeHttp:
    """记录 (method, url) 调用序列；按 url 后缀返回预置响应。"""

    def __init__(self, runs=None, state=None, delete_status=204):
        self.calls = []
        self.runs = runs if runs is not None else []
        self.state = state if state is not None else {"values": {}}
        self.delete_status = delete_status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append(("GET", url))
        if url.endswith("/state"):
            return FakeResp(200, self.state)
        if url.endswith("/runs"):
            return FakeResp(200, self.runs)
        return FakeResp(404)

    async def post(self, url, params=None, headers=None, json=None):
        self.calls.append(("POST", url))
        return FakeResp(200, {})

    async def delete(self, url, headers=None):
        self.calls.append(("DELETE", url))
        return FakeResp(self.delete_status)


def _patch(monkeypatch, http, require_ok=True):
    def _factory(*a, **kw):
        return http

    monkeypatch.setattr(td.httpx, "AsyncClient", _factory)
    if require_ok:
        monkeypatch.setattr(td, "require_thread", lambda request, thread_id: {"user_id": "u1"})
    else:

        def _deny(request, thread_id):
            raise HTTPException(status_code=403, detail="无权访问该会话")

        monkeypatch.setattr(td, "require_thread", _deny)


def _run(handler, request):
    return asyncio.run(handler(request))


def _body(resp):
    return json.loads(resp.body)


# ── 契约：校验 / 权限 ────────────────────────────────────────────────


def test_invalid_uuid_returns_400(monkeypatch):
    http = FakeHttp()
    _patch(monkeypatch, http)
    resp = _run(td.delete_thread, FakeRequest(thread_id="not-a-uuid"))
    assert resp.status_code == 400
    assert resp.body  # 有 error 键
    assert http.calls == [], "非法 ID 不该发起任何后端调用"


def test_not_owner_returns_403_with_error_key(monkeypatch):
    """403 必须**返回**带 `error` 的 JSON（抛 HTTPException 会被 langgraph 的处理器
    包成 `{"detail":...}`，前端只认 `.error`，用户看到的是「HTTP 403」）。"""
    http = FakeHttp()
    _patch(monkeypatch, http, require_ok=False)
    resp = _run(td.delete_thread, FakeRequest())
    assert resp.status_code == 403
    assert resp.body
    assert http.calls == []


# ── 主路径：先取消在飞 run，再删会话 ────────────────────────────────


def test_cancels_active_runs_then_deletes(monkeypatch):
    http = FakeHttp(
        runs=[
            {"run_id": "run-main", "status": "running"},
            {"run_id": "run-done", "status": "success"},
        ],
        state={
            "values": {
                "async_tasks": {
                    SUB: {"status": "running", "run_id": "run-sub"},
                    DONE_SUB: {"status": "success", "run_id": "run-sub-done"},
                }
            }
        },
    )
    _patch(monkeypatch, http)
    resp = _run(td.delete_thread, FakeRequest())
    assert resp.status_code == 200
    body = _body(resp)
    assert body["ok"] is True

    methods = [m for m, _ in http.calls]
    assert methods[-1] == "DELETE", "删会话必须是最后一步：先删了 run 就 cancel 不掉了"
    cancelled_urls = [u for m, u in http.calls if m == "POST"]
    assert any("run-main/cancel" in u for u in cancelled_urls)
    assert any(f"/threads/{SUB}/runs/run-sub/cancel" in u for u in cancelled_urls)
    assert not any("run-done" in u or "run-sub-done" in u for u in cancelled_urls), (
        "已终态 run 不必 cancel"
    )
    assert http.calls[-1][1].endswith(f"/threads/{MAIN}")


def test_already_deleted_is_idempotent(monkeypatch):
    """会话已经没了（并发删/同步 watcher 已收口）⇒ 仍返回 ok，不报错。"""

    class Gone(FakeHttp):
        async def get(self, url, params=None, headers=None):
            self.calls.append(("GET", url))
            return FakeResp(404)

    http = Gone(delete_status=404)
    _patch(monkeypatch, http)
    resp = _run(td.delete_thread, FakeRequest())
    assert resp.status_code == 200
    assert _body(resp)["ok"] is True


def test_delete_failure_returns_error_not_raise(monkeypatch):
    http = FakeHttp(delete_status=500)
    _patch(monkeypatch, http)
    resp = _run(td.delete_thread, FakeRequest())
    assert resp.status_code >= 500
    assert _body(resp)["ok"] is False
    assert _body(resp).get("error")


# ── 注册 ────────────────────────────────────────────────────────────


def test_route_registered_with_delete_method():
    from api.custom_app import ROUTES

    hit = [
        r
        for r in ROUTES
        if getattr(r, "path", "") == "/api/threads/{thread_id}"
        and "DELETE" in (getattr(r, "methods", None) or set())
    ]
    assert hit, "新端点必须挂进 custom_app.ROUTES，否则前端拿到 405/404"
