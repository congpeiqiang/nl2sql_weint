"""异步子任务取消 API（P1-8）。

POST /api/threads/{task_id}/cancel
body: {
  "main_thread_id": "<主会话 thread_id>",       # 必填：用于回写 async_tasks
}

用户在输入区进度卡片点「停止」时调用。本端点做三件事：
1. 读主线程 async_tasks[task_id] 拿到子 run 的 run_id
2. 真正取消子 run（POST /threads/{task_id}/runs/{run_id}/cancel，action=interrupt）
3. 回写主线程 async_tasks[task_id].status = "cancelled"（重试 + sync watcher 保活）

与 deepagents 内置 cancel_async_task（agent tool）的语义一致，但作为 HTTP 端点
暴露给前端按钮直接触发，不再依赖主智能体 LLM 响应。

边界：任务正停在 SQL 审批闸门（run 状态 success、但 awaiting_approval 已中继）
时同样可取消——此时取消子 run 是 no-op，但回写 cancelled + 清 awaiting_approval
会让审批卡消失；sync 侧 P1-8 检查保证不把 awaiting_approval 死而复生。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

import httpx
from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response, parse_body

_logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# deepagents 的 run 终态集（async_subagents._TERMINAL_STATUSES），与 sync 一致
_TERMINAL_STATUSES = {"success", "error", "cancelled", "timeout", "interrupted"}
_MAIN_WRITE_RETRIES = 3


def _base_url() -> str:
    return (os.environ.get("LANGGRAPH_API_URL") or "http://localhost:2026").rstrip("/")


async def _read_main_entry(
    http: httpx.AsyncClient, base: str, main_thread_id: str, task_id: str
) -> dict | None:
    """读主线程 state 中 async_tasks[task_id] 条目。"""
    try:
        resp = await http.get(f"{base}/threads/{main_thread_id}/state")
        if resp.status_code != 200:
            return None
        values = (resp.json() or {}).get("values") or {}
        entry = (values.get("async_tasks") or {}).get(task_id)
        return entry if isinstance(entry, dict) else None
    except Exception as e:  # noqa: BLE001
        _logger.warning("[task_cancel] 读主线程条目失败: %s", e)
        return None


async def _get_latest_run(
    http: httpx.AsyncClient, base: str, thread_id: str
) -> dict | None:
    """读子线程最新 run（含 run_id/status）。"""
    try:
        resp = await http.get(f"{base}/threads/{thread_id}/runs", params={"limit": 1})
        if resp.status_code != 200:
            return None
        runs = resp.json() or []
        return runs[0] if runs else None
    except Exception as e:  # noqa: BLE001
        _logger.warning("[task_cancel] 读子 run 失败: %s", e)
        return None


def _ensure_sync_watcher(main_thread_id: str, task_id: str, task_entry: dict):
    """sync watcher 已退出时重新拉起（保证取消后的终态/active_queries 同步不丢）。"""
    try:
        from agent.subagents.sync_subagent_todos import is_sync_alive, launch_sync

        if not is_sync_alive(task_id):
            launch_sync(
                main_thread_id,
                task_id,
                str(task_entry.get("agent_name") or "nl2sql"),
                task_entry,
            )
            _logger.info(
                "[task_cancel] sync watcher 已退出，重新拉起: %s", task_id[:8]
            )
    except Exception as e:  # noqa: BLE001
        _logger.warning("[task_cancel] 重启 sync watcher 失败: %s", e)


async def _mark_main_cancelled(
    base: str,
    main_thread_id: str,
    task_id: str,
    entry: dict,
    status_override: str = "cancelled",
) -> None:
    """回写主线程 async_tasks[task_id] → 终态（重试 + watcher 保活）。

    主线程可能 in-flight（用户正在对话）→ update_state 被拒；重试几次，
    最终兜底由 sync watcher 负责（P1-7 保留 cancelled）。

    status_override 默认 "cancelled"（用户主动取消）；当子 run 已自然终态
    （success/error）而 async_tasks 还 running 时，回写 run 的实际终态，
    让前端进度条不再永久转圈（见 cancel_task 幂等分支）。
    """
    updated = dict(entry)
    updated["task_id"] = task_id
    updated["thread_id"] = task_id
    updated["status"] = status_override
    updated.pop("awaiting_approval", None)
    updated["last_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        for attempt in range(_MAIN_WRITE_RETRIES):
            try:
                resp = await client.post(
                    f"{base}/threads/{main_thread_id}/state",
                    json={
                        "values": {"async_tasks": {task_id: updated}},
                        "as_node": "__start__",
                    },
                )
                if resp.status_code == 200:
                    _logger.info(
                        "[task_cancel] 主线程 async_tasks[%s] 已标记 cancelled",
                        task_id[:8],
                    )
                    _ensure_sync_watcher(main_thread_id, task_id, updated)
                    return
                _logger.warning(
                    "[task_cancel] 标记 cancelled 被拒 (HTTP %d, 第%d次)",
                    resp.status_code, attempt + 1,
                )
            except Exception as e:  # noqa: BLE001
                _logger.warning("[task_cancel] 标记 cancelled 失败: %s", e)
            # 退避等待 in-flight run 结束
            await asyncio.sleep(1.5)
    _ensure_sync_watcher(main_thread_id, task_id, updated)


async def cancel_task(request: Request):
    task_id = request.path_params.get("task_id", "")
    if not _UUID_RE.match(task_id or ""):
        return json_response({"ok": False, "error": "非法的任务 ID"}, status=400)

    body = await parse_body(request)
    main_thread_id = str(body.get("main_thread_id") or "")
    if not _UUID_RE.match(main_thread_id):
        return json_response(
            {"ok": False, "error": "main_thread_id 缺失或非法"}, status=400
        )

    base = _base_url()
    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        # 1. 读主线程 async_tasks 条目
        entry = await _read_main_entry(http, base, main_thread_id, task_id)
        if entry is None:
            return json_response(
                {"ok": False, "error": "找不到该任务（可能已从会话中移除）"},
                status=404,
            )
        if entry.get("status") == "cancelled":
            # 幂等：已取消直接返回成功
            return json_response(
                {
                    "ok": True,
                    "task_id": task_id,
                    "main_thread_id": main_thread_id,
                    "status": "cancelled",
                }
            )

        # 2. 判断子 run 是否仍可取消
        latest = await _get_latest_run(http, base, task_id)
        run_status = latest.get("status") if latest else None
        pending_approval = bool(entry.get("awaiting_approval"))

        # 已终态且无待审批 → 任务已结束，幂等返回成功
        # （sync watcher 可能把 cancelled 覆盖为 interrupted 等其他终态，
        #  前端重试 cancel 时不应报错——用户的目的是让任务停下来，已停就行）
        if run_status in _TERMINAL_STATUSES and not pending_approval:
            # ⚠ 若 async_tasks 仍显示 running（重启后 sync watcher 未拉起 /
            # 僵尸 run 恢复），必须回写实际终态，否则前端进度条永久 running、
            # 点停止无效（2026-08-29 会话 01a04b54 实测复现）。
            if entry.get("status") not in _TERMINAL_STATUSES:
                await _mark_main_cancelled(
                    base,
                    main_thread_id,
                    task_id,
                    entry,
                    status_override=run_status or "cancelled",
                )
            return json_response(
                {
                    "ok": True,
                    "task_id": task_id,
                    "main_thread_id": main_thread_id,
                    # run 已终态时优先返回实际终态（entry 可能还是旧的 running）
                    "status": run_status or entry.get("status") or "cancelled",
                }
            )

        # 3. 取消子 run（真正 running 才 cancel；停在审批闸门时是 no-op）
        run_id = entry.get("run_id") or (latest or {}).get("run_id")
        if run_status not in _TERMINAL_STATUSES and run_id:
            try:
                resp = await http.post(
                    f"{base}/threads/{task_id}/runs/{run_id}/cancel",
                    params={"action": "interrupt"},
                )
                if resp.status_code >= 400:
                    _logger.warning(
                        "[task_cancel] 取消子 run 被拒: HTTP %d %s",
                        resp.status_code, resp.text[:200],
                    )
            except Exception as e:  # noqa: BLE001
                _logger.warning("[task_cancel] 取消子 run 请求失败: %s", e)

    # 4. 回写主线程 async_tasks → cancelled（重试 + watcher 保活）
    await _mark_main_cancelled(base, main_thread_id, task_id, entry)

    return json_response(
        {
            "ok": True,
            "task_id": task_id,
            "main_thread_id": main_thread_id,
            "status": "cancelled",
        }
    )


routes: list[BaseRoute] = [
    Route(
        "/api/threads/{task_id}/cancel",
        cancel_task,
        methods=["POST"],
    ),
]
