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
#
# 2026-09-28 按账号隔离：以前是**单个模块级实例**，即 `create_model()` 不带 user_id
# ⇒ 读全局/共享 store —— 新账号没配模型时，这里会拿**别账号的 key** 代它生成标题
# （额度记在别人名下）。现改为「按 user_id 缓存」，每个账号只用自己的配置。
# 上限 _TITLE_MODEL_CACHE_MAX：登录账号数可能很大，不能让缓存无界增长
# （超出后整体清空重来，淘汰策略够用且实现简单）。
_title_models: dict[str, object | None] = {}
_TITLE_MODEL_CACHE_MAX = 64


def _get_title_model(user_id: str = ""):
    """取该账号的标题模型实例（懒加载、关思考）；该账号没配模型时返回 None。

    ⚠️ `user_id` 为空串时 `create_model` 落到全局 store（无登录身份的老行为，
    AUTH_DISABLED 本地开发场景）；带身份的正常请求必须传真实 `user_id`。
    """
    key = user_id or ""
    if key in _title_models:
        return _title_models[key]
    from agent.llms.model import create_model

    model = create_model(enable_thinking=False, user_id=user_id or None)
    if len(_title_models) >= _TITLE_MODEL_CACHE_MAX:
        _title_models.clear()
    _title_models[key] = model
    return model


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

    # 按账号取模型（2026-09-28 隔离）：新账号没配模型时 error 返回 → 前端沿用
    # 「用问题原文当标题」的既有降级，不会显示别的账号生成的标题。
    from api._common import require_user

    model = _get_title_model(require_user(request)["user_id"])
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
