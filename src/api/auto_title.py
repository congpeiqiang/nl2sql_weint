"""会话自动标题 API（P1-4，对标 deepseek-harness dsh-session-title）。

POST /api/auto-title  body: {"text": "<用户首条问题>"}  →  {"title": "<≤20字标题>"}

设计：
- 无状态：只把传入文本交给 LLM 生成短标题，不读 checkpointer/thread state
  （自定义 app 访问 thread state 链路复杂，故由前端把首条问题文本直接带来）。
- 前端拿到 title 后经 langgraph SDK `threads.update` 写入 thread metadata.title。
- 用独立的轻量模型实例（关思考），避免污染主 agent 单例、也更快。
"""
from __future__ import annotations

import logging
import re

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response, parse_body

_logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 500
MAX_TITLE_CHARS = 20

# 轻量模型实例（懒加载、关思考）。生成标题是极短任务，无需思考链。
_title_model = None


def _get_title_model():
    global _title_model
    if _title_model is None:
        from agent.llms.model import create_model
        _title_model = create_model(enable_thinking=False)
    return _title_model


def _clean_title(raw: str) -> str:
    """去掉引号/多余空白/换行，截断到上限。"""
    t = (raw or "").strip()
    t = re.sub(r"^[\s\"'`《【\[]+|[\s\"'`》】\]]+$", "", t)
    t = t.replace("\n", " ").strip()
    return t[:MAX_TITLE_CHARS]


async def auto_title(request: Request):
    data = await parse_body(request)
    text = str(data.get("text", "") or "").strip()
    if not text:
        return json_response({"error": "text 必填"}, status=400)
    text = text[:MAX_INPUT_CHARS]

    model = _get_title_model()
    if model is None:
        return json_response({"error": "模型不可用"}, status=500)

    from langchain_core.messages import HumanMessage

    prompt = (
        "请把下面的用户问题概括成一个简短的对话标题，用于会话列表展示。要求：\n"
        f"- 不超过 {MAX_TITLE_CHARS} 个字\n"
        "- 只输出标题本身，不要引号、句号、解释或前缀\n"
        "- 保留关键实体（表名/指标/时间范围等）\n\n"
        f"用户问题：{text}"
    )
    try:
        resp = await model.ainvoke([HumanMessage(content=prompt)])
        content = resp.content if hasattr(resp, "content") else str(resp)
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in content
            )
        title = _clean_title(str(content))
    except Exception as e:  # noqa: BLE001
        _logger.warning("[auto_title] 生成失败，回退截断: %s", e)
        title = text[:MAX_TITLE_CHARS]

    if not title:
        title = text[:MAX_TITLE_CHARS]
    return json_response({"title": title})


# ── 路由表（custom_app.py 聚合）──────────────────────────
routes: list[BaseRoute] = [
    Route("/api/auto-title", auto_title, methods=["POST"]),
]
