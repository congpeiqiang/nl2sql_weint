"""SQL 审批恢复 API（P1-3，对标 deepseek-harness ApprovalPanel 的 Allow/Reject 回调）。

POST /api/threads/{sub_thread_id}/sql-approval
body: {
  "main_thread_id": "<主会话 thread_id>",          # 必填：用于回写 async_tasks
  "decisions": [{"type": "approve"} | {"type":"reject","message":...}
                 | {"type":"edit","edited_action":{"name","args"}}],
  "db_name": "<可选，恢复 run 的 configurable.db_name>"
}

子 agent（nl2sql_agent）的 run_sql 工具被审批闸门 interrupt 后，子 run 停在
"interrupted"。本端点把用户决策以 `Command(resume={"decisions": [...]})`
注回子线程，子 run 从断点继续（approve 执行 / reject 返回拒绝 ToolMessage /
edit 用修改后的 SQL 执行）。

随后尽力把主线程 async_tasks[task] 条目刷新（清 awaiting_approval、换新 run_id）；
若 sync watcher 线程已退出（等待超上限等）还会重新拉起，保证后续进度/终态同步。
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

_VALID_DECISION_TYPES = {"approve", "reject", "edit"}
_MAIN_WRITE_RETRIES = 3


def _base_url() -> str:
    return (os.environ.get("LANGGRAPH_API_URL") or "http://localhost:2026").rstrip("/")


def _validate_decisions(decisions) -> str | None:
    """校验 decisions 结构，返回错误信息；合法返回 None。"""
    if not isinstance(decisions, list) or not decisions:
        return "decisions 必须是非空数组"
    for d in decisions:
        if not isinstance(d, dict):
            return "decision 必须是对象"
        dtype = d.get("type")
        if dtype not in _VALID_DECISION_TYPES:
            return f"非法 decision 类型: {dtype!r}"
        if dtype == "edit":
            edited = d.get("edited_action")
            if not isinstance(edited, dict) or not edited.get("name") \
                    or not isinstance(edited.get("args"), dict):
                return "edit 决策需要 edited_action.name 与 edited_action.args"
        if dtype == "reject" and "message" in d and not isinstance(d["message"], str):
            return "reject 决策的 message 必须是字符串"
    return None


async def _find_pending_interrupt(
    http: httpx.AsyncClient, base: str, thread_id: str
) -> dict | None:
    """读子线程 state，返回待处理审批 interrupt 的 payload（无则 None）。"""
    try:
        resp = await http.get(f"{base}/threads/{thread_id}/state")
        if resp.status_code != 200:
            return None
        state = resp.json()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[sql_approval] 读子线程 state 失败: %s", e)
        return None
    for task in state.get("tasks") or []:
        for intr in task.get("interrupts") or []:
            value = intr.get("value")
            if isinstance(value, dict) and value.get("action_requests"):
                return value
    return None


def _ensure_sync_watcher(main_thread_id: str, sub_thread_id: str, task_entry: dict):
    """sync watcher 已退出时重新拉起（保证恢复后的进度/终态同步不丢）。"""
    try:
        from agent.subagents.sync_subagent_todos import is_sync_alive, launch_sync

        if not is_sync_alive(sub_thread_id):
            launch_sync(
                main_thread_id,
                sub_thread_id,
                str(task_entry.get("agent_name") or "nl2sql"),
                task_entry,
            )
            _logger.info(
                "[sql_approval] sync watcher 已退出，重新拉起: %s", sub_thread_id[:8]
            )
    except Exception as e:  # noqa: BLE001
        _logger.warning("[sql_approval] 重启 sync watcher 失败: %s", e)


async def _refresh_main_task_entry(
    base: str,
    main_thread_id: str,
    sub_thread_id: str,
    new_run_id: str | None,
) -> None:
    """尽力刷新主线程 async_tasks[sub] 条目：清除 awaiting_approval、换 run_id。

    主线程可能 in-flight（用户正在对话）→ update_state 被拒；重试几次，
    最终兜底由 sync watcher 负责（它也会写该条目）。无论成败都保证 watcher 存活。
    """
    fallback_entry = {
        "task_id": sub_thread_id,
        "thread_id": sub_thread_id,
        "agent_name": "nl2sql",
        "run_id": new_run_id,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        for attempt in range(_MAIN_WRITE_RETRIES):
            try:
                resp = await client.get(f"{base}/threads/{main_thread_id}/state")
                if resp.status_code != 200:
                    _ensure_sync_watcher(main_thread_id, sub_thread_id, fallback_entry)
                    return
                values = (resp.json() or {}).get("values") or {}
                entry = (values.get("async_tasks") or {}).get(sub_thread_id)
                if not isinstance(entry, dict):
                    # 条目不存在 → 无法回写，交给 sync watcher
                    _ensure_sync_watcher(main_thread_id, sub_thread_id, fallback_entry)
                    return
                updated = dict(entry)
                updated.pop("awaiting_approval", None)
                updated["status"] = "running"
                if new_run_id:
                    updated["run_id"] = new_run_id
                updated["last_updated_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                )
                resp2 = await client.post(
                    f"{base}/threads/{main_thread_id}/state",
                    json={
                        "values": {"async_tasks": {sub_thread_id: updated}},
                        "as_node": "__start__",
                    },
                )
                if resp2.status_code == 200:
                    _logger.info(
                        "[sql_approval] 主线程 async_tasks[%s] 已刷新 (run_id=%s)",
                        sub_thread_id[:8], new_run_id,
                    )
                    _ensure_sync_watcher(main_thread_id, sub_thread_id, updated)
                    return
                _logger.warning(
                    "[sql_approval] 主线程条目刷新被拒 (HTTP %d, 第%d次)",
                    resp2.status_code, attempt + 1,
                )
            except Exception as e:  # noqa: BLE001
                _logger.warning("[sql_approval] 主线程条目刷新失败: %s", e)
            # 退避等待 in-flight run 结束
            await asyncio.sleep(1.5)
    _ensure_sync_watcher(main_thread_id, sub_thread_id, fallback_entry)


async def decide_sql_approval(request: Request):
    sub_thread_id = request.path_params.get("thread_id", "")
    if not _UUID_RE.match(sub_thread_id or ""):
        return json_response({"ok": False, "error": "非法的子会话 ID"}, status=400)

    body = await parse_body(request)
    main_thread_id = str(body.get("main_thread_id") or "")
    decisions = body.get("decisions")
    db_name = str(body.get("db_name") or "")

    if not _UUID_RE.match(main_thread_id):
        return json_response(
            {"ok": False, "error": "main_thread_id 缺失或非法"}, status=400
        )
    err = _validate_decisions(decisions)
    if err:
        return json_response({"ok": False, "error": err}, status=400)

    base = _base_url()
    timeout = httpx.Timeout(120.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        # 1. 确认子线程确实停在审批 interrupt 上
        pending = await _find_pending_interrupt(http, base, sub_thread_id)
        if pending is None:
            return json_response(
                {"ok": False, "error": "该任务当前没有待审批的 SQL（可能已处理）"},
                status=409,
            )

        # 2. graph_id 从线程 metadata 读（thread 创建时已写入）
        graph_id = "nl2sql_agent"
        try:
            resp = await http.get(f"{base}/threads/{sub_thread_id}")
            if resp.status_code == 200:
                meta = (resp.json() or {}).get("metadata") or {}
                graph_id = str(meta.get("graph_id") or graph_id)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[sql_approval] 读子线程 metadata 失败: %s", e)

        # 3. 恢复子 run：Command(resume={"decisions": [...]})
        resume_config: dict = {"recursion_limit": 500}
        if db_name:
            resume_config["configurable"] = {"db_name": db_name}
        try:
            resp = await http.post(
                f"{base}/threads/{sub_thread_id}/runs",
                json={
                    "assistant_id": graph_id,
                    "command": {"resume": {"decisions": decisions}},
                    "config": resume_config,
                },
            )
        except Exception as e:  # noqa: BLE001
            _logger.error("[sql_approval] 恢复子 run 请求失败: %s", e)
            return json_response({"ok": False, "error": f"恢复执行失败: {e}"}, status=502)
        if resp.status_code not in (200, 201, 202):
            _logger.error(
                "[sql_approval] 恢复子 run 被拒: HTTP %d %s",
                resp.status_code, resp.text[:200],
            )
            return json_response(
                {
                    "ok": False,
                    "error": f"恢复执行失败 (HTTP {resp.status_code})",
                },
                status=502,
            )
        run = {}
        try:
            run = resp.json() or {}
        except Exception:  # noqa: BLE001
            pass
        new_run_id = run.get("run_id")

    # 4. 尽力刷新主线程条目 + 保活 watcher（失败不阻断响应）
    await _refresh_main_task_entry(
        base=base,
        main_thread_id=main_thread_id,
        sub_thread_id=sub_thread_id,
        new_run_id=new_run_id,
    )

    return json_response(
        {
            "ok": True,
            "thread_id": sub_thread_id,
            "main_thread_id": main_thread_id,
            "run_id": new_run_id,
        }
    )


routes: list[BaseRoute] = [
    Route(
        "/api/threads/{thread_id}/sql-approval",
        decide_sql_approval,
        methods=["POST"],
    ),
]
