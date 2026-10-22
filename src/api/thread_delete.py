"""DELETE /api/threads/{thread_id} —— 删会话（并取消这次在跑的 run）。

为什么不能直接调 SDK 的 `client.threads.delete()`：langgraph 的
`DELETE /threads/{id}`（`langgraph_api/api/threads.py:delete_thread`）**只删注册表
行，不 cancel 任何 run**。2026-10-22 dev-34 实测：03:30:21 删掉定时任务刚 fire 的
会话，03:30:46 主 run 照派子任务，子 run 继续烧全局 10 个 run 槽，sync watcher
每 0.5s 撞一次 404（4 分钟 6682 行日志）。

本端点的语义（用户拍板）：
1. 取消**这一次**在飞的 run —— 主 run + `async_tasks` 里仍在跑的子 run（子 run
   同样占槽，不取消就是孤儿）；
2. 再删会话；
3. **不影响下一次定时触发** —— `next_run_at` 在 claim 时就推进了；调度器的
   reconcile 见到 thread 404 会立刻收口 `running_thread_id`，下轮照常 fire。

错误一律「返回」不「抛」（前端只认 `.error`；抛 HTTPException 会被 langgraph 的
处理器包成 `{"detail": ...}`）。
"""
from __future__ import annotations

import logging
import re

import httpx
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import _api_base_url as _base_url, json_response, require_thread

_logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# 与 task_cancel / sync 一致的 run 终态集
_TERMINAL_STATUSES = {"success", "error", "cancelled", "timeout", "interrupted"}


def _auth_headers(request: Request) -> dict[str, str]:
    """转发原始请求的认证头（Cookie/Authorization），供内部 LangGraph API 校验。"""
    headers: dict[str, str] = {}
    cookie = request.headers.get("cookie")
    if cookie:
        headers["cookie"] = cookie
    authorization = request.headers.get("authorization")
    if authorization:
        headers["authorization"] = authorization
    return headers


async def _active_runs(
    http: httpx.AsyncClient, base: str, thread_id: str, auth_headers: dict[str, str]
) -> list[dict]:
    """主线程上仍在跑的 run（读不到就返回空——会话可能已经没了，删是幂等的）。"""
    try:
        resp = await http.get(
            f"{base}/threads/{thread_id}/runs",
            params={"limit": 50},
            headers=auth_headers,
        )
        if resp.status_code != 200:
            return []
        runs = resp.json() or []
    except Exception as e:  # noqa: BLE001
        _logger.warning("[thread_delete] 读 run 列表失败 %s: %s", thread_id, e)
        return []
    return [
        r
        for r in runs
        if isinstance(r, dict) and str(r.get("status") or "") not in _TERMINAL_STATUSES
    ]


async def _active_subtasks(
    http: httpx.AsyncClient, base: str, thread_id: str, auth_headers: dict[str, str]
) -> list[tuple[str, str]]:
    """`async_tasks` 里仍在跑的子 run（子线程 id, run_id）。"""
    try:
        resp = await http.get(f"{base}/threads/{thread_id}/state", headers=auth_headers)
        if resp.status_code != 200:
            return []
        values = (resp.json() or {}).get("values") or {}
        tasks = values.get("async_tasks") or {}
    except Exception as e:  # noqa: BLE001
        _logger.warning("[thread_delete] 读 async_tasks 失败 %s: %s", thread_id, e)
        return []
    out: list[tuple[str, str]] = []
    if not isinstance(tasks, dict):
        return out
    for key, entry in tasks.items():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status") or "") in _TERMINAL_STATUSES:
            continue
        sub_thread_id = str(entry.get("thread_id") or key or "")
        run_id = str(entry.get("run_id") or "")
        if sub_thread_id and run_id:
            out.append((sub_thread_id, run_id))
    return out


async def _cancel_run(
    http: httpx.AsyncClient,
    base: str,
    thread_id: str,
    run_id: str,
    auth_headers: dict[str, str],
) -> bool:
    """POST …/runs/{run_id}/cancel?action=interrupt。失败只记日志，不阻断删除。"""
    try:
        resp = await http.post(
            f"{base}/threads/{thread_id}/runs/{run_id}/cancel",
            params={"action": "interrupt"},
            headers=auth_headers,
        )
    except Exception as e:  # noqa: BLE001
        _logger.warning("[thread_delete] 取消 run 请求失败 %s/%s: %s", thread_id, run_id, e)
        return False
    if resp.status_code >= 400 and resp.status_code != 404:
        _logger.warning(
            "[thread_delete] 取消 run 被拒 %s/%s: HTTP %d %s",
            thread_id,
            run_id,
            resp.status_code,
            resp.text[:200],
        )
        return False
    return True


async def delete_thread(request: Request):
    thread_id = request.path_params.get("thread_id", "")
    if not _UUID_RE.match(thread_id or ""):
        return json_response({"ok": False, "error": "非法的会话 ID"}, status=400)

    try:
        require_thread(request, thread_id)
    except HTTPException as e:
        if e.status_code == 401:
            return json_response({"error": "登录已失效，无法删除会话；请重新登录后再试"}, status=401)
        return json_response({"error": "权限不足：该会话不属于当前账号，无法删除"}, status=403)

    auth_headers = _auth_headers(request)
    base = _base_url()
    cancelled: list[dict] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as http:
        # 1) 主 run
        for run in await _active_runs(http, base, thread_id, auth_headers):
            run_id = str(run.get("run_id") or "")
            if run_id and await _cancel_run(http, base, thread_id, run_id, auth_headers):
                cancelled.append({"thread_id": thread_id, "run_id": run_id})
        # 2) 子 run（占着全局 run 槽，不取消就成了孤儿）
        for sub_thread_id, run_id in await _active_subtasks(
            http, base, thread_id, auth_headers
        ):
            if await _cancel_run(http, base, sub_thread_id, run_id, auth_headers):
                cancelled.append({"thread_id": sub_thread_id, "run_id": run_id})
        # 3) 删会话（必须在 cancel 之后：删了注册表行，cancel 就 404 了）
        try:
            resp = await http.delete(f"{base}/threads/{thread_id}", headers=auth_headers)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[thread_delete] 删除会话请求失败 %s: %s", thread_id, e)
            return json_response(
                {"ok": False, "error": f"删除会话失败：{type(e).__name__}"}, status=502
            )

    if resp.status_code >= 400 and resp.status_code != 404:
        _logger.warning(
            "[thread_delete] 删除会话被拒 %s: HTTP %d %s",
            thread_id,
            resp.status_code,
            resp.text[:200],
        )
        return json_response(
            {"ok": False, "error": f"删除会话失败（HTTP {resp.status_code}）"}, status=502
        )

    _logger.info(
        "[thread_delete] 会话已删 %s（取消 %d 个在飞 run）", thread_id[:8], len(cancelled)
    )
    return json_response({"ok": True, "thread_id": thread_id, "cancelled": cancelled})


routes: list[BaseRoute] = [
    Route("/api/threads/{thread_id}", delete_thread, methods=["DELETE"]),
]
