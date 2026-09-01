"""MessageSlimmerMiddleware — 主动瘦身 ToolMessage（截断超大 + 去重完全重复）。

方案 B 第一阶段（L1，低风险高收益）：解决 LangGraph checkpoint 累积膨胀。
每个 checkpoint 都存「截至当前的完整消息列表」（O(n²) 膨胀），而最大头是
超大 tool 结果（实测线程里单条 73.4KB read_file 占消息总大小 24%）与
完全重复的 tool 结果（2×14KB write_todos + 2×8.8KB SKILL 读取）。

两个动作都在工具结果进入 state/checkpoint 之前改写（wrap_tool_call / awrap_tool_call）：

1. **截断超大 tool 结果**：文本超过阈值（默认 16000 字符）的 ToolMessage，
   把完整内容落盘到 `large_tool_results/<tool_call_id>`，消息内只保留
   head+tail 预览 + 路径指针（复用 deepagents 的 `_offload_tool_message_content`）。
   覆盖 read_file / execute 等 deepagents 主动驱逐豁免的工具——那些正是本系统膨胀源。

2. **去重完全重复**：新 ToolMessage 文本与线程内历史某条 ToolMessage 完全相同时，
   把内容替换成小占位（引用首次出现的 tool_call_id），**保留 tool_call_id / name / id**，
   LangGraph 的 tool_call 配对与前端 deriveStepsFromSubMessages 的步骤关联都不受影响。

安全设计：
- 全程 fail-open：任何异常只记日志并返回原始结果，绝不阻断 agent 循环。
- 不触碰 AI/Human 消息（AI 消息瘦身属 L2，暂缓）。
- 阈值/开关由构造参数控制，可在 main_agent.py 调大或置 None 关闭。
"""
from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage, RemoveMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware._message_eviction import (
    _extract_text_from_message,
    _aoffload_tool_message_content,
    _offload_tool_message_content,
)

if TYPE_CHECKING:
    from langgraph.graph.message import REMOVE_ALL_MESSAGES  # noqa: F401

_logger = logging.getLogger(__name__)

# 默认截断阈值（字符数）。16000 字符 ≈ 4k token，足以覆盖 73KB read_file、
# 超长 execute 输出等真实膨胀源；8.8KB 的 SKILL.md 读取与 14KB 的 write_todos
# 结果都低于该阈值，不会被截断（重复场景交给去重处理）。
_DEFAULT_MAX_CHARS_BEFORE_TRUNCATE = 16_000

# 去重占位文本。tool_call_id 保持原值，这里引用首次出现的那条。
_DEDUP_STUB = (
    "[内容重复已省略] 此工具结果与线程内先前同名工具结果完全相同"
    "（首次出现于 tool_call_id: {first_id}），不再重复展示。"
    "如需完整内容，请向上查阅历史中的该次结果。"
)


def _text_md5(text: str) -> str:
    """计算文本内容的 md5，用于完全重复检测。"""
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()


def _state_messages(state: Any) -> list[Any]:
    """从 Agent state（dict 或 BaseModel）中安全取出 messages 列表。"""
    if isinstance(state, dict):
        return list(state.get("messages", []) or [])
    return list(getattr(state, "messages", []) or [])


def _unwrap_command_messages(update: dict[str, Any]) -> tuple[list[Any], bool]:
    """从 Command update 中取出消息列表，并检测 REMOVE_ALL_MESSAGES 哨兵。"""
    command_messages = update.get("messages", [])
    if (
        isinstance(command_messages, list)
        and command_messages
        and isinstance(command_messages[0], RemoveMessage)
    ):
        from langgraph.graph.message import REMOVE_ALL_MESSAGES

        if command_messages[0].id == REMOVE_ALL_MESSAGES:
            return command_messages[1:], True
    return command_messages, False


def _rewrap_command_messages(messages: list[Any], *, wrapped: bool) -> list[Any]:
    """还原 REMOVE_ALL_MESSAGES 哨兵。"""
    if wrapped:
        from langgraph.graph.message import REMOVE_ALL_MESSAGES

        return [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]
    return list(messages)


ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")


class MessageSlimmerMiddleware(AgentMiddleware):
    """在工具结果进入 state 前，截断超大 tool 结果并去重完全重复的 tool 结果。"""

    def __init__(
        self,
        *,
        backend: BackendProtocol | None = None,
        max_chars_before_truncate: int | None = _DEFAULT_MAX_CHARS_BEFORE_TRUNCATE,
    ) -> None:
        """初始化。

        Args:
            backend: 落盘超大工具结果用的后端（如 main_agent 的 composite_backend）。
                None 时仍可去重，但超大结果不落盘（保持原样，等价只做去重）。
            max_chars_before_truncate: 触发截断的文本字符阈值；None 关闭截断（只去重）。
        """
        self._backend = backend
        self._max_chars_before_truncate = max_chars_before_truncate

        # 超大工具结果落盘目录前缀。
        # CompositeBackend 的路由 {"/": file_backend} 会匹配所有以 "/" 开头的路径，
        # 导致 "/large_tool_results" 落到 file_backend（src/agent/large_tool_results/）。
        # 改为不以 "/" 开头的相对路径，CompositeBackend 不匹配任何路由，
        # fallback 到 default（shell_backend），落盘到 workspace/large_tool_results/。
        # 非 CompositeBackend 时保持原来 "/large_tool_results" 绝对路径。
        if isinstance(backend, CompositeBackend):
            self._large_tool_results_prefix = "large_tool_results"
        else:
            self._large_tool_results_prefix = "/large_tool_results"

    # ── 核心处理 ──────────────────────────────────────────────

    def _dedup(self, message: ToolMessage, prior_messages: list[Any]) -> ToolMessage | None:
        """与历史完全重复则返回去重占位消息；否则 None。

        只在文本 md5 完全一致且工具名相同时判定重复。返回的占位保留
        tool_call_id / name / id，LangGraph tool 配对与前端步骤关联不受影响。
        """
        content_str = _extract_text_from_message(message)
        if not content_str:
            return None
        digest = _text_md5(content_str)
        for prior in prior_messages:
            if not isinstance(prior, ToolMessage) or prior.name != message.name:
                continue
            prior_text = _extract_text_from_message(prior)
            if prior_text and _text_md5(prior_text) == digest:
                _logger.info(
                    "[MessageSlimmer] 去重 %s 结果（%d chars，首次 tool_call_id=%s）→ 占位",
                    message.name,
                    len(content_str),
                    prior.tool_call_id,
                )
                return message.model_copy(
                    update={"content": _DEDUP_STUB.format(first_id=prior.tool_call_id)}
                )
        return None

    def _should_truncate(self, content_str: str) -> bool:
        """文本是否超过截断阈值。"""
        return (
            self._max_chars_before_truncate is not None
            and len(content_str) > self._max_chars_before_truncate
        )

    def _process_tool_message_sync(
        self, message: ToolMessage, prior_messages: list[Any]
    ) -> ToolMessage:
        """同步路径：去重 → 落盘截断 → 原样返回。"""
        deduped = self._dedup(message, prior_messages)
        if deduped is not None:
            return deduped

        content_str = _extract_text_from_message(message)
        if self._backend is not None and self._should_truncate(content_str):
            try:
                processed = _offload_tool_message_content(
                    message, content_str, self._backend, self._large_tool_results_prefix
                )
                if processed is not None:
                    _logger.info(
                        "[MessageSlimmer] 截断 %s 结果（%d chars）→ 落盘 %s/...",
                        message.name, len(content_str), self._large_tool_results_prefix,
                    )
                    return processed
            except Exception as e:  # noqa: BLE001  # fail-open：落盘失败保留原结果
                _logger.warning("[MessageSlimmer] 落盘失败，保留原结果: %s", e)
        return message

    async def _process_tool_message_async(
        self, message: ToolMessage, prior_messages: list[Any]
    ) -> ToolMessage:
        """异步路径：去重 → await 落盘截断 → 原样返回。

        注意：_aoffload_tool_message_content 是协程，必须 await，
        否则协程对象会泄漏进 messages 通道导致 reducer 崩溃。
        """
        deduped = self._dedup(message, prior_messages)
        if deduped is not None:
            return deduped

        content_str = _extract_text_from_message(message)
        if self._backend is not None and self._should_truncate(content_str):
            try:
                processed = await _aoffload_tool_message_content(
                    message, content_str, self._backend, self._large_tool_results_prefix
                )
                if processed is not None:
                    _logger.info(
                        "[MessageSlimmer] 截断 %s 结果（%d chars）→ 落盘 %s/...",
                        message.name, len(content_str), self._large_tool_results_prefix,
                    )
                    return processed
            except Exception as e:  # noqa: BLE001  # fail-open：落盘失败保留原结果
                _logger.warning("[MessageSlimmer] 落盘失败，保留原结果: %s", e)
        return message

    def _process_result_sync(
        self, result: ToolMessage | Command, state: Any
    ) -> ToolMessage | Command:
        """同步：处理 wrap_tool_call 返回值（单 ToolMessage 或带消息的 Command）。"""
        prior_messages = _state_messages(state)
        if isinstance(result, ToolMessage):
            return self._process_tool_message_sync(result, prior_messages)

        if isinstance(result, Command) and result.update is not None:
            command_messages, wrapped = _unwrap_command_messages(result.update)
            processed = [
                self._process_tool_message_sync(m, prior_messages)
                if isinstance(m, ToolMessage)
                else m
                for m in command_messages
            ]
            return Command(
                goto=result.goto,
                graph=result.graph,
                update={**result.update, "messages": _rewrap_command_messages(processed, wrapped=wrapped)},
            )
        return result

    async def _process_result_async(
        self, result: ToolMessage | Command, state: Any
    ) -> ToolMessage | Command:
        """异步：处理 wrap_tool_call 返回值（单 ToolMessage 或带消息的 Command）。"""
        prior_messages = _state_messages(state)
        if isinstance(result, ToolMessage):
            return await self._process_tool_message_async(result, prior_messages)

        if isinstance(result, Command) and result.update is not None:
            command_messages, wrapped = _unwrap_command_messages(result.update)
            processed = [
                await self._process_tool_message_async(m, prior_messages)
                if isinstance(m, ToolMessage)
                else m
                for m in command_messages
            ]
            return Command(
                goto=result.goto,
                graph=result.graph,
                update={**result.update, "messages": _rewrap_command_messages(processed, wrapped=wrapped)},
            )
        return result

    # ── 中间件接口 ─────────────────────────────────────────────

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """同步工具调用：先执行，再瘦身结果。"""
        try:
            result = handler(request)
            return self._process_result_sync(result, request.state)
        except Exception as e:  # noqa: BLE001  # 瘦身失败不阻断 agent，原始异常照常向上抛
            _logger.warning("[MessageSlimmer] wrap_tool_call 异常，按原始行为透传: %s", e)
            raise

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> ToolMessage | Command:
        """异步工具调用：先执行，再瘦身结果。"""
        try:
            result = await handler(request)
            return await self._process_result_async(result, request.state)
        except Exception as e:  # noqa: BLE001  # 瘦身失败不阻断 agent，原始异常照常向上抛
            _logger.warning("[MessageSlimmer] awrap_tool_call 异常，按原始行为透传: %s", e)
            raise
