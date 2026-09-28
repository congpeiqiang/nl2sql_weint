"""P2-2：请求级可观测性 —— request-id + 访问日志（状态码/耗时）。

三件事：
1. **request-id**：入口取 `X-Request-ID`（nginx 用 `$request_id` 注入；内部自调用
   直接透传），没有就生成一枚 12 位 hex。它放在 contextvar 里，由 `RequestIdFilter`
   注进**每一条**日志记录（业务日志也带，不只是 access 日志）；同时**回显**到响应头
   `X-Request-ID` —— 前端报错截图/浏览器 Network 面板拿到的那枚 id 可以直接 grep 后端日志。
2. **访问日志**：一行/请求，含状态码 + 首字节耗时（latency）+ 总耗时（total）+
   客户端 IP + 用户 + 会话 id（从路径里摘）。**为什么不用 uvicorn 自带的
   `access_log=True`**：它的格式里没有耗时、也拿不到 rid，且要改它只能换 formatter，
   拿不到 duration 字段。所以 `start_server.py` 里保持 `access_log=False`，由本中间件替代。
3. **跨组件**：内部 HTTP 自调用（`api._common.stamp_thread_owner`）带同一枚 rid →
   一次用户操作在「nginx → 后端 → 后端自调用」三层日志里是同一个 id（nginx 侧见
   `docker/nginx.conf` 的 `log_format`，`$request_id` 由 nginx 生成）。

安全（两条都不是装饰）：
- **不信任**入参 header：只接受 `[A-Za-z0-9._:-]{1,64}`，不合规一律丢弃并重新生成 ——
  否则调用方可以往日志里塞换行/控制字符（**日志伪造**：一行变两行，伪造成别人的请求）。
- 日志落在 `<AGENT_DATA_ROOT>/logs`，而 agent 的文件读权限只放行 `/shared/**` 与
  `/workspace/**`（`agent/settings/file_permissions.py`）→ `/logs/**` 对模型**不可读**，
  不会把别人的请求路径/用户名/会话 id 暴露给 agent。**改日志落点时别把它挪进**这两个目录。

纯 ASGI：不缓冲 body、只**读**不改 `http.response.body` 消息 → 不破坏 SSE（`/runs/stream`）。
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from contextvars import ContextVar
from typing import Any

#: 回显与透传统一用这个头名（nginx `proxy_set_header` 同名）
REQUEST_ID_HEADER = "X-Request-ID"
#: 接受的入站头（小写；nginx 优先，其它调用方用 X-Correlation-ID 也能对上）
_INBOUND_HEADERS = ("x-request-id", "x-correlation-id")
#: **白名单**字符集：宁可丢调用方的 id，也不让换行/空格/引号进日志
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
#: `/threads/<tid>/...` → 日志里带 tid 前 8 位（一次问数的主/子 run 都挂在它下面）
_THREAD_RE = re.compile(r"/threads/([A-Za-z0-9_-]{4,})")
#: 高频健康检查不记（docker healthcheck 每 30s 一次 = 每天约 2880 行噪音）
_NO_LOG_PATHS = frozenset({"/ok"})
#: 路径+query 截断长度（防超长 query 把日志刷爆）
_PATH_MAX = 300

_rid_var: ContextVar[str] = ContextVar("nl2sql_request_id", default="")

access_logger = logging.getLogger("nl2sql.access")


def new_request_id() -> str:
    """生成一枚短 id（12 位 hex，够用且好抄）。"""
    return uuid.uuid4().hex[:12]


def sanitize_request_id(raw: str | None) -> str | None:
    """校验入站 id：合法返回原值，非法返回 None（调用方应重新生成）。"""
    if not raw:
        return None
    raw = raw.strip()
    return raw if _ID_RE.match(raw) else None


def current_request_id() -> str:
    """当前请求的 id；不在请求上下文（后台任务/CLI）里返回空串。"""
    return _rid_var.get()


def rid_headers() -> dict[str, str]:
    """内部 HTTP 自调用该带的头（无请求上下文时返回空 dict）。

    新增任何「后端调自己」的 httpx 调用都请带上它 —— 否则那条调用在后端日志里
    会另起一枚 id，一次用户操作就断成两截（现有调用点：`api/_common.stamp_thread_owner`）。
    """
    rid = current_request_id()
    return {REQUEST_ID_HEADER: rid} if rid else {}


class RequestIdFilter(logging.Filter):
    """把 `rid` 注进每条日志记录，供 formatter 的 `%(request_id)s` 使用。

    挂在 **handler** 上（不是 logger）：这样所有 logger（含 langgraph 自己的）
    经同一 handler 输出时都带上 rid，无需逐个 logger 注册。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or "-"
        return True


def _headers_map(scope: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in scope.get("headers") or []:
        try:
            out[k.decode("latin-1").lower()] = v.decode("latin-1")
        except Exception:  # noqa: BLE001  畸形 header 不该影响请求
            continue
    return out


def _client_ip(scope: dict[str, Any], headers: dict[str, str]) -> str:
    """优先取 XFF 第一跳（nginx 会追加），退回 socket 对端。"""
    xff = headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "-"


def _user_of(scope: dict[str, Any]) -> str:
    """取 AuthMiddleware 注入的用户（未鉴权/白名单路径没有 → "-"）。"""
    try:
        user = (scope.get("state") or {}).get("user") or {}
        return str(user.get("user_id") or "-")
    except Exception:  # noqa: BLE001
        return "-"


def _target(scope: dict[str, Any]) -> str:
    path = scope.get("path") or ""
    qs = (scope.get("query_string") or b"").decode("latin-1")
    target = f"{path}?{qs}" if qs else path
    return target[:_PATH_MAX]


class RequestContextMiddleware:
    """纯 ASGI：分配/透传 rid、回显响应头、结束时记一行 access 日志。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            # websocket / lifespan 原样透传
            await self.app(scope, receive, send)
            return

        headers = _headers_map(scope)
        rid = None
        for name in _INBOUND_HEADERS:
            rid = sanitize_request_id(headers.get(name))
            if rid:
                break
        inbound = bool(rid)
        rid = rid or new_request_id()

        token = _rid_var.set(rid)
        started = time.perf_counter()
        marks: dict[str, Any] = {"status": None, "first_byte": None}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                marks["status"] = message.get("status")
                marks["first_byte"] = time.perf_counter() - started
                raw_headers = message.setdefault("headers", [])
                if not any(k.lower() == b"x-request-id" for k, _ in raw_headers):
                    raw_headers.append((b"x-request-id", rid.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            total = time.perf_counter() - started
            try:
                _log_access(scope, headers, rid, inbound, marks, total)
            except Exception:  # noqa: BLE001  记日志绝不能反过来搞挂请求
                pass
            _rid_var.reset(token)


def _log_access(
    scope: dict[str, Any],
    headers: dict[str, str],
    rid: str,
    inbound: bool,
    marks: dict[str, Any],
    total: float,
) -> None:
    path = scope.get("path") or ""
    if path in _NO_LOG_PATHS:
        return
    status = marks["status"] if marks["status"] is not None else "-"
    latency = marks["first_byte"]
    latency_ms = f"{latency * 1000:.1f}" if latency is not None else "-"
    line = "[access] rid=%s%s %s %s -> %s latency=%sms total=%.1fms ip=%s user=%s"
    args: list[Any] = [
        rid,
        "" if inbound else "(gen)",
        scope.get("method", "-"),
        _target(scope),
        status,
        latency_ms,
        total * 1000,
        _client_ip(scope, headers),
        _user_of(scope),
    ]
    m = _THREAD_RE.search(path)
    if m:
        line += " tid=%s"
        args.append(m.group(1)[:8])
    if status == "-":
        line += " (no response: 客户端提前断开或异常)"
    if isinstance(status, int) and status >= 500:
        access_logger.error(line, *args)
    else:
        access_logger.info(line, *args)
