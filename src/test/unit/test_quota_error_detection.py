"""QuotaErrorMiddleware 的额度错误识别。

2026-10-20 事故（会话 01a158a1）：网关返回

    429 {'error': {'message': 'The budget limit configured by the customer has
    been reached. ...', 'type': 'BudgetLimitExceeded'}}

全文既无 quota 也无 balance，旧关键词表全部漏判 ⇒ 异常原样上抛、前端只显示
英文原文。本文件的用例锁住「budget 类错误必须被翻译」。
"""

import asyncio

from langchain_core.messages import AIMessage

from agent.middlewares.quota_error import (
    QUOTA_EXHAUSTED_MESSAGE,
    QuotaErrorMiddleware,
    is_quota_error,
)
from agent.utils.failure_signal import FAILURE_KEY, KIND_QUOTA_EXHAUSTED

# 生产原始异常文本（网关原文，一字未改）
_BUDGET_MSG = (
    "Error code: 429 - {'error': {'message': 'The budget limit configured by the "
    "customer has been reached. API calls cannot continue until the budget limit "
    "is increased or reset. For details, see: https://example.com/help', "
    "'type': 'BudgetLimitExceeded', 'param': '', 'code': 'BudgetLimitExceeded'}}"
)


class _FakeExc(Exception):
    """带 status_code 的异常（模拟 openai.RateLimitError 的默认属性）。"""

    def __init__(self, text: str, status_code: int | None = None) -> None:
        super().__init__(text)
        if status_code is not None:
            self.status_code = status_code


class TestBudgetLimitDetection:
    """网关 budget 类文案必须判为额度错误。"""

    def test_budget_limit_with_429_status(self):
        assert is_quota_error(_FakeExc(_BUDGET_MSG, 429)) is True

    def test_budget_limit_text_only(self):
        """部分 SDK 只在文本里带码，没有 status_code 属性。"""
        assert is_quota_error(_FakeExc(_BUDGET_MSG)) is True

    def test_budget_limit_phrase_alone(self):
        """`budget limit` 是强关键词，无需状态码佐证。"""
        assert is_quota_error(_FakeExc("BudgetLimitExceeded: budget limit reached")) is True

    def test_bare_budget_without_status_is_not_quota(self):
        """弱关键词 `budget` 单独出现（如「维度预算」）不得误判。"""
        assert is_quota_error(_FakeExc("候选维度太多，超出预算 budget")) is False

    def test_existing_strong_keywords_still_work(self):
        assert is_quota_error(_FakeExc("Insufficient Balance")) is True
        assert is_quota_error(_FakeExc("insufficient_quota")) is True
        assert is_quota_error(_FakeExc("额度已用尽")) is True

    def test_auth_error_is_not_quota(self):
        """401 鉴权失败不能被当成额度问题（否则用户会去充值）。"""
        assert is_quota_error(_FakeExc("Error code: 401 - invalid_api_key", 401)) is False

    def test_plain_sql_error_is_not_quota(self):
        assert is_quota_error(_FakeExc("syntax error at or near \"SELECT\"")) is False


class TestQuotaErrorMiddleware:
    """额度错误 → 友好 AIMessage + 失败戳；其他错误原样上抛。"""

    def _raise_with(self, exc: BaseException):
        def _handler(_request):
            raise exc

        async def _run():
            return await QuotaErrorMiddleware().awrap_model_call(object(), _handler)

        return asyncio.run(_run())

    def test_budget_limit_turns_into_friendly_message(self):
        resp = self._raise_with(_FakeExc(_BUDGET_MSG, 429))
        msg = resp.result[0]
        assert isinstance(msg, AIMessage)
        assert msg.content == QUOTA_EXHAUSTED_MESSAGE
        assert "额度" in msg.content
        mark = msg.additional_kwargs[FAILURE_KEY]
        assert mark["kind"] == KIND_QUOTA_EXHAUSTED
        assert mark["detail"] == QUOTA_EXHAUSTED_MESSAGE

    def test_non_quota_error_propagates(self):
        exc = _FakeExc("Error code: 500 - upstream boom", 500)
        try:
            self._raise_with(exc)
        except _FakeExc as raised:
            assert raised is exc
        else:  # pragma: no cover - 失败路径
            raise AssertionError("非额度错误必须原样上抛，不能吞成友好文案")
