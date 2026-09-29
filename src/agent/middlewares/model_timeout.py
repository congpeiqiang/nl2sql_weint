"""ModelTimeoutMiddleware — LLM 调用超时 → 前端友好中文提示 + 韧性（P2-11/P3-4）。

场景：模型 API 在限定时间内无响应（httpx ReadTimeout / openai APITimeoutError，
消息常为 "Request timed out."）。与额度耗尽一样，后端原始异常会被 LangGraph
序列化进 run stream 的 error 事件，而前端 SDK 不渲染 stream.error —— 表现为
「卡住、无提示」（trace ef83792 实证：model 节点 4×60s 重试后超时，界面空白）。

因此本中间件与 QuotaErrorMiddleware 同构：**不抛异常**，捕获超时类错误后返回
一条带友好中文文案的 AIMessage。agent 正常结束、消息写入 checkpoint → 用户直接
看到「模型调用超时…」，界面不卡。

⚠️ 2026-09-28 补：这条消息**同时盖失败戳**（`agent/utils/failure_signal.py`）。
「不抛异常」的代价是这条消息与正常回答无法区分，曾导致下游三处各自猜错（CaliberGate
把超时当没写完的终稿打回模型、白烧第二个 240s，`check_async_task` 报 `success`）。
戳是附加信息，**文案一字符不改**。

边界：
- 只翻译「模型调用超时」，其他错误原样上抛（保持原有失败语义）。
- 本中间件包在模型调用边界（wrap_model_call），业务 SQL 报错不会以异常形式
  出现在这里，不会误伤。

P2-3：这里同时是**模型调用的唯一收口点**（主 agent 与子 agent 的图都注册了它），
所以顺手把「结果三态 + 耗时」记进 prometheus（`nl2sql_llm_calls_total` /
`nl2sql_llm_latency_seconds`）。埋点走 `note_llm_call`，它自己不抛异常 ——
指标坏了不能让问答跟着挂。计时覆盖**同步与 await 两段**（异步 handler 的
真耗时在 await 里，只量 `handler(request)` 会量成 0）。

P2-11 + P3-4（2026-09-24，P2-7 压测驱动）：收口点同时长出三件事，判定与实现都在
`agent/utils/llm_gate.py`，这里只负责**接线**：

1. **异常链归因**（P2-11）：非超时异常原来只上抛、服务端不记日志，于是压测里
   那 3 次 `openai.APIConnectionError` 只留下 SDK 自己的 warning，看不到
   `__cause__`（真正的原因在里层）。现在每次失败都 ERROR 记
   `describe_exception_chain(e)`。
2. **连接类错误带抖动退避重试**（P3-4）：SDK 自带的 `max_retries=3` 在生产上**已
   全部用尽**，这一层是更慢的第二道，默认只加 1 次。
3. **并发闸 + 按用户配额**（P3-4）：模型调用逐个取槽（`model_call_slot`），
   排队超预算 **fail-open 放行**（闸不能自己变成故障）。

⚠️ 计数口径：每次**真实尝试**都记一次 `note_llm_call`，所以「重试后成功」的调用会
在 `nl2sql_llm_calls_total{outcome="error"}` 上留一笔。那**不等于**用户可见失败数
（用户没感知），要区分就看 `nl2sql_llm_retries_total{outcome="recovered"}`。

⚠️ 兼容：`is_timeout_error` 的实现已挪到 `agent/utils/llm_gate.py`（那里统一做
模型调用异常归类），本模块**原样再导出** —— 对外名字不变，别在本文件里另写一份。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

from agent.utils.failure_signal import KIND_MODEL_TIMEOUT, mark_failed
from agent.utils.llm_gate import (
    backoff_seconds,
    describe_exception_chain,
    is_connection_error,
    is_timeout_error,  # noqa: F401  再导出（对外仍从本模块可用）
    model_call_slot,
    model_call_slot_sync,
    retry_attempts,
)
from agent.utils.prom_metrics import note_llm_call, note_llm_retry

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")

# ── 超时友好提示（前端直接展示）──────────────────────────────
MODEL_TIMEOUT_MESSAGE = (
    "模型调用超时：AI 服务在限定时间内没有返回响应（常见于模型服务繁忙、网络波动，"
    "或推理模型处理大上下文时首字延迟过高）。本轮未能生成回答，请稍后重试；"
    "若反复超时，可在「模型配置」换一个模型。"
)


def _friendly_response(request: ModelRequest[ContextT]) -> ModelResponse:
    """构造一条带友好文案的 AIMessage 作为模型响应（agent 正常结束，前端渲染这张卡片）。

    注意：AIMessage 不带 tool_calls → 模型节点后直接走 END，不会触发工具节点，
    也就不会因「缺工具结果」再抛错。消息会经 add_messages reducer 写入 checkpoint，
    刷新页面后仍可见。

    ⚠️ 同时要盖 `failure_signal` 的失败戳（2026-09-28 生产事故的根因）：不盖戳时，
    这条消息与「模型真的答完了」在数据上无法区分，下游会各自猜错 ——
    `CaliberGateMiddleware` 把它当成「终稿缺业务口径块」打回模型（白烧第二个 240s）、
    `check_async_task` 把它当 `status: "success"` 报给主 agent、watcher 与 run 各记一套
    终态。**文案本身一个字符都不改**（既有断言按内容比较，文案是用户可见契约）。

    消费入口（语义一致、入口不同）：子图由 watcher + `check_async_task` 读戳
    （`async_tasks.status` 落失败态、走失败汇报）；主图由 `/api/threads/{tid}/run-status`
    读戳（`turn_failed` ⇒ 前端「上一轮执行失败」横幅 + 重试按钮）。
    """
    msg = mark_failed(
        AIMessage(content=MODEL_TIMEOUT_MESSAGE),
        KIND_MODEL_TIMEOUT,
        MODEL_TIMEOUT_MESSAGE,
    )
    return ModelResponse(result=[msg], structured_response=None)


def _classify_failure(
    exc: BaseException, *, attempt: int, attempts: int, started: float,
) -> tuple[str, float]:
    """记指标 + 日志，并给出**下一步动作**：`("friendly"|"retry"|"raise", 退避秒)`。

    判定顺序**必须是超时优先**：`openai.APITimeoutError` 是 `APIConnectionError` 的
    子类，反过来判会把「对端慢」当「连接断」去重试，用户白等几个 60s。
    """
    elapsed = time.perf_counter() - started
    if is_timeout_error(exc):
        note_llm_call("timeout", elapsed)
        _logger.warning(
            "[ModelTimeout] 模型调用超时（第 %d/%d 次尝试，%.1fs）: %s | %s",
            attempt + 1, attempts + 1, elapsed, exc, describe_exception_chain(exc),
        )
        return "friendly", 0.0

    note_llm_call("error", elapsed)
    retryable = is_connection_error(exc)
    if retryable and attempt < attempts:
        delay = backoff_seconds(attempt)
        _logger.warning(
            "[ModelRetry] 连接类错误（第 %d/%d 次尝试，%.1fs），%.2fs 后重试 | %s",
            attempt + 1, attempts + 1, elapsed, delay, describe_exception_chain(exc),
        )
        return "retry", delay

    if retryable:
        note_llm_retry("exhausted")
    # P2-11：这就是压测里缺的那条日志 —— 只上抛不记，等于没有诊断信息。
    _logger.error(
        "[ModelCall] 模型调用失败（第 %d/%d 次尝试，%.1fs），异常链: %s",
        attempt + 1, attempts + 1, elapsed, describe_exception_chain(exc),
    )
    return "raise", 0.0


class ModelTimeoutMiddleware(AgentMiddleware[ContextT, ResponseT]):
    """把 LLM 调用超时错误转为前端可见的友好 AI 消息；并做归因/退避/并发闸。

    捕获模型调用异常：超时类错误 → 返回友好 AIMessage（不抛异常，agent 正常结束，
    前端 SDK 自动渲染，界面不卡）；连接类错误 → 带抖动退避重试若干次（默认 1 次）；
    其他错误原样上抛（保持原有失败语义）。

    每次**尝试**都单独取一次并发闸：退避睡眠在闸外（槽已释放），否则一个退避中的
    调用会白占着额度把别人堵住。
    """

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """同步模型调用：超时 → 友好 AIMessage；连接类 → 退避重试；其他原样上抛。"""
        attempts = retry_attempts()
        started = time.perf_counter()
        for attempt in range(attempts + 1):
            backoff: float | None = None
            with model_call_slot_sync():
                try:
                    result = handler(request)
                except Exception as e:  # noqa: BLE001
                    action, delay = _classify_failure(
                        e, attempt=attempt, attempts=attempts, started=started,
                    )
                    if action == "friendly":
                        return _friendly_response(request)
                    if action == "retry":
                        backoff = delay
                    else:
                        raise
                else:
                    note_llm_call("ok", time.perf_counter() - started)
                    if attempt:
                        note_llm_retry("recovered")
                    return result
            if backoff is not None:
                time.sleep(backoff)
        raise RuntimeError("模型调用重试循环异常退出")  # pragma: no cover  上面每轮都 return/raise

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        """异步模型调用：超时 → 友好 AIMessage；连接类 → 退避重试；其他原样上抛。"""
        attempts = retry_attempts()
        started = time.perf_counter()
        for attempt in range(attempts + 1):
            backoff: float | None = None
            async with model_call_slot():
                try:
                    result = handler(request)
                    if hasattr(result, "__await__"):
                        result = await result
                except Exception as e:  # noqa: BLE001
                    action, delay = _classify_failure(
                        e, attempt=attempt, attempts=attempts, started=started,
                    )
                    if action == "friendly":
                        return _friendly_response(request)
                    if action == "retry":
                        backoff = delay
                    else:
                        raise
                else:
                    note_llm_call("ok", time.perf_counter() - started)
                    if attempt:
                        note_llm_retry("recovered")
                    return result
            if backoff is not None:
                await asyncio.sleep(backoff)
        raise RuntimeError("模型调用重试循环异常退出")  # pragma: no cover
