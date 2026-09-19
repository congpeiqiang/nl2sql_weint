"""ModelTimeoutMiddleware — LLM 调用超时 → 前端友好中文提示。

场景：模型 API 在限定时间内无响应（httpx ReadTimeout / openai APITimeoutError，
消息常为 "Request timed out."）。与额度耗尽一样，后端原始异常会被 LangGraph
序列化进 run stream 的 error 事件，而前端 SDK 不渲染 stream.error —— 表现为
「卡住、无提示」（trace ef83792 实证：model 节点 4×60s 重试后超时，界面空白）。

因此本中间件与 QuotaErrorMiddleware 同构：**不抛异常**，捕获超时类错误后返回
一条带友好中文文案的 AIMessage。agent 正常结束、消息写入 checkpoint、前端按
普通 AI 回复渲染 → 用户直接看到「模型调用超时…」，界面不卡。

边界：
- 只翻译「模型调用超时」，其他错误原样上抛（保持原有失败语义）。
- 本中间件包在模型调用边界（wrap_model_call），业务 SQL 报错不会以异常形式
  出现在这里，不会误伤。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")

# ── 超时友好提示（前端直接展示）──────────────────────────────
MODEL_TIMEOUT_MESSAGE = (
    "模型调用超时：AI 服务在限定时间内没有返回响应（常见于模型服务繁忙、网络波动，"
    "或推理模型处理大上下文时首字延迟过高）。本轮未能生成回答，请稍后重试；"
    "若反复超时，可在「模型配置」换一个模型。"
)

# ── 超时识别 ─────────────────────────────────────────────────
# httpx / openai SDK 的标准超时消息；覆盖 ReadTimeout/ConnectTimeout/WriteTimeout/
# PoolTimeout/APITimeoutError 等（它们 __str__ 都是 "Request timed out."）。
_TIMEOUT_RE = re.compile(
    r"(request|read|connect|write|pool)?\s*(timed\s?out|time\s*out)",
    re.IGNORECASE,
)


def is_timeout_error(exc: BaseException) -> bool:
    """判断异常是否为「模型/传输层超时」类错误。

    判定链（任一命中即判定）：
    1. httpx.TimeoutException（ReadTimeout 等）；
    2. 内置 TimeoutError（部分 SDK / asyncio.timeout 直接抛）；
    3. 类名含 Timeout（openai.APITimeoutError、requests.ConnectTimeout 等）；
    4. 异常链（__cause__/__context__）任一环命中上述任一条。
    """
    seen: set[int] = set()

    def _check(e: BaseException | None) -> bool:
        if e is None:
            return False
        if id(e) in seen:
            return False
        seen.add(id(e))
        try:
            import httpx
            if isinstance(e, httpx.TimeoutException):
                return True
        except Exception:  # noqa: BLE001  httpx 未安装不影响（openai 自带）
            pass
        if isinstance(e, TimeoutError):
            return True
        name = type(e).__name__
        if "Timeout" in name or "timedout" in name.lower():
            return True
        if _TIMEOUT_RE.search(str(e)):
            return True
        return _check(e.__cause__) or _check(e.__context__)

    return _check(exc)


def _friendly_response(request: ModelRequest[ContextT]) -> ModelResponse:
    """构造一条带友好文案的 AIMessage 作为模型响应（agent 正常结束，前端按普通回复渲染）。

    注意：AIMessage 不带 tool_calls → 模型节点后直接走 END，不会触发工具节点，
    也就不会因「缺工具结果」再抛错。消息会经 add_messages reducer 写入 checkpoint，
    刷新页面后仍可见。
    """
    msg = AIMessage(content=MODEL_TIMEOUT_MESSAGE)
    return ModelResponse(result=[msg], structured_response=None)


class ModelTimeoutMiddleware(AgentMiddleware[ContextT, ResponseT]):
    """把 LLM 调用超时错误转为前端可见的友好 AI 消息。

    捕获模型调用异常：超时类错误 → 返回友好 AIMessage（不抛异常，agent 正常结束，
    前端 SDK 自动渲染，界面不卡）；其他错误原样上抛（保持原有失败语义）。
    """

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """同步模型调用：超时错误 → 友好 AIMessage；其他错误原样上抛。"""
        try:
            return handler(request)
        except Exception as e:  # noqa: BLE001
            if is_timeout_error(e):
                _logger.warning("[ModelTimeout] 模型调用超时: %s", e)
                return _friendly_response(request)
            raise

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        """异步模型调用：超时错误 → 友好 AIMessage；其他错误原样上抛。"""
        try:
            result = handler(request)
            if hasattr(result, "__await__"):
                result = await result
            return result
        except Exception as e:  # noqa: BLE001
            if is_timeout_error(e):
                _logger.warning("[ModelTimeout] 模型调用超时: %s", e)
                return _friendly_response(request)
            raise
