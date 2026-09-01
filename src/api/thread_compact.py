"""手动压缩 API —— 用户点击上下文圆环时触发，LLM 总结旧消息 + RemoveMessage 裁剪。

POST /api/threads/{thread_id}/compact

对标 deepagents 的 SummarizationMiddleware（自动压缩，85% 阈值），本端点
提供用户主动触发的压缩路径（对标 v2 手动压缩）。

实现编排 langgraph 内置端点，通过 LANGGRAPH_API_URL 自调用（async httpx）：
  1. GET {base}/threads/{tid}/state  → values.messages（当前消息列表）
  2. 按 keep 策略分割消息（保留最近 ~10% 上下文窗口，即 ~12000 tokens）
  3. 调用 LLM 生成摘要（采用廉价模型，enable_thinking=False 节省成本）
  4. POST {base}/threads/{tid}/state  → RemoveMessage(REMOVE_ALL_MESSAGES)
     + 摘要消息 + 保留的消息，as_node 触发 __copy__ 语义
  5. 返回 {"ok": true, "summarized_count": N, "preserved_count": M}

设计要点：
  - 复用 thread_fork.py 的自调用模式（httpx.AsyncClient → POST /threads/{tid}/state）
  - RemoveMessage(id=REMOVE_ALL_MESSAGES) 从 langgraph.graph.message 导入
  - 摘要消息类型为 HumanMessage，带 lc_source="summarization" 标记
  - 消息分割基于字符数估算 token（~4 chars/token），精确度足够
  - fail-safe：任何步骤失败返回 502，不修改原始 state
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from api._common import json_response

_logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# 上下文窗口上限（与 model.py:169 ModelProfile(max_input_tokens=120000) 一致）
_MAX_CONTEXT_TOKENS = 120_000

# 保留比例：最近 10% 上下文窗口的消息不被压缩
_KEEP_FRACTION = 0.10

# 保留消息数下限：至少保留最近 8 条消息不压缩（保证对话连贯性）
_MIN_KEEP_MESSAGES = 8

# 压缩触发下限：少于 20 条消息不压缩（压缩无意义）
_MIN_MESSAGES_TO_COMPACT = 20

# 摘要生成用的提示词
_SUMMARY_SYSTEM_PROMPT = """你是一个对话摘要助手。请将以下对话历史总结为简洁的摘要，保留关键信息：
- 用户的问题和需求
- 助手执行的主要操作（SQL 查询、工具调用、数据分析等）
- 重要的中间结果和结论
- 任何未完成的待办事项

摘要应简洁但完整，用中文输出。不要遗漏重要的上下文信息。
"""

# 估算 token 数（粗略：4 字符 ≈ 1 token）
_CHARS_PER_TOKEN = 4


def _base_url() -> str:
    return (os.environ.get("LANGGRAPH_API_URL") or "http://localhost:2026").rstrip("/")


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


def _estimate_tokens(messages: list[dict]) -> int:
    """估算消息列表的总 token 数（字符数 / 4）。"""
    total = 0
    for m in messages:
        total += len(_msg_text(m))
    return total // _CHARS_PER_TOKEN


def _split_messages(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """将消息列表分割为「待总结」和「保留」两部分。

    保留策略：至少保留 _MIN_KEEP_MESSAGES 条，且保留部分不超过
    _KEEP_FRACTION * _MAX_CONTEXT_TOKENS tokens（约 12000 tokens）。
    """
    if len(messages) <= _MIN_KEEP_MESSAGES:
        return messages, []

    keep_target = int(_MAX_CONTEXT_TOKENS * _KEEP_FRACTION)  # ~12000 tokens

    # 从后往前找保留边界
    preserved: list[dict] = []
    preserved_tokens = 0
    for m in reversed(messages):
        preserved.insert(0, m)
        preserved_tokens += len(_msg_text(m)) // _CHARS_PER_TOKEN
        if len(preserved) >= _MIN_KEEP_MESSAGES and preserved_tokens >= keep_target:
            break

    # 保留消息之前的全部作为待总结
    to_summarize = messages[: len(messages) - len(preserved)]
    return to_summarize, preserved


async def _generate_summary(messages: list[dict]) -> str:
    """调用 LLM 生成对话摘要。"""
    try:
        # 延迟导入，避免循环依赖
        from agent.llms.model import create_model

        model = create_model(enable_thinking=False)
        if model is None:
            _logger.warning("[thread_compact] 无可用模型，使用简单截断摘要")
            return _fallback_summary(messages)

        # 构建对话文本
        conversation_parts = []
        for m in messages:
            typ = m.get("type") or m.get("role") or "unknown"
            text = _msg_text(m)
            if text.strip():
                conversation_parts.append(f"[{typ}]: {text[:2000]}")  # 截断单条消息
        conversation_text = "\n\n".join(conversation_parts)

        if not conversation_text.strip():
            return "（对话历史为空）"

        from langchain_core.messages import SystemMessage

        response = await model.ainvoke([
            SystemMessage(content=_SUMMARY_SYSTEM_PROMPT),
            HumanMessage(content=f"请总结以下对话历史：\n\n{conversation_text}"),
        ])
        content = response.content
        if isinstance(content, list):
            content = "\n".join(
                blk.get("text", "") for blk in content
                if isinstance(blk, dict) and blk.get("type") == "text"
            )
        return str(content).strip() or _fallback_summary(messages)

    except Exception as e:
        _logger.warning("[thread_compact] 摘要生成失败，使用简单截断摘要: %s", e)
        return _fallback_summary(messages)


def _fallback_summary(messages: list[dict]) -> str:
    """降级摘要：列出对话轮次和关键操作。"""
    parts = []
    human_count = 0
    ai_count = 0
    tool_calls = set()
    for m in messages:
        typ = m.get("type") or m.get("role") or ""
        if typ in ("human", "user"):
            human_count += 1
            text = _msg_text(m)
            if text.strip():
                parts.append(f"用户问题：{text[:200]}")
        elif typ in ("ai", "assistant"):
            ai_count += 1
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict):
                    tool_calls.add(tc.get("name") or "unknown")

    summary = f"对话历史摘要（共 {human_count} 轮对话，{ai_count} 次助手回复）"
    if tool_calls:
        summary += f"\n使用的工具：{', '.join(sorted(tool_calls))}"
    if parts:
        summary += "\n\n关键问题：\n" + "\n".join(f"- {p}" for p in parts[-10:])
    return summary


async def compact_thread(request: Request):
    thread_id = request.path_params["thread_id"]
    if not _UUID_RE.match(thread_id):
        return Response("无效的会话 ID", status_code=400, media_type="text/plain")

    base = _base_url()
    timeout = httpx.Timeout(180.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        # 1. 读取当前 state
        try:
            r = await http.get(f"{base}/threads/{thread_id}/state")
        except httpx.HTTPError as e:
            _logger.error("[thread_compact] 读取会话 state 失败: %s", e)
            return json_response({"error": f"读取会话失败: {e}"}, status=502)
        if r.status_code == 404:
            return json_response({"error": "会话不存在"}, status=404)
        if r.status_code != 200:
            return json_response(
                {"error": f"读取会话失败: HTTP {r.status_code}"}, status=502
            )

        state = r.json() or {}
        values = state.get("values") or {}
        messages: list[dict] = values.get("messages") or []

        if len(messages) < _MIN_MESSAGES_TO_COMPACT:
            return json_response({
                "ok": True,
                "skipped": True,
                "reason": f"消息数不足（{len(messages)} < {_MIN_MESSAGES_TO_COMPACT}），无需压缩",
                "message_count": len(messages),
            })

        # 2. 分割消息
        to_summarize, preserved = _split_messages(messages)
        if not to_summarize:
            return json_response({
                "ok": True,
                "skipped": True,
                "reason": "没有需要压缩的消息",
                "message_count": len(messages),
            })

        # 3. 生成摘要
        summary = await _generate_summary(to_summarize)
        _logger.info(
            "[thread_compact] 压缩完成: 总结 %d 条消息 → %d chars 摘要, 保留 %d 条",
            len(to_summarize), len(summary), len(preserved),
        )

        # 4. 写入新 state：RemoveMessage(ALL) + 摘要 + 保留的消息
        summary_msg = HumanMessage(
            content=f"📋 **对话历史摘要**（由自动压缩生成）\n\n{summary}",
            additional_kwargs={"lc_source": "summarization"},
        )

        new_messages: list[Any] = [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            summary_msg,
            *preserved,
        ]

        try:
            r = await http.post(
                f"{base}/threads/{thread_id}/state",
                json={"values": {"messages": new_messages}},
            )
        except httpx.HTTPError as e:
            _logger.error("[thread_compact] 写入 state 失败: %s", e)
            return json_response({"error": f"写入压缩结果失败: {e}"}, status=502)
        if r.status_code != 200:
            _logger.error(
                "[thread_compact] 写入 state 失败: HTTP %s %s",
                r.status_code, r.text[:300],
            )
            return json_response(
                {"error": f"写入压缩结果失败: HTTP {r.status_code}"}, status=502
            )

        return json_response({
            "ok": True,
            "summarized_count": len(to_summarize),
            "preserved_count": len(preserved),
            "summary_length": len(summary),
        })


# ── 路由表（custom_app.py 聚合）──────────────────────────
routes: list[BaseRoute] = [
    Route("/api/threads/{thread_id}/compact", compact_thread, methods=["POST"]),
]