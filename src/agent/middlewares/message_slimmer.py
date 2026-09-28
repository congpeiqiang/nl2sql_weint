"""MessageSlimmerMiddleware — 主动瘦身 ToolMessage（截断超大 + 去重完全重复）。

方案 B 第一阶段（L1，低风险高收益）：解决 LangGraph checkpoint 累积膨胀。
每个 checkpoint 都存「截至当前的完整消息列表」（O(n²) 膨胀），而最大头是
超大 tool 结果（实测线程里单条 73.4KB read_file 占消息总大小 24%）与
完全重复的 tool 结果（2×14KB write_todos + 2×8.8KB SKILL 读取）。

两个动作都在工具结果进入 state/checkpoint 之前改写（wrap_tool_call / awrap_tool_call）：

1. **截断超大 tool 结果**：文本超过阈值（默认 8000 字符，环境变量 `LARGE_RESULT_TRUNCATE_CHARS` 可调）的 ToolMessage，
   把完整内容落盘到 `large_tool_results/<tool_call_id>`，消息内只保留
   head+tail 预览 + 路径指针（复用 deepagents 的 `_offload_tool_message_content`）。
   覆盖 read_file / execute 等 deepagents 主动驱逐豁免的工具——那些正是本系统膨胀源。

2. **去重完全重复**：新 ToolMessage 文本与线程内历史某条 ToolMessage 完全相同时，
   把内容替换成小占位（引用首次出现的 tool_call_id），**保留 tool_call_id / name / id**，
   LangGraph 的 tool_call 配对与前端 deriveStepsFromSubMessages 的步骤关联都不受影响。

3. **图表结果免截断**：带**完整**交互式 iframe（`data:text/html;base64,`）的结果不落盘
   —— 预览是按行截断的，图表结果基本只有一行，会被砍得 `</iframe>` 都不剩，导致
   聊天与报告**同时静默丢图**。体积上限见 `_CHART_IFRAME_EXEMPT_MAX_CHARS`。

4. **知识料结果免截断**：知识类工具（`get_instructions` / `get_all_knowledge` /
   `list_knowledge`）的结果不落盘 —— 它们是本子任务唯一的业务口径来源，落盘后模型只看到
   head5/tail5 预览、正文在被砍掉的中间，就会拿文件工具去 VFS 里找并不存在的 knowledge 路径。
   体积上限见 `_KNOWLEDGE_EXEMPT_MAX_CHARS`。

安全设计：
- 全程 fail-open：任何异常只记日志并返回原始结果，绝不阻断 agent 循环。
- 不触碰 AI/Human 消息（AI 消息瘦身属 L2，暂缓）。
- 阈值由环境变量 `LARGE_RESULT_TRUNCATE_CHARS` 控制（main_agent.py 不再传死值）；
  构造参数传显式 int 可覆盖，传 None 关闭截断（只去重）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
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

# 默认截断阈值（字符数）。可用环境变量 LARGE_RESULT_TRUNCATE_CHARS 覆盖
# （main_agent / langfuse_span 读同一变量，保证「落盘阈值」与「span output 截断阈值」对齐）。
# 默认 8000 字符（此前 main_agent.py 显式传 8_000 的实际运行值）；
# 8.8KB 的 SKILL.md 读取与 14KB 的 write_todos 结果高于该阈值会被截断（重复场景交给去重处理）。
_DEFAULT_MAX_CHARS_BEFORE_TRUNCATE = int(
    os.environ.get("LARGE_RESULT_TRUNCATE_CHARS", "8000")
)

# 去重占位文本。tool_call_id 保持原值，这里引用首次出现的那条。
_DEDUP_STUB = (
    "[内容重复已省略] 此工具结果与线程内先前同名工具结果完全相同"
    "（首次出现于 tool_call_id: {first_id}），不再重复展示。"
    "如需完整内容，请向上查阅历史中的该次结果。"
)

# ── 图表结果免截断 ──────────────────────────────────────────────
# 交互式图表（ECharts）以整段 `<iframe src="data:text/html;base64,...">` 内嵌在
# 工具结果里，base64 让体积膨胀 4/3：实测 200 个分类 ≈ 9.6KB、1000 个分类 ≈ 22KB，
# 都越过 8000 阈值。而 `_create_content_preview` 是**按行**取头尾 5 行、每行截到
# 1000 字符（deepagents `_message_eviction.py`）—— 图表结果基本只有一行，于是那行
# 被砍断、`</iframe>` 消失：报告侧 `_RE_IFRAME` 匹配不到、前端
# `extractInteractiveChartIframes` 也提取不到，**聊天和报告同时静默丢图**
# （工具卡还写着「图表已渲染在消息中 ↑」，用户看不到任何东西）。
# 因此：内容里带**完整**交互式 iframe 时不落盘 —— 宁可让它进 LLM 上下文
# （阈值以下的小图本来就一直在上下文里，这里只是把线抬高）。60KB 之上仍走落盘
# 兜底，避免上下文被无上限的巨型图撑爆。
_CHART_IFRAME_EXEMPT_MAX_CHARS = int(
    os.environ.get("CHART_IFRAME_EXEMPT_MAX_CHARS", "60000")
)
_INTERACTIVE_CHART_MARK = "data:text/html;base64,"
_CHART_IFRAME_RE = re.compile(r"<iframe[^>]*>.*?</iframe>", re.IGNORECASE | re.DOTALL)


def _carries_complete_chart_iframe(content_str: str) -> bool:
    """内容里是否有**完整闭合**的交互式图表 iframe。

    必须是完整的 `<iframe ...></iframe>`：已经被截断过的结果（只剩开头一千字符的
    `<iframe src="data:text/html;base64,`）不算 —— 那种情况图已经丢了，落盘反而
    能保住全文，让 read_file 还有得救。
    """
    if _INTERACTIVE_CHART_MARK not in content_str:
        return False
    return _CHART_IFRAME_RE.search(content_str) is not None


# ── 知识料结果免截断 ──────────────────────────────────────────────
# 背景（生产 trace `2f98ed67…`，2026-09-25）：四路取料的**规则轴** `get_instructions`
# 返回 `knowledge/rules/*.md` 的无边界拼接（实测 20,919 字符 = 3 个文件），被本中间件
# 落盘成一个 head5/tail5 预览 —— 而 R1~R8 正文正好在被砍掉的中间。模型读到的头 5 行
# 里恰好写着「工时专项见 `报工与工时.md`」，于是拿 read_file/grep/glob/ls 去文件系统
# 找了它 9 次（真路径 `/workspace/<语义库目录>/knowledge/...` 含一段不可推导的目录名，
# 且不在子 agent 可读通道内），全被权限拒，紧接着 LLM 调用 243s 超时、子任务报废。
#
# 知识料恰恰是**最不该落盘**的一类：它是本子任务唯一的业务口径来源，且按 wren-retrieve
# 的「唯一性铁律」一次取齐、全程只读这一份 —— 用「模型找不到口径」换掉的这点上下文，
# 完全不值。故：知识类工具结果在 `KNOWLEDGE_EXEMPT_MAX_CHARS` 以内不落盘
# （超过上限仍落盘兜底，避免上下文被无上限的巨型知识库撑爆）。
#
# 按**后缀**匹配：MCP 工具名带 `wrenai_<库名>_` 前缀（如 `wrenai_witops_get_instructions`），
# 按库名拼前缀会漏掉没预期的库，后缀才稳；不带下划线 ⇒ 裸名与带前缀名同时命中。
# 判据依赖 `ToolMessage.name` = 工具名（langgraph ToolNode 用 `call["name"]` 构造，已核）；
# 万一某条 ToolMessage 没有 name，则**不豁免**（退回落盘），方向是安全的。
_KNOWLEDGE_EXEMPT_TOOL_SUFFIXES = (
    "get_instructions",
    "get_all_knowledge",
    "list_knowledge",
)
_KNOWLEDGE_EXEMPT_MAX_CHARS = int(
    os.environ.get("KNOWLEDGE_EXEMPT_MAX_CHARS", "60000")
)


def _is_knowledge_tool(tool_name: str | None) -> bool:
    """工具名是否属于知识料取料工具（见上方说明：按后缀匹配以兼容 `wrenai_<库名>_` 前缀）。"""
    if not tool_name:
        return False
    return tool_name.endswith(_KNOWLEDGE_EXEMPT_TOOL_SUFFIXES)


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
        chart_exempt_max_chars: int | None = _CHART_IFRAME_EXEMPT_MAX_CHARS,
        knowledge_exempt_max_chars: int | None = _KNOWLEDGE_EXEMPT_MAX_CHARS,
    ) -> None:
        """初始化。

        Args:
            backend: 落盘超大工具结果用的后端（如 main_agent 的 composite_backend）。
                None 时仍可去重，但超大结果不落盘（保持原样，等价只做去重）。
            max_chars_before_truncate: 触发截断的文本字符阈值；None 关闭截断（只去重）。
            chart_exempt_max_chars: 图表结果免截断的体积上限；None 表示不设上限
                （带完整交互式 iframe 的结果永不落盘）。见模块顶部说明。
            knowledge_exempt_max_chars: 知识类工具（get_instructions / get_all_knowledge /
                list_knowledge）结果的免截断体积上限；None 表示不设上限（永不落盘）。
        """
        self._backend = backend
        self._max_chars_before_truncate = max_chars_before_truncate
        self._chart_exempt_max_chars = chart_exempt_max_chars
        self._knowledge_exempt_max_chars = knowledge_exempt_max_chars

        # 超大工具结果落盘目录前缀。统一用 "/workspace/large_tool_results"：
        # 命中 CompositeBackend 的 "/workspace/" 路由 → workspace_data_backend
        # （前端选择的工作区），与 SummarizationMiddleware 溢出落盘、langfuse_span
        # vfs 指针同路径口径。非 CompositeBackend 时保持原来 "/large_tool_results"。
        if isinstance(backend, CompositeBackend):
            self._large_tool_results_prefix = "/workspace/large_tool_results"
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

    def _is_chart_exempt(self, content_str: str) -> bool:
        """承载交互式图表的结果是否免于落盘截断（见模块顶部说明）。

        超过 `chart_exempt_max_chars` 的仍落盘：那种量级不是正常图表，
        不能让上下文无上限膨胀；此时记 warning 便于排查「图又没了」。
        """
        if not _carries_complete_chart_iframe(content_str):
            return False
        if (
            self._chart_exempt_max_chars is not None
            and len(content_str) > self._chart_exempt_max_chars
        ):
            _logger.warning(
                "[MessageSlimmer] 图表结果 %d chars 超过免截断上限 %d，仍落盘截断",
                len(content_str),
                self._chart_exempt_max_chars,
            )
            return False
        return True

    def _is_knowledge_exempt(self, message: ToolMessage, content_str: str) -> bool:
        """知识类工具的结果是否免于落盘截断（见模块顶部说明）。

        超过 `knowledge_exempt_max_chars` 的仍落盘：那种量级不是正常知识库，不能让
        上下文无上限膨胀；此时记 warning 便于排查「模型又在找 knowledge 路径」。
        """
        if not _is_knowledge_tool(message.name):
            return False
        if (
            self._knowledge_exempt_max_chars is not None
            and len(content_str) > self._knowledge_exempt_max_chars
        ):
            _logger.warning(
                "[MessageSlimmer] 知识类结果 %s %d chars 超过免截断上限 %d，仍落盘截断",
                message.name,
                len(content_str),
                self._knowledge_exempt_max_chars,
            )
            return False
        return True

    def _process_tool_message_sync(
        self, message: ToolMessage, prior_messages: list[Any]
    ) -> ToolMessage:
        """同步路径：去重 → 落盘截断 → 原样返回。"""
        deduped = self._dedup(message, prior_messages)
        if deduped is not None:
            return deduped

        content_str = _extract_text_from_message(message)
        if self._backend is not None and self._should_truncate(content_str):
            if self._is_knowledge_exempt(message, content_str):
                _logger.info(
                    "[MessageSlimmer] 知识类结果 %s %d chars 免截断（否则正文被砍掉后，"
                    "模型会去 VFS 找并不存在的 knowledge 路径）",
                    message.name,
                    len(content_str),
                )
                return message
            if self._is_chart_exempt(content_str):
                _logger.info(
                    "[MessageSlimmer] 图表结果 %d chars 免截断（保住完整 iframe，"
                    "否则聊天与报告都会静默丢图）",
                    len(content_str),
                )
                return message
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
            if self._is_knowledge_exempt(message, content_str):
                _logger.info(
                    "[MessageSlimmer] 知识类结果 %s %d chars 免截断（否则正文被砍掉后，"
                    "模型会去 VFS 找并不存在的 knowledge 路径）",
                    message.name,
                    len(content_str),
                )
                return message
            if self._is_chart_exempt(content_str):
                _logger.info(
                    "[MessageSlimmer] 图表结果 %d chars 免截断（保住完整 iframe，"
                    "否则聊天与报告都会静默丢图）",
                    len(content_str),
                )
                return message
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
