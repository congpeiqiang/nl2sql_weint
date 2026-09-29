"""会话 run 状态查询 API（治「turn 被外部打断后前端永久转圈」）。

GET /api/threads/{thread_id}/run-status
→ {
    "ok": True,
    "thread_id": "...",
    "checkpoint_id": "...",
    "has_active_run": False,        # 有 pending/running 的 run
    "active_run_ids": [],
    "next": ["model"],              # state.next（图停在哪个节点）
    "awaiting_interrupt": False,    # HITL 审批等待中
    "last_message_is_final": False, # 最后一条消息是「终稿 assistant 文本」
    "last_run": {"run_id": ..., "status": "interrupted", "updated_at": ...},
    "turn_incomplete": True,        # ← 前端据此渲染「已中断，可继续」
    "turn_failed": False,           # ← 前端据此渲染「执行失败 + 原因 + 重试」
    "last_error": "",               # turn_failed 时给出可读的失败原因
  }

背景（生产事故 01a09edd，2026-09-14）：后端重启把一次 run 打死在 `model` 节点上，
state 停在 `next=['model']`、没有活跃 run；前端只拿到 messages 与 isLoading（流被
掐断时 isLoading 可能一直为 True），界面就永久停在最后一个工具调用（`write_todos`）
上转圈。本端点把「图停在半轮且没人在推」这个事实显式暴露给前端。

判据（四者同时成立才提示，任一不成立都不提示——宁可漏报不可误报）：
1. `next` 非空 —— 图还有待执行节点；
2. 无 pending/running 的 run —— 没人在推；
3. 无 HITL interrupt 等待 —— 那是等用户审批，界面另有审批卡；
4. 最后一条消息不是「终稿 assistant 文本」（有正文、无 tool_calls）。

判据 4 必需：实测存在 run=success 但 next 非空的会话（图在节点边界结束），
此时最后一条消息是完整答复（01a09ed5-1208，07:37:22 success + 2723 字答复），
只看 next+runs 会误报。

其中**最常见**的一类是「幽灵 next」（P2-12）：sync 的
`update_state(as_node="__start__")` 状态补丁（`_sync_update_state`）会把 head checkpoint
的 `next` 写成图入口节点，而它只可能在主 run 终态之后落地（LangGraph 的 update_state
硬闸）⇒ 补丁后图头挂着一个没有任何机制推进的节点。它**不是丢步**（答复已终稿），
判据 4 正是把它判成「不算未完成」的那一条。机制与判据回归见
`scripts/verify_phantom_next.py`；**切勿**把「next 非空」单独当作未完成的证据。

**判据 5（turn_failed）**：最后一轮 run 是终态失败（error/timeout）时不报「已中断」，
改报 `turn_failed` + `last_error`。生产 01a0a394（2026-09-15）：新建会话第一轮就被
provider 400 打回（`The supported API model names are deepseek-flash, deepseek-v4-pro,
but you passed deepseek-v4.1-flash.`），线程里只有一条 human 消息、`next=['model']`、
无活跃 run —— 四判据全部成立，界面显示「上一轮回复已中断（可继续）」，用户点「继续」
只是把同一个必失败的请求再发一遍，且从头到尾看不到原因。`cancelled`（用户点停止）与
`interrupted`（HITL）**不算失败**，仍走中断/审批分支。
原因文本取自 `state.tasks[].error`：run 对象的 `error` 字段在本环境恒为 None
（列表与单 run GET 都实测过），只有 checkpoint 里的 task 保留了异常。

**判据 5 的第二个来源（2026-09-28）**：`run.status == "success"` 但**末条 AI 消息
带失败戳**（`agent/utils/failure_signal.py`）—— 超时/额度耗尽/未配模型被中间件吞成一条
友好 AIMessage，图照常 END ⇒ SDK 报 success，而这一轮**什么都没执行**。此前它被当成
一段正常回答显示给用户（用户以为答完了）。戳是机器可读的，比文本匹配可靠（主 agent
向用户解释失败时正文里也会出现「模型调用超时」几个字）。此时 `last_error` 回落到戳里的
`detail`。

已知边界：若历史上有 run 卡成 `running` 僵尸（.langgraph_ops.pckl 复活，
见 langgraph-inmem-pckl-zombie-runs），判据 2 恒不成立、本端点不会提示——
那种情况要清 pckl 重启，属于另一条链路。
"""
from __future__ import annotations

import ast
import json
import logging
import os
import re

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from agent.utils.failure_signal import detail_of, last_failed_mark
from api._common import json_response

_logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# langgraph run 的非终态（排队中 / 执行中）——与 task_cancel._TERMINAL_STATUSES 互斥
_ACTIVE_STATUSES = {"pending", "running"}

# 算「上一轮执行失败」的 run 终态。故意不含 cancelled（用户点停止）与 interrupted
# （HITL 审批）——那两者是用户/流程预期的暂停，报「失败」会误导。
_FAILED_STATUSES = {"error", "timeout"}

_ERROR_MAX = 300
# tasks[].error 是异常对象的 repr：BadRequestError("Error code: 400 - {'error': {...}}")
_ERR_REPR_RE = re.compile(r"^[A-Za-z_][\w.]*\((.*)\)$", re.S)
# provider 正文里的 message 才是最该给用户看的一句
_ERR_MSG_RE = re.compile(r"['\"]message['\"]\s*:\s*['\"](.+?)['\"]\s*[,}]", re.S)

_RUNS_PAGE = 10


def _base_url() -> str:
    return (os.environ.get("LANGGRAPH_API_URL") or "http://localhost:2026").rstrip("/")


def _text_of(message: dict) -> str:
    """取消息正文（兼容字符串 content 与 content-block 列表）。"""
    content = (message or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text") or "" for b in content if isinstance(b, dict)
        )
    return ""


def _last_message_is_final(values: dict) -> bool:
    """最后一条消息是否为「终稿 assistant 文本」（有正文且无 tool_calls）。

    纯工具调用（content 为空、只有 tool_calls）不算终稿——那正是被打断的形态。
    """
    messages = (values or {}).get("messages") or []
    if not messages:
        return False
    last = messages[-1] or {}
    role = last.get("type") or last.get("role")
    if role != "ai":
        return False
    if last.get("tool_calls"):
        return False
    if (last.get("additional_kwargs") or {}).get("tool_calls"):
        return False
    return bool(_text_of(last).strip())


def _has_interrupt(state: dict) -> bool:
    """state.tasks[].interrupts 非空 = 图停在 HITL 审批上（不是「被打断」）。"""
    for task in state.get("tasks") or []:
        if isinstance(task, dict) and task.get("interrupts"):
            return True
    return False


def _extract_error_text(raw: object) -> str:
    """把 tasks[].error 压成一行可读文本（前端直接展示给用户）。

    `tasks[].error` 是异常对象的 repr，例如
    `BadRequestError("Error code: 400 - {'error': {'message': 'The supported API model
    names are deepseek-flash, deepseek-v4-pro, but you passed deepseek-v4.1-flash.', …}}")`。
    三步剥：异常外壳 → provider 的 message → 压平截断。任何一步解析失败都退回
    上一层的原文，绝不返回空串（宁可给用户一段丑的也不能给「未知错误」）。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    m = _ERR_REPR_RE.match(text)
    if m:
        inner = m.group(1).strip()
        try:
            val = ast.literal_eval(inner)
        except (ValueError, SyntaxError):
            val = inner
        if isinstance(val, str):
            text = val
        elif isinstance(val, (dict, list)):
            text = json.dumps(val, ensure_ascii=False)
        else:
            text = inner
    m2 = _ERR_MSG_RE.search(text)
    if m2 and m2.group(1).strip():
        text = m2.group(1)
    text = " ".join(text.split())
    if len(text) > _ERROR_MAX:
        text = text[: _ERROR_MAX - 1] + "…"
    return text


def _last_task_error(state: dict, next_nodes: list) -> str:
    """取 tasks[].error 里最新一条非空错误，优先图当前停着的节点。

    生产上只有停在 `next` 的那个 task 带 error；退一步取任意最后一个带 error 的
    task（多节点并行时 next 可能已被清空）。
    """
    tasks = [t for t in (state.get("tasks") or []) if isinstance(t, dict)]
    if not tasks:
        return ""
    pending = set(next_nodes or [])
    picked = [t for t in tasks if t.get("name") in pending and t.get("error")]
    if not picked:
        picked = [t for t in tasks if t.get("error")]
    if not picked:
        return ""
    return _extract_error_text(picked[-1].get("error"))


def classify(state: dict, runs: list) -> dict:
    """纯函数：由 state + runs 判定是否提示「已中断」。

    抽成纯函数便于离线回归——本文件其余部分只做 HTTP 自调用，不易单测。
    """
    state = state or {}
    runs = [r for r in (runs or []) if isinstance(r, dict)]
    active = [r for r in runs if r.get("status") in _ACTIVE_STATUSES]
    # /runs 排序不保证（task_cancel 直接取 runs[0]），这里显式取 created_at 最大者
    ordered = sorted(runs, key=lambda r: str(r.get("created_at") or ""))
    last_run = ordered[-1] if ordered else None
    next_nodes = list(state.get("next") or [])
    awaiting = _has_interrupt(state)
    is_final = _last_message_is_final(state.get("values") or {})
    # 末条 AI 上的失败戳（超时/额度耗尽/未配模型被中间件吞成友好文案，见 failure_signal）：
    # 这类 run 的 status 是 success（图照常 END），只有内容知道它其实什么都没执行。
    mark = last_failed_mark((state.get("values") or {}).get("messages") or [])
    # 判据 5：最后一轮 run 终态失败 **或** 末条带失败戳 → 是「跑过但挂了」，
    # 不是「被外部打断」。无活跃 run / 无审批是前提（有活跃 run 时 last_run 就是那条
    # run，本也命不中）。
    turn_failed = bool(
        ((last_run and last_run.get("status") in _FAILED_STATUSES) or mark)
        and not active
        and not awaiting
    )
    return {
        "has_active_run": bool(active),
        "active_run_ids": [r.get("run_id") for r in active],
        "next": next_nodes,
        "awaiting_interrupt": awaiting,
        "last_message_is_final": is_final,
        "last_run": (
            {
                "run_id": last_run.get("run_id"),
                "status": last_run.get("status"),
                "updated_at": last_run.get("updated_at"),
            }
            if last_run
            else None
        ),
        "turn_failed": turn_failed,
        # 优先 checkpoint 里的 task.error（真异常），没有则回落到失败戳的 detail
        "last_error": (
            _last_task_error(state, next_nodes) or detail_of(mark)
        ) if turn_failed else "",
        "turn_incomplete": bool(next_nodes)
        and not active
        and not awaiting
        and not is_final
        and not turn_failed,
    }


async def thread_run_status(request: Request) -> JSONResponse:
    thread_id = str(request.path_params.get("thread_id") or "")
    if not _UUID_RE.match(thread_id):
        return json_response({"ok": False, "error": "invalid thread_id"}, 400)

    # P1：显式归属校验（原先只靠下游 ops 层兜底）。放在最前，fail-fast，
    # 不把「有没有这个会话」暴露给非 owner（403 与 404 的区别另见 E2E 约定）。
    from api._common import require_thread
    require_thread(request, thread_id)

    base = _base_url()
    timeout = httpx.Timeout(10.0, connect=5.0)
    # 转发原始请求的认证头（Cookie/Authorization），供 LangGraph auth 校验
    auth_headers: dict[str, str] = {}
    cookie = request.headers.get("cookie")
    if cookie:
        auth_headers["cookie"] = cookie
    authorization = request.headers.get("authorization")
    if authorization:
        auth_headers["authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as http:
            state_resp = await http.get(
                f"{base}/threads/{thread_id}/state", headers=auth_headers
            )
            if state_resp.status_code == 404:
                return json_response({"ok": False, "error": "thread not found"}, 404)
            if state_resp.status_code != 200:
                # fail-closed：读不到就绝不提示（避免误报横幅）
                return json_response(
                    {"ok": False, "error": f"state HTTP {state_resp.status_code}"}, 502
                )
            state = state_resp.json() or {}

            runs_resp = await http.get(
                f"{base}/threads/{thread_id}/runs",
                params={"limit": _RUNS_PAGE},
                headers=auth_headers,
            )
            if runs_resp.status_code != 200:
                return json_response(
                    {"ok": False, "error": f"runs HTTP {runs_resp.status_code}"}, 502
                )
            runs = runs_resp.json() or []
    except Exception as e:  # noqa: BLE001
        _logger.warning("[thread_run_status] 读取会话状态失败: %s", e)
        return json_response({"ok": False, "error": "upstream unavailable"}, 502)

    payload = {
        "ok": True,
        "thread_id": thread_id,
        "checkpoint_id": state.get("checkpoint_id"),
    }
    payload.update(classify(state, runs))
    return json_response(payload)


routes: list[BaseRoute] = [
    Route(
        "/api/threads/{thread_id}/run-status",
        thread_run_status,
        methods=["GET"],
    ),
]
