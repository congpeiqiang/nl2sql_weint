# -*- coding: utf-8 -*-
"""P2-1 优雅停机（drain）：发版时让在跑的 run 跑完，新提交明确被拒。

**为什么不能指望 langgraph 自己的排空**：inmem 运行时的排空窗口是**硬编码 5 秒**
（`langgraph_runtime_inmem/queue.py` 的 `SHUTDOWN_GRACE_PERIOD_SECS = 5`，`asyncio.wait_for`
到点即放弃），而 `langgraph_api.config.BG_JOB_SHUTDOWN_GRACE_PERIOD_SECS`（默认 180）
在本版本**没有任何读取点**（只在 config 里定义）——它属于 postgres/Go 核心那条路径。
叠加 `docker stop` 默认再给 10 秒就 SIGKILL，结果是一个跑几分钟的 run 必被腰斩：
症状是前端转圈/半轮对话丢失，而不是"发版失败"（所以很久没人把它当故障看）。

**两个入口，各管一段**：

1. **协作式（发版脚本主动调）**：`POST /api/admin/drain` → 置 draining 标志（新 run 由
   `DrainGateMiddleware` 以 503 明确拒掉，带 `Retry-After`）→ 等到在跑的后台任务清零
   （预算内）→ 脚本随后才 `docker compose stop -t <预算 + 余量>`。**这一段的"拒绝新
   请求"是真会发生的**（进程还在正常接连接）。
2. **信号式（兜底）**：`docker stop` / SIGTERM 时 uvicorn 先关监听、再跑 lifespan
   shutdown。自定义 app 的 lifespan 排在 langgraph 基础 lifespan **之前**退出
   （`langgraph_api/timing/timer.py: combine_lifespans(base, user)` + `AsyncExitStack`
   逆序），所以在 langgraph 那 5 秒窗口**之前**我们还有一次等待机会。
   ⚠️ 这一步 socket 已关，不会再有新请求进来，gate 形同虚设（不影响正确性）。
   它保证的是"即使有人直接 `docker restart`，在跑的 run 也有预算跑完"。

**计数的真源**：`langgraph_runtime_inmem.queue.get_num_workers()` = 在跑的后台任务数
（含子 agent run 与 sync 循环 —— 一次问数约 3 个，所以清零点比"run 数"更保守）。
读不到（非 inmem 运行时 / 离线）→ 视为 0 并 debug 记一笔：**没有计数就不等**
（宁可照旧硬停，也不要凭空阻塞发版）。

**预算与边界**：`NL2SQL_DRAIN_SECS`（默认 180，`0` = 不等待只拒绝新请求）。
预算用尽仍未清零 → warning 里点出**还剩几个**，然后照常退出 —— 剩下的会被 langgraph
的 5 秒窗口与 docker 的 SIGKILL 截断，这条日志就是事后归因的唯一线索。
⚠️ 部署侧必须让 `stop_grace_period > NL2SQL_DRAIN_SECS`，否则 docker 的 SIGKILL 会比
我们的预算先到，等待白做（见 `docs/weint环境/` 的 compose 补丁说明）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable

from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

_logger = logging.getLogger(__name__)

# 默认预算：与 langgraph 自己的 BG_JOB_SHUTDOWN_GRACE_PERIOD_SECS 默认值同量级。
# 上界不设死，但 compose 的 stop_grace_period 必须大于它（模块头已注明）。
_DEFAULT_BUDGET_SECS = 180
_POLL_SECS = 0.5

# ── 只拦"创建 run"的 POST ────────────────────────────────────────────
# 必须是**白名单式**正则：`/runs/cancel`、`/threads/{tid}/runs/{rid}/cancel`、
# `/runs/crons*` 都含 `/runs` 且是 POST，按前缀一刀切会把"排空期间取消自己的 run"
# 也拒掉 —— 那正好是用户最需要能用的操作。这 7 条来自
# `langgraph_api/api/runs.py` 的 ApiRoute 定义（stream/wait/batch/无后缀 × 有无 thread 前缀）。
_RUN_CREATE_RE = re.compile(
    r"^/(?:threads/[^/]+/)?runs(?:/(?:stream|wait|batch))?$"
)

_lock = threading.Lock()
_state: dict[str, Any] = {
    "draining": False,
    "started_at": 0.0,      # monotonic；0 = 未开始
    "deadline_at": 0.0,     # monotonic；0 = 未定
    "waited_secs": 0.0,     # 最近一次等待实际等了多久
    "remaining": 0,         # 最近一次读到的在跑任务数
    "drained": False,       # 是否等到了清零
    "reason": "",           # 触发来源，便于日志归因（release / lifespan / admin）
}


def budget_secs() -> int:
    """排空预算（秒）；`NL2SQL_DRAIN_SECS=0` = 只拒绝新请求、不等待。"""
    raw = (os.environ.get("NL2SQL_DRAIN_SECS") or "").strip()
    if not raw:
        return _DEFAULT_BUDGET_SECS
    try:
        return max(0, int(float(raw)))
    except ValueError:
        _logger.warning("[drain] NL2SQL_DRAIN_SECS 不是数字（%r），按默认 %s 秒", raw, _DEFAULT_BUDGET_SECS)
        return _DEFAULT_BUDGET_SECS


def active_workers() -> int:
    """在跑的 langgraph 后台任务数（含子 agent run / sync 循环）。

    读不到该计数（非 inmem 运行时、离线测试、包版本变了）→ 返回 0 并只记一次 debug：
    没有计数就不等待，避免把发版凭空拖住。
    """
    try:
        from langgraph_runtime_inmem.queue import get_num_workers

        return int(get_num_workers())
    except Exception as e:  # noqa: BLE001
        _logger.debug("[drain] 读不到后台任务计数（按 0 处理，不等待）: %s", e)
        return 0


def _effective_budget(budget: int | None) -> float:
    """本次等待的**剩余**预算（秒，浮点）。

    显式给了 `budget` 就用它；否则按 `begin_drain` 记下的截止时刻算**剩余**时间
    （协作式：脚本可能过了几秒才来查，前面耗掉的不该重复计入）。
    ⚠️ 别在这里 `int()` 取整：`int(1.0 - ε) == 0` 会把整份预算归零、等待直接跳过
    （实测：`NL2SQL_DRAIN_SECS=1` 与"进入排空后 1 秒内调用"都踩这个坑）。
    """
    if budget is not None:
        return max(0.0, float(budget))
    with _lock:
        deadline = _state["deadline_at"]
    if deadline:
        return max(0.0, deadline - time.monotonic())
    return float(budget_secs())


def is_draining() -> bool:
    with _lock:
        return bool(_state["draining"])


def begin_drain(reason: str = "", budget: int | None = None) -> bool:
    """进入排空态：此后新 run 被 503 拒掉。返回是否为**首次**进入。"""
    with _lock:
        first = not _state["draining"]
        _state["draining"] = True
        if first:
            now = time.monotonic()
            _state["started_at"] = now
            _state["deadline_at"] = now + (budget_secs() if budget is None else budget)
            _state["drained"] = False
            _state["waited_secs"] = 0.0
            _state["reason"] = reason
    if first:
        _logger.warning(
            "[drain] 进入排空态（来源=%s，预算=%ss，当前在跑 %s 个后台任务）—— 新提交将返回 503",
            reason or "unknown", budget_secs() if budget is None else budget, active_workers(),
        )
    return first


def end_drain() -> dict[str, Any]:
    """撤销排空态，恢复接单（运维改了主意 / 排空失败回退时用）。"""
    with _lock:
        was = bool(_state["draining"])
        _state["draining"] = False
        _state["deadline_at"] = 0.0
    if was:
        _logger.warning("[drain] 排空态已撤销，恢复接单")
    return drain_state()


def drain_state() -> dict[str, Any]:
    """当前排空状态快照（状态端点与日志共用）。"""
    with _lock:
        snapshot = dict(_state)
        deadline = snapshot["deadline_at"]
    snapshot["remaining"] = active_workers()
    snapshot["budget_secs"] = budget_secs()
    if deadline:
        snapshot["deadline_in_secs"] = max(0.0, round(deadline - time.monotonic(), 1))
    else:
        snapshot["deadline_in_secs"] = None
    snapshot["drained"] = bool(snapshot["drained"]) and snapshot["remaining"] == 0
    return snapshot


async def wait_for_idle(
    budget: int | None = None,
    *,
    poll_secs: float = _POLL_SECS,
    on_tick: Callable[[int, float], None] | None = None,
) -> dict[str, Any]:
    """等到在跑的后台任务清零，最多等 `budget` 秒。

    - `budget=None` → 用 `begin_drain` 那一刻定的**剩余**预算（见 `_effective_budget`）
    - 每次都**重新**读 `active_workers()`（不是采样一次就信）
    - 期间被 `end_drain()` 撤销 → 立即返回（运维可中止）
    - 到点未清零 → warning 点名剩余数，然后返回（调用方照常退出）

    返回最终快照（含 `drained` / `waited_secs` / `remaining`）。
    """
    budget = _effective_budget(budget)

    started = time.monotonic()
    remaining = active_workers()
    ticks = 0
    while remaining > 0 and (time.monotonic() - started) < budget and is_draining():
        if on_tick is not None:
            try:
                on_tick(remaining, time.monotonic() - started)
            except Exception:  # noqa: BLE001  探针不该影响等待
                pass
        await asyncio.sleep(poll_secs)
        ticks += 1
        remaining = active_workers()

    waited = time.monotonic() - started
    with _lock:
        _state["waited_secs"] = waited
        _state["remaining"] = remaining
        _state["drained"] = remaining == 0
    if remaining == 0:
        _logger.warning("[drain] 排空完成：等了 %.1fs（%d 次轮询），在跑任务已清零", waited, ticks)
    elif not is_draining():
        _logger.warning("[drain] 排空被撤销（等了 %.1fs，仍有 %d 个在跑）", waited, remaining)
    else:
        _logger.warning(
            "[drain] **排空超时**：预算 %.1fs 用尽，仍有 %d 个后台任务在跑 —— "
            "它们将被 langgraph 的 5s 窗口 / docker 的 SIGKILL 截断（这就是那批'半轮对话'的来源）",
            budget, remaining,
        )
    return drain_state()


def _drain_response() -> tuple[bytes, list[tuple[bytes, bytes]]]:
    state = drain_state()
    body = json.dumps(
        {
            "error": "服务正在停机排空（发版中），暂不接受新的查询请求，请稍后重试",
            "draining": True,
            "remaining_tasks": state["remaining"],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"retry-after", b"30"),
    ]
    return body, headers


class DrainGateMiddleware:
    """排空期间拒绝**新 run 提交**的纯 ASGI 中间件（503 + Retry-After）。

    为什么是纯 ASGI 而不是 `BaseHTTPMiddleware`：本仓已有约定（`_common.py` 文件头）
    —— BaseHTTPMiddleware 包裹整个 app 对 langgraph 的 SSE 流式路由有风险；这里也
    只读 `scope` 就决定放行/拒绝，不需要碰 body。

    只拦**新建** run 的 7 条路径（见 `_RUN_CREATE_RE`）：取消 / 查询 / 列表 / 会话操作
    一律放行 —— 排空期间用户最需要能用的恰恰是"取消我这个跑不完的查询"。

    ⚠️ 它挂在 app 级（`custom_app` 的 middleware 列表），langgraph 会把自定义 app 的
    中间件放到全局（`langgraph_api/server.py`：`app.user_middleware = custom_middleware
    + global_middleware`），所以原生 `/threads/{tid}/runs/stream` 也在拦截范围内。
    """

    def __init__(self, app: ASGIApp, **kwargs: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_draining():
            method = scope.get("method", "")
            path = scope.get("path", "")
            if method == "POST" and _RUN_CREATE_RE.match(path):
                body, headers = _drain_response()
                _logger.info("[drain] 拒绝新 run 提交: %s %s", method, path)
                await _send_plain(send, 503, headers, body)
                return
        await self.app(scope, receive, send)


async def _send_plain(send: Send, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": headers + [(b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


# ── HTTP 端点（挂进 custom_app.ROUTES）────────────────────────────────

async def drain_endpoint(request):
    """`POST /api/admin/drain`：进入排空态并等到在跑任务清零（预算内）。

    `?wait=0` 只置位不等待（脚本想自己控制节奏时用）。返回最终快照。
    """
    from api._common import json_response, require_admin

    require_admin(request)
    wait = (request.query_params.get("wait") or "1").lower() not in ("0", "false", "no")
    begin_drain(reason="admin")
    if wait:
        await wait_for_idle()
    return json_response(drain_state())


async def drain_status_endpoint(request):
    """`GET /api/admin/drain`：只看当前状态（发版脚本轮询用）。"""
    from api._common import json_response, require_admin

    require_admin(request)
    return json_response(drain_state())


async def undrain_endpoint(request):
    """`DELETE /api/admin/drain`：撤销排空态、恢复接单（运维改主意 / 排空失败回退）。"""
    from api._common import json_response, require_admin

    require_admin(request)
    return json_response(end_drain())


routes: list[Route] = [
    Route("/api/admin/drain", drain_endpoint, methods=["POST"]),
    Route("/api/admin/drain", drain_status_endpoint, methods=["GET"]),
    Route("/api/admin/drain", undrain_endpoint, methods=["DELETE"]),
]
