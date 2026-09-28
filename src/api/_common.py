"""自定义 API 共享工具（挂进 langgraph API 的自定义 app 用）。

替代原 db-config 独立服务的全局 `_JSONBodyMiddleware`：JSON body 解析内联到
handler，避免 BaseHTTPMiddleware 包裹整个 app（对 langgraph SSE 流式路由有风险）。

P1 扩展：请求级 auth helper（require_user / require_admin / require_db / require_thread）。
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from api.request_log import rid_headers

_logger = logging.getLogger(__name__)


# ── JSON 工具（原有）─────────────────────────────────────────

async def parse_body(request: Request) -> dict:
    """安全解析 JSON body：空 body / 非法 JSON / 非 JSON 内容 → {}。

    复刻原 `_JSONBodyMiddleware` 的容错语义（解析失败当空 body），但只在调用方
    handler 内生效，不干扰 langgraph 其他路由。
    """
    try:
        return await request.json() or {}
    except Exception:  # noqa: BLE001  无 body / 解析失败
        return {}


def json_response(data: dict, status: int = 200) -> JSONResponse:
    """统一 JSON 响应（content-type application/json）。"""
    return JSONResponse(data, status_code=status)


# ── P1 Auth Helper ──────────────────────────────────────────

def get_user(request: Request) -> dict[str, Any] | None:
    """从 request.state 获取用户信息（auth_middleware 注入）。"""
    return getattr(request.state, "user", None)


def require_user(request: Request) -> dict[str, Any]:
    """要求已登录用户，否则 401。"""
    user = get_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def require_admin(request: Request) -> dict[str, Any]:
    """要求管理员，否则 403。"""
    user = require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def require_db(request: Request, db_name: str) -> dict[str, Any]:
    """要求用户有权访问指定库，否则 403。"""
    user = require_user(request)
    from agent.auth.grants import can_access_db
    if not can_access_db(user, db_name):
        raise HTTPException(status_code=403, detail=f"无权访问数据库: {db_name}")
    return user


def require_thread(request: Request, thread_id: str) -> dict[str, Any]:
    """要求用户有权访问指定会话（仅本人 or 管理员），否则 403。"""
    user = require_user(request)
    from agent.auth.grants import owned_thread
    if not owned_thread(user, thread_id):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return user


# ── 会话归属写入（P2+ 归属隔离）──────────────────────────────

def _api_base_url() -> str:
    return (os.environ.get("LANGGRAPH_API_URL") or "http://localhost:2026").rstrip("/")


async def stamp_thread_owner(thread_id: str, owner: str) -> bool:
    """把 metadata.owner 写到线程上（内部自调用 PATCH `/threads/{tid}`）。

    为什么需要：`@auth.on.threads.create` 钩子只覆盖 `POST /threads`。子 agent 线程
    由 deepagents 走**进程内 /noauth 客户端**创建（既不过钩子、也不过 ops 授权层），
    服务端必须自己补打归属——否则前端带 Cookie 读子线程状态（`threads.getState`）
    会被归属过滤器拒掉，任务卡的进度/待办全空。

    鉴权：自调用不带 Cookie 且来自容器网络 → AuthMiddleware 标 internal →
    `_guard_thread_access` 对 internal 不过滤（`src/agent/auth/backend.py`）。

    服务端 `Threads.patch` 对 metadata 是**浅合并**（`{**old, **new}`），所以只发
    `{"owner": ...}` 就够，graph_id / title 不会丢。

    P2-2：带上调用方的 rid（`X-Request-ID`）—— 这条自调用在后端自己的 access 日志里
    应该与触发它的那次用户请求**同一枚 id**，否则一次操作在日志里断成两截。
    """
    from agent.auth.ownership import OWNER_KEY

    url = f"{_api_base_url()}/threads/{thread_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.patch(
                url, json={"metadata": {OWNER_KEY: owner}}, headers=rid_headers() or None,
            )
    except Exception as e:  # noqa: BLE001  归属补打失败不阻断主流程
        _logger.warning("[thread_owner] 补打归属失败 %s: %s", thread_id, e)
        return False
    if r.status_code >= 300:
        _logger.warning(
            "[thread_owner] 补打归属被拒 %s: HTTP %s %s",
            thread_id, r.status_code, r.text[:200],
        )
        return False
    return True
