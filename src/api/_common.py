"""自定义 API 共享工具（挂进 langgraph API 的自定义 app 用）。

替代原 db-config 独立服务的全局 `_JSONBodyMiddleware`：JSON body 解析内联到
handler，避免 BaseHTTPMiddleware 包裹整个 app（对 langgraph SSE 流式路由有风险）。

P1 扩展：请求级 auth helper（require_user / require_admin / require_db / require_thread）。
"""
from __future__ import annotations

from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse


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
