"""QuotaErrorMiddleware — LLM 额度耗尽错误 → 前端友好中文提示。

需求：模型账号额度耗尽时（DeepSeek 等返回 HTTP 402/403，如 "Free quota
exhausted" / "insufficient_quota" / "Insufficient Balance"），后端原始异常
会被 LangGraph 序列化进 run stream 的 error 事件。但前端 SDK（useStream）
只把 error 放进 stream.error 状态，聊天界面不渲染它——表现为「卡住、无提示」。

因此本中间件**不抛异常**，而是在捕获额度类错误后返回一条带友好中文文案的
AIMessage（ModelResponse）：agent 正常结束、消息写入 checkpoint、前端 SDK
按普通 AI 回复渲染 → 用户直接看到「模型额度已用完…」提示，界面不卡。

约束（P1-9 配置权威性）：本中间件**只翻译错误文案**，不读取/回退 .env 的
LLM_*——模型配置唯一来源仍是前端 CRUD 的 model_config.json。

非 agent 场景（auto_title / thread_compact 等直接调用模型、自带 try/except
降级）不走本中间件；QuotaExhaustedError 供那些调用点手动翻译用。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")

# ── 额度耗尽友好提示（前端直接展示）──────────────────────────────
QUOTA_EXHAUSTED_MESSAGE = (
    "模型额度已用完（API 返回额度/余额不足，如 402/403 insufficient_quota）。"
    "请到「模型配置」更换该模型的 API Key，或为该账号充值后重试。"
)

# ── 额度/余额错误识别关键词（大小写不敏感）────────────────────────
# 强关键词：单独出现即可判定（供应商直出消息或 openai SDK 包装消息）
_QUOTA_STRONG_KEYWORDS = (
    "insufficient_quota",
    "quota exhausted",
    "quota has been exhausted",
    "free quota",
    "out of quota",
    "insufficient balance",
    "balance insufficient",
    "no enough balance",
    "额度",
    "余额不足",
    "欠费",
)
# 弱关键词：必须同时见到 HTTP 状态码（402/403/429）才判定，
# 避免把含 "quota/balance" 的业务文本（如 SQL 报错）误判为额度问题
_QUOTA_WEAK_KEYWORDS = ("quota", "balance", "余额")

# 命中这些 HTTP 状态码且消息含额度关键词时才翻译（避免误伤其他 403 鉴权错误）
_QUOTA_STATUS_CODES = (402, 403, 429)


class QuotaExhaustedError(RuntimeError):
    """模型额度耗尽（带友好中文消息，LangGraph 会将其序列化到 stream error）。"""


def is_quota_error(exc: BaseException) -> bool:
    """判断异常是否为「额度/余额不足」类错误。

    策略：
    - 强关键词（insufficient_quota / quota exhausted / free quota / 额度 等）
      单独命中即判定（供应商直出消息，如 "Free quota exhausted."）；
    - 弱关键词（quota / balance / 余额）需同时见到 HTTP 状态码 402/403/429
      （文本中或异常的 status_code 属性），避免把含 "balance" 的业务文本误判；
    - 其他错误（401 鉴权失败、纯 SQL 错误等）原样透传。
    """
    text = str(exc)
    lower = text.lower()

    status = getattr(exc, "status_code", None)
    status_hint = status in _QUOTA_STATUS_CODES or any(
        f" {code}" in lower or f"status: {code}" in lower or f"status_code={code}" in lower
        for code in _QUOTA_STATUS_CODES
    )

    # 强关键词单独命中 → 判定（无需状态码佐证）
    if any(k in lower for k in _QUOTA_STRONG_KEYWORDS):
        return True

    # 弱关键词 + 状态码 402/403/429 → 判定
    if status_hint and any(k in lower for k in _QUOTA_WEAK_KEYWORDS):
        return True

    return False


def _translate_quota_error(exc: BaseException) -> BaseException | None:
    """额度类错误 → QuotaExhaustedError（保留原始异常为 cause，便于追踪）；非额度错误返回 None。"""
    if is_quota_error(exc):
        _logger.warning("[QuotaError] 检测到 LLM 额度耗尽: %s", exc)
        return QuotaExhaustedError(QUOTA_EXHAUSTED_MESSAGE)
    return None


def _friendly_response(request: ModelRequest[ContextT]) -> ModelResponse:
    """构造一条带友好文案的 AIMessage 作为模型响应（agent 正常结束，前端按普通回复渲染）。

    注意：AIMessage 不带 tool_calls → 模型节点后直接走 END，不会触发工具节点，
    也就不会因「缺工具结果」再抛错。消息会经 add_messages reducer 写入 checkpoint，
    刷新页面后仍可见。
    """
    msg = AIMessage(content=QUOTA_EXHAUSTED_MESSAGE)
    # 保留请求里的 id 前缀，便于前端按消息顺序渲染（可选）
    return ModelResponse(result=[msg], structured_response=None)


class QuotaErrorMiddleware(AgentMiddleware[ContextT, ResponseT]):
    """把 LLM 额度耗尽错误转为前端可见的友好 AI 消息（不改配置来源）。

    捕获模型调用异常：额度类错误 → 返回友好 AIMessage（不抛异常，agent 正常结束，
    前端 SDK 自动渲染，界面不卡）；其他错误原样上抛（保持原有失败语义）。
    """

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """同步模型调用：额度类错误 → 友好 AIMessage；其他错误原样上抛。"""
        try:
            return handler(request)
        except Exception as e:  # noqa: BLE001
            translated = _translate_quota_error(e)
            if translated is not None:
                return _friendly_response(request)
            raise

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        """异步模型调用：额度类错误 → 友好 AIMessage；其他错误原样上抛。"""
        try:
            result = handler(request)
            if hasattr(result, "__await__"):
                result = await result
            return result
        except Exception as e:  # noqa: BLE001
            translated = _translate_quota_error(e)
            if translated is not None:
                return _friendly_response(request)
            raise
