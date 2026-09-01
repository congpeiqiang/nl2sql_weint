"""会话分叉 API（P1-5，对标 deepseek-harness ui-workspace fork）。

POST /api/threads/{thread_id}/fork
body: {"message_id": "<可选，分叉锚点消息 id>"}

- 不带 message_id：整线程复制（copy），返回新会话。
- 带 message_id：copy 后在新线程历史里定位「该消息是最后一条」的 checkpoint，
  再用 update_state(as_node="__copy__") 把新线程 head 回退到该 checkpoint ——
  得到「截止到这条消息」的分叉会话，用户可改口径/改条件继续。

实现全部编排 langgraph 内置端点（copy / history / state / patch），
通过 LANGGRAPH_API_URL 自调用（async httpx，不阻塞事件循环）：
  1. POST {base}/threads/{tid}/copy            → 新线程（含全部 checkpoint + metadata）
  2. GET  {base}/threads/{new}/history?limit=&before=  → 从新到旧翻页定位锚点 checkpoint
  3. POST {base}/threads/{new}/state           → values=null + as_node="__copy__"
     （LangGraph 的 fork 原语：复制指定 checkpoint 为新 head，source="fork"）
  4. PATCH {base}/threads/{new}                → 标题加「（分叉）」后缀（尽力而为）

任何一步失败都会尽力删除已创建的副本线程，避免残留脏数据。
"""
from __future__ import annotations

import logging
import os
import re

import httpx
from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response, parse_body

_logger = logging.getLogger(__name__)

# 单页历史条数 / 最多扫描的 checkpoint 数（超长线程保护；锚点通常位于近几页）
_PAGE_SIZE = 50
_MAX_SCAN_CHECKPOINTS = 500

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def _base_url() -> str:
    return (os.environ.get("LANGGRAPH_API_URL") or "http://localhost:2026").rstrip("/")


async def _delete_thread_quiet(http: httpx.AsyncClient, base: str, thread_id: str):
    """尽力删除副本线程（清理用，忽略失败）。"""
    try:
        await http.delete(f"{base}/threads/{thread_id}")
    except Exception as e:  # noqa: BLE001
        _logger.warning("[thread_fork] 清理副本线程 %s 失败: %s", thread_id, e)


async def _find_anchor_checkpoint(
    http: httpx.AsyncClient, base: str, thread_id: str, message_id: str
) -> str | None:
    """从新到旧翻页扫描历史，返回锚点消息对应的 checkpoint_id。

    首选「该消息是最后一条消息」的 checkpoint（其状态恰好截止于锚点）；
    找不到时退回「包含该消息」的最旧 checkpoint（同 superstep 批量追加场景）。
    """
    target_cp: str | None = None
    fallback_cp: str | None = None
    before: str | None = None
    scanned = 0

    while scanned < _MAX_SCAN_CHECKPOINTS:
        limit = min(_PAGE_SIZE, _MAX_SCAN_CHECKPOINTS - scanned)
        params: dict[str, str | int] = {"limit": limit}
        if before:
            params["before"] = before
        r = await http.get(f"{base}/threads/{thread_id}/history", params=params)
        if r.status_code != 200:
            _logger.warning(
                "[thread_fork] 读取历史失败: HTTP %s %s",
                r.status_code,
                r.text[:200],
            )
            break
        entries = r.json() or []
        if not entries:
            break

        for c in entries:  # 新 → 旧
            msgs = (c.get("values") or {}).get("messages") or []
            ids = [m.get("id") for m in msgs if isinstance(m, dict)]
            if not ids:
                continue
            if ids[-1] == message_id:
                return c.get("checkpoint_id")
            if message_id in ids:
                fallback_cp = c.get("checkpoint_id")

        scanned += len(entries)
        if len(entries) < limit:
            break
        oldest = entries[-1].get("checkpoint_id")
        if not oldest:
            break
        before = oldest

    return target_cp or fallback_cp


async def fork_thread(request: Request):
    thread_id = request.path_params["thread_id"]
    if not _UUID_RE.match(thread_id):
        return json_response({"error": "无效的会话 ID"}, status=400)

    data = await parse_body(request)
    message_id = str(data.get("message_id") or "").strip() or None

    base = _base_url()
    timeout = httpx.Timeout(120.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        # 1. 整线程复制
        try:
            r = await http.post(f"{base}/threads/{thread_id}/copy")
        except httpx.HTTPError as e:
            _logger.error("[thread_fork] copy 请求失败: %s", e)
            return json_response({"error": f"复制会话失败: {e}"}, status=502)
        if r.status_code != 200:
            status = 404 if r.status_code in (404, 409) else 502
            return json_response(
                {"error": f"复制会话失败: HTTP {r.status_code}"}, status=status
            )
        new_thread = r.json()
        new_tid = new_thread.get("thread_id")
        if not new_tid:
            return json_response({"error": "复制会话失败: 缺少新会话 ID"}, status=502)

        # 2. （可选）定位锚点 checkpoint 并回退 head
        if message_id:
            target_cp = await _find_anchor_checkpoint(http, base, new_tid, message_id)
            if not target_cp:
                await _delete_thread_quiet(http, base, new_tid)
                return json_response(
                    {"error": "未找到该消息对应的检查点，无法从此处分叉"}, status=404
                )
            try:
                r = await http.post(
                    f"{base}/threads/{new_tid}/state",
                    json={
                        "checkpoint_id": target_cp,
                        "values": None,
                        "as_node": "__copy__",
                    },
                )
            except httpx.HTTPError as e:
                await _delete_thread_quiet(http, base, new_tid)
                return json_response({"error": f"回退分叉点失败: {e}"}, status=502)
            if r.status_code != 200:
                await _delete_thread_quiet(http, base, new_tid)
                _logger.warning(
                    "[thread_fork] update_state 回退失败: HTTP %s %s",
                    r.status_code,
                    r.text[:300],
                )
                return json_response(
                    {"error": f"回退分叉点失败: HTTP {r.status_code}"}, status=502
                )
        else:
            target_cp = None

        # 3. 标题加「（分叉）」后缀（尽力而为，失败不影响分叉结果）
        meta = dict(new_thread.get("metadata") or {})
        orig_title = str(meta.get("title") or "").strip()
        new_title = f"{orig_title}（分叉）" if orig_title else "分叉会话"
        try:
            await http.patch(
                f"{base}/threads/{new_tid}",
                json={"metadata": {**meta, "title": new_title}},
            )
        except httpx.HTTPError as e:
            _logger.warning("[thread_fork] 写入分叉标题失败: %s", e)

        return json_response(
            {
                "ok": True,
                "thread_id": new_tid,
                "source_thread_id": thread_id,
                "checkpoint_id": target_cp,
                "title": new_title,
            }
        )


# ── 路由表（custom_app.py 聚合）──────────────────────────
routes: list[BaseRoute] = [
    Route("/api/threads/{thread_id}/fork", fork_thread, methods=["POST"]),
]
