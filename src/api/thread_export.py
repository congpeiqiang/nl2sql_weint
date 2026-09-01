"""会话日志下载 API —— 导出当前会话为 Markdown / JSON 文件。

GET /api/threads/{thread_id}/export?format=md|json

对标 deepseek-harness dsh-session-log-export，但 nl2sql 的子 agent 运行内嵌在
主会话 state 里（不产生独立 thread），故导出单个会话即可覆盖完整对话
（含子 agent 的 SQL、工具调用），无需多会话树 ZIP。

实现编排 langgraph 内置端点，通过 LANGGRAPH_API_URL 自调用（async httpx，
不阻塞事件循环、不手工碰 msgpack）：
  1. GET {base}/threads/{tid}/state  → values.messages（服务端已正确反序列化）
  2. 按 format 渲染为 Markdown（人类可读聊天记录）或 JSON（原始消息数组）
  3. 返回 attachment 响应（Content-Disposition），浏览器原生下载，前端不缓冲内容

消息结构（与 thread_search._extract_message_text 已确认的字段一致）：
  - type: human / ai / tool / system
  - content: str 或 text block 列表 [{type:"text", text:"..."}]
  - ai 消息 tool_calls: [{name, args:{sql?, ...}, id}]
  - additional_kwargs.reasoning_content: 深度思考内容
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

_logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def _base_url() -> str:
    return (os.environ.get("LANGGRAPH_API_URL") or "http://localhost:2026").rstrip("/")


def _sanitize_filename(s: str) -> str:
    """净化文件名（去除路径分隔符/非法字符，保留 UUID 与安全字符）。"""
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", s)
    return s.strip("._") or "session"


# ── 消息文本提取 ────────────────────────────────────────────────────────

def _msg_text(msg: dict) -> str:
    """提取一条消息的纯文本 content（兼容 str 与 text block 列表）。"""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                t = blk.get("text", "")
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return str(content) if content else ""


def _render_tool_calls(msg: dict) -> str:
    """渲染 ai 消息的 tool_calls（重点展示 SQL）。"""
    lines = []
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        name = tc.get("name") or "unknown"
        args = tc.get("args") or {}
        if isinstance(args, dict) and args.get("sql"):
            sql = str(args["sql"]).strip()
            lines.append(f"**调用工具** `{name}`：\n\n```sql\n{sql}\n```")
        else:
            lines.append(f"**调用工具** `{name}`")
    return "\n\n".join(lines)


def _render_message(msg: dict) -> str:
    """单条消息 → Markdown 块。"""
    typ = msg.get("type") or msg.get("role") or ""
    text = _msg_text(msg)

    if typ in ("human", "user"):
        return f"## 👤 用户\n\n{text}" if text else "## 👤 用户"

    if typ in ("ai", "assistant"):
        parts = ["## 🤖 助手"]
        # 深度思考折叠块（reasoning_content 捕获自 additional_kwargs）
        reasoning = (msg.get("additional_kwargs") or {}).get("reasoning_content")
        if reasoning and str(reasoning).strip():
            parts.append(
                f"<details>\n<summary>深度思考</summary>\n\n{str(reasoning).strip()}\n\n</details>"
            )
        if text:
            parts.append(text)
        tc = _render_tool_calls(msg)
        if tc:
            parts.append(tc)
        return "\n\n".join(parts) if len(parts) > 1 else parts[0]

    if typ == "tool":
        name = msg.get("name") or "tool"
        return f"### 🔧 工具 `{name}`\n\n```\n{text}\n```"

    # system / 其他
    return f"## {typ}\n\n{text}" if text else f"## {typ}"


def _to_markdown(messages: list, thread_id: str, title: str) -> str:
    """完整消息列表 → Markdown 文档。"""
    header = [
        f"# 会话日志",
        f"",
        f"- 会话 ID：`{thread_id}`",
        f"- 标题：{title or '（无标题）'}",
        f"- 消息数：{len(messages)}",
        f"",
        "---",
        "",
    ]
    body = []
    for m in messages:
        if isinstance(m, dict):
            body.append(_render_message(m))
            body.append("")
    return "\n".join(header + body).strip() + "\n"


# ── 端点 ────────────────────────────────────────────────────────────────

async def export_thread(request: Request):
    thread_id = request.path_params["thread_id"]
    if not _UUID_RE.match(thread_id):
        return Response("无效的会话 ID", status_code=400, media_type="text/plain")

    fmt = (request.query_params.get("format") or "md").lower()
    if fmt not in ("md", "json"):
        return Response("format 仅支持 md / json", status_code=400, media_type="text/plain")

    base = _base_url()
    timeout = httpx.Timeout(120.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            r = await http.get(f"{base}/threads/{thread_id}/state")
        except httpx.HTTPError as e:
            _logger.error("[thread_export] 读取会话 state 失败: %s", e)
            return Response(f"读取会话失败: {e}", status_code=502, media_type="text/plain")
        if r.status_code == 404:
            return Response("会话不存在", status_code=404, media_type="text/plain")
        if r.status_code != 200:
            return Response(
                f"读取会话失败: HTTP {r.status_code}", status_code=502, media_type="text/plain"
            )
        state = r.json() or {}
        values = state.get("values") or {}
        messages = values.get("messages") or []
        title = (state.get("metadata") or {}).get("title") or ""

    safe = _sanitize_filename(thread_id)
    if fmt == "json":
        body = json.dumps(
            {"thread_id": thread_id, "title": title, "messages": messages},
            ensure_ascii=False,
            indent=2,
        )
        filename = f"session-{safe}.json"
        media_type = "application/json; charset=utf-8"
    else:
        body = _to_markdown(messages, thread_id, title)
        filename = f"session-{safe}.md"
        media_type = "text/markdown; charset=utf-8"

    return Response(
        body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(body.encode("utf-8"))),
        },
    )


# ── 路由表（custom_app.py 聚合）──────────────────────────
routes: list[BaseRoute] = [
    Route("/api/threads/{thread_id}/export", export_thread, methods=["GET"]),
]
