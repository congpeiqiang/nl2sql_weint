"""TokenMeterMiddleware — 采集每次 LLM 调用的 token 用量和耗时，累积到 state。

对标 deepseek-harness 的 session-stats projection，在 LangGraph middleware 层打点：
- wrap_model_call：记录 LLM 耗时（wall-clock），从 AIMessage.usage_metadata 提取
  input/output/cache_read/reasoning tokens，通过 ExtendedModelResponse.command 写入 state。
- 累积由 state reducer _accumulate_token_stats 完成，支持多步/多轮自动累加。

state 字段 token_stats 结构：
{
    "total_llm_ms": 5000,        # LLM 调用总耗时（毫秒）
    "total_input_tokens": 10000,
    "total_output_tokens": 800,
    "total_cache_read_tokens": 3000,
    "total_reasoning_tokens": 0,
    "step_count": 3,             # LLM 调用次数
    "steps": [...]               # 每步详情
}
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, TypeVar

from langchain.agents.middleware import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langgraph.types import Command

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")


def _accumulate_token_stats(
    existing: dict[str, Any] | None, update: dict[str, Any],
) -> dict[str, Any]:
    """按 key 累加 token 统计（LangGraph reducer）。

    数值字段累加，step_count 递增，steps 追加。
    """
    merged: dict[str, Any] = dict(existing or {})
    numeric_keys = (
        "total_llm_ms",
        "total_input_tokens",
        "total_output_tokens",
        "total_cache_read_tokens",
        "total_reasoning_tokens",
    )
    for key in numeric_keys:
        merged[key] = merged.get(key, 0) + update.get(key, 0)
    merged["step_count"] = merged.get("step_count", 0) + 1
    steps: list[dict[str, Any]] = list(merged.get("steps", []))
    steps.append(update)
    # 保留最近 50 步，避免 state 膨胀
    if len(steps) > 50:
        steps = steps[-50:]
    merged["steps"] = steps
    return merged


class TokenMeterMiddleware(AgentMiddleware):
    """在每次 LLM 调用时采集 usage_metadata 和耗时。"""

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | ExtendedModelResponse[ResponseT]:
        """同步 LLM 调用：计时 → 调用 → 提取 usage_metadata → 写 state。"""
        round_index = _current_round(request)
        t0 = time.time()
        try:
            response = handler(request)
        except Exception:
            raise  # 不吞异常，计时失败不影响 agent 循环
        elapsed_ms = int((time.time() - t0) * 1000)

        usage = _extract_usage(response)
        if usage is None:
            return response

        step_stat = _build_step_stat(elapsed_ms, usage, round_index)
        _logger.debug(
            "[TokenMeter] round=%s step=%s llm_ms=%d in=%d out=%d cache=%d reasoning=%d",
            round_index,
            step_stat.get("step", 0),
            elapsed_ms,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            (usage.get("input_token_details") or {}).get("cache_read", 0),
            (usage.get("output_token_details") or {}).get("reasoning_tokens", 0),
        )
        return ExtendedModelResponse(
            model_response=response,
            command=Command(update={"token_stats": step_stat}),
        )

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT] | ExtendedModelResponse[ResponseT]:
        """异步 LLM 调用：计时 → 调用 → 提取 usage_metadata → 写 state。"""
        round_index = _current_round(request)
        t0 = time.time()
        try:
            result = handler(request)
            if hasattr(result, "__await__"):
                result = await result
        except Exception:
            raise
        elapsed_ms = int((time.time() - t0) * 1000)

        usage = _extract_usage(result)
        if usage is None:
            return result

        step_stat = _build_step_stat(elapsed_ms, usage, round_index)
        _logger.debug(
            "[TokenMeter] round=%s step=%s llm_ms=%d in=%d out=%d cache=%d reasoning=%d",
            round_index,
            step_stat.get("step", 0),
            elapsed_ms,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            (usage.get("input_token_details") or {}).get("cache_read", 0),
            (usage.get("output_token_details") or {}).get("reasoning_tokens", 0),
        )
        return ExtendedModelResponse(
            model_response=result,
            command=Command(update={"token_stats": step_stat}),
        )


def _extract_usage(response: ModelResponse[ResponseT]) -> dict[str, Any] | None:
    """从 ModelResponse 的 AIMessage 中提取 usage_metadata。"""
    if not isinstance(response, ModelResponse):
        return None
    for msg in getattr(response, "result", []) or []:
        usage = getattr(msg, "usage_metadata", None)
        if usage:
            return usage
    return None


def _build_step_stat(
    elapsed_ms: int, usage: dict[str, Any], round_index: int = 0,
) -> dict[str, Any]:
    """构建单步统计 dict。round_index 为当前轮号（1-based，0 = 无法判定）。"""
    input_details = usage.get("input_token_details") or {}
    output_details = usage.get("output_token_details") or {}
    return {
        "total_llm_ms": elapsed_ms,
        "total_input_tokens": usage.get("input_tokens", 0),
        "total_output_tokens": usage.get("output_tokens", 0),
        "total_cache_read_tokens": input_details.get("cache_read", 0),
        "total_reasoning_tokens": output_details.get("reasoning_tokens", 0),
        "round_index": round_index,
    }


def _current_round(request: ModelRequest[ContextT]) -> int:
    """从 request.state 数 human 消息数得到当前轮号（1-based）。

    ModelRequest.state 由 factory 构造时传入完整 state（含 messages），
    因此 wrap_model_call 里可直接读 request.state["messages"]（不含 system）。
    若拿不到 messages 或异常，返回 0（前端按「无轮号」处理，不计入任何轮）。
    """
    try:
        state = getattr(request, "state", None) or {}
        if not isinstance(state, dict):
            return 0
        messages = state.get("messages") or []
        human_count = sum(
            1 for m in messages if getattr(m, "type", "") == "human"
        )
        return human_count or 0
    except Exception:  # noqa: BLE001
        return 0