"""自定义 API 共享工具（挂进 langgraph API 的自定义 app 用）。

替代原 db-config 独立服务的全局 `_JSONBodyMiddleware`：JSON body 解析内联到
handler，避免 BaseHTTPMiddleware 包裹整个 app（对 langgraph SSE 流式路由有风险）。
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse


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
