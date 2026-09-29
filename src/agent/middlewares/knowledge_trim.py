"""知识料按问题裁剪 —— `get_instructions` 的结果只保留与当前问题相关的规则文件。

## 为什么只能在中间件做

wren 的 `get_instructions()` **不带任何参数**（`wren/mcp_server.py:313`：`def
get_instructions() -> dict`），工具层根本拿不到问题；只有中间件同时看得见 state / run
metadata 与工具结果，也才有能力改写 ToolResult。

## 裁剪对象只有 `get_instructions`（三个邻近工具的取舍都写在这里）

- `list_knowledge` → `{"files": [...], "agents": bool}`：**只有文件名、没有正文**，没有可裁
  的东西；而它的用途恰恰是「来源标注与存在性核对」，裁它等于给模型假信息 ⇒ **不动**。
- `list_stored_queries`：语义就是「枚举全部」（wren 自己的 docstring 写明它区别于
  `recall_queries` 的语义 top-k），且已被 `path_resolver` 的 fast-path 接管 ⇒ **不动**。
- `get_all_knowledge`：**生产容器里没有这个工具**（本机 `.venv` 那份 wren 被手改过、上游
  没有）⇒ 不为它写代码。

## 安全闸（任一条不过 ⇒ **逐字原样**返回，只记一条 info）

1. **重建 == 原文**：按 wren `load_rules` 的口径（`glob("*.md")` + `sorted` + 逐份 `strip()`
   + 跳空 + 先 `knowledge/rules/*` 后 legacy `<project>/instructions.md`，整体 `"\n\n"` 拼）
   重建分片；拼起来与收到的原文**不相等就放弃**。这条闸把「同构」变成自验证的 —— 顺序或
   strip 差一点只会静默不裁，**绝不会裁错内容**。
2. 原文短于 `NL2SQL_KNOWLEDGE_TRIM_MIN_CHARS`（默认 8000）⇒ 不裁（小语料没必要）。
3. 没有当前问题 ⇒ 不裁。
4. 检索无命中 ⇒ 不裁。
5. **地板**：保留文件数 ≥ `..._MIN_FILES`（默认 3）且字符数 ≥ 原文 × `..._KEEP_RATIO`
   （默认 0.4），不足按**原序**补足（口径有先后依赖，不许重排）。
裁剪后追加说明段并列出未展示的文件名；追加后若**反而更长** ⇒ 不裁。

⚠️ 说明段的措辞是「**与当前问题无关**」，**不是「读不到」** —— 后者会让模型去 VFS 里找
并不存在的路径（见 `message_slimmer` 模块顶部的同类事故）。

⚠️ 现行默认值下，生产（`knowledge/rules/` 只有 3 个文件）**基本不会触发裁剪**（地板就是
3 份）。这是有意的保守：这条中间件的价值在大语料上，宁可当 no-op 也不冒丢口径的风险；
要放开就调 `NL2SQL_KNOWLEDGE_TRIM_MIN_FILES`。

## fail-open

本中间件**吞掉**所有异常（**与 `MessageSlimmerMiddleware` 的 `raise` 不同**）：那里 raise 是
为了让瘦身失败不阻断 agent，而这里 raise 会打断工具调用链、最坏损失只是「少吃一点 token」。
`NL2SQL_RETRIEVAL=off` 或 `NL2SQL_KNOWLEDGE_TRIM=off` ⇒ 完全不动。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deepagents.middleware._message_eviction import _extract_text_from_message

# 复用同一套「ToolMessage / Command 拆包」语义（同一份判据不写两遍）
from agent.middlewares.message_slimmer import (
    _rewrap_command_messages,
    _state_messages,
    _unwrap_command_messages,
)

_logger = logging.getLogger(__name__)

#：只裁这一个（理由见模块 docstring）。按**后缀**匹配以兼容 `wrenai_<库名>_` 前缀。
_TRIM_TOOL_SUFFIXES = ("get_instructions",)

_ENV_TRIM = "NL2SQL_KNOWLEDGE_TRIM"
_ENV_MIN_CHARS = "NL2SQL_KNOWLEDGE_TRIM_MIN_CHARS"
_ENV_MIN_FILES = "NL2SQL_KNOWLEDGE_TRIM_MIN_FILES"
_ENV_KEEP_RATIO = "NL2SQL_KNOWLEDGE_TRIM_KEEP_RATIO"
_ENV_HITS = "NL2SQL_KNOWLEDGE_TRIM_HITS"

_DEFAULT_MIN_CHARS = 8000
_DEFAULT_MIN_FILES = 3
_DEFAULT_KEEP_RATIO = 0.4
_DEFAULT_HITS = 6

_TRAILER = (
    "\n\n[说明] 本次返回已按当前问题裁剪：上面只含命中的 {kept} 个规则文件"
    "（{kept_names}）；其余 {omitted} 个（{omitted_names}）与当前问题无关，"
    "其正文不在本次返回中。请只依据以上原文写业务口径，不要去找文件。"
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default


def _is_trim_tool(tool_name: str | None) -> bool:
    """工具名是否属于本中间件的裁剪对象（按后缀匹配，兼容 `wrenai_<库名>_` 前缀）。"""
    if not tool_name:
        return False
    return tool_name.endswith(_TRIM_TOOL_SUFFIXES)


def _rules_parts(project: Path) -> list[tuple[str, str]]:
    """`[(显示名, 原文), ...]`，与 wren `load_rules` 的拼接口径**逐字同构**（见 docstring 闸 1）。"""
    parts: list[tuple[str, str]] = []
    rules_dir = Path(project) / "knowledge" / "rules"
    if rules_dir.is_dir():
        for md in sorted(rules_dir.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                parts.append((md.name, text))
    legacy = Path(project) / "instructions.md"
    if legacy.exists():
        try:
            text = legacy.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            parts.append((legacy.name, text))
    return parts


def _current_question(state: Any) -> str:
    """当前问题：先读每 run 注入的 metadata，再回落 state 里最后一条用户消息。"""
    try:
        from langgraph.config import get_config

        cfg = get_config()
        if cfg:
            q = str((cfg.get("metadata", {}) or {}).get("user_question", "") or "").strip()
            if q:
                return q
    except Exception:  # noqa: BLE001 —— 拿不到 config 不是错误（离线/测试路径）
        pass

    for msg in reversed(_state_messages(state)):
        if getattr(msg, "type", "") != "human":
            continue
        text = _extract_text_from_message(msg)
        text = re.sub(r"^\[系统自动通知\][\s\S]*", "", text or "").strip()
        if text:
            return text
    return ""


class KnowledgeTrimMiddleware(AgentMiddleware):
    """把 `get_instructions` 的正文裁到与当前问题相关的规则文件（见模块 docstring）。"""

    def __init__(
        self,
        *,
        min_chars: int | None = None,
        min_files: int | None = None,
        keep_ratio: float | None = None,
        hits: int | None = None,
    ) -> None:
        self._min_chars = _env_int(_ENV_MIN_CHARS, _DEFAULT_MIN_CHARS) if min_chars is None else min_chars
        self._min_files = _env_int(_ENV_MIN_FILES, _DEFAULT_MIN_FILES) if min_files is None else min_files
        self._keep_ratio = _env_float(_ENV_KEEP_RATIO, _DEFAULT_KEEP_RATIO) if keep_ratio is None else keep_ratio
        self._hits = _env_int(_ENV_HITS, _DEFAULT_HITS) if hits is None else hits

    # ── 核心：纯函数，便于离线验证 ──────────────────────────────

    def _trim(self, raw: str, project: Path, question: str) -> str | None:
        """裁剪后的 `instructions`；`None` ＝ 不裁（任何一道闸不过）。"""
        if not raw or not question.strip():
            return None
        if len(raw) < self._min_chars:
            return None

        parts = _rules_parts(project)
        if not parts:
            return None
        if "\n\n".join(text for _, text in parts) != raw:      # 闸 1：重建必须逐字相等
            _logger.info(
                "[KnowledgeTrim] 重建与原文不一致（>%d 字符差），放弃裁剪（宁可不裁）",
                abs(len(raw) - sum(len(t) + 2 for _, t in parts)),
            )
            return None

        try:
            from agent.retrieval import schema as S
            from agent.retrieval import search

            if not S.is_enabled():
                return None
            hits = search.search_project(
                Path(project), question.strip(),
                limit=max(1, self._hits), kinds={S.KIND_KNOWLEDGE_RULE},
            )
        except Exception as e:  # noqa: BLE001
            _logger.warning("[KnowledgeTrim] 检索失败，按不裁处理: %s", e)
            return None
        if not hits:
            return None

        hit_names = {
            Path(str((h.get("meta") or {}).get("file") or "").split("#")[0]).name
            for h in hits
        }
        kept = [i for i, (name, _) in enumerate(parts) if name in hit_names]
        if not kept:
            return None

        # 地板（份数 → 字符），按原序补足；绝不把语料砍到地板以下
        floor_chars = max(self._min_chars // 2, int(len(raw) * self._keep_ratio))
        for i in range(len(parts)):
            if len(kept) >= max(1, self._min_files):
                break
            if i not in kept:
                kept.append(i)
        for i in range(len(parts)):
            if sum(len(parts[j][1]) + 2 for j in kept) >= floor_chars:
                break
            if i not in kept:
                kept.append(i)
        kept = sorted(set(kept))
        if len(kept) >= len(parts):
            return None                                        # 一份都没省下

        omitted = [name for j, (name, _) in enumerate(parts) if j not in kept]
        trimmed = "\n\n".join(parts[j][1] for j in kept) + _TRAILER.format(
            kept=len(kept),
            kept_names="、".join(parts[j][0] for j in kept),
            omitted=len(omitted),
            omitted_names="、".join(omitted),
        )
        if len(trimmed) >= len(raw):
            return None                                        # 裁完反而更长 ⇒ 不裁
        return trimmed

    def _rewrite(self, message: ToolMessage, state: Any, project: Path) -> ToolMessage:
        """`get_instructions` 的 ToolMessage → 裁剪版（形状与其余字段一字不动）。"""
        try:
            if not _is_trim_tool(getattr(message, "name", "")):
                return message
            if not isinstance(message.content, str):
                return message            # 非纯文本（多模态块）不动：形状优先
            question = _current_question(state)
            if not question:
                return message
            raw = _extract_text_from_message(message)
            try:
                import json as _json

                payload = _json.loads(raw)
            except Exception:  # noqa: BLE001
                return message
            if not isinstance(payload, dict) or not isinstance(payload.get("instructions"), str):
                return message
            trimmed = self._trim(payload["instructions"], project, question)
            if trimmed is None:
                return message
            payload["instructions"] = trimmed        # 键集不变（instructions / used_legacy）
            _logger.info(
                "[KnowledgeTrim] %s: instructions %d → %d chars",
                message.name, len(raw), len(trimmed),
            )
            return message.model_copy(
                update={"content": _json.dumps(payload, ensure_ascii=False)}
            )
        except Exception as e:  # noqa: BLE001 —— 吞掉：裁剪失败不能打断工具链
            _logger.warning("[KnowledgeTrim] 裁剪失败，按原样透传: %s", e)
            return message

    # ── 中间件接口（先执行，再改写结果；与 MessageSlimmer 同形）────

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        result = handler(request)
        if not self._enabled():
            return result
        return self._safe_process(result, request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> ToolMessage | Command:
        result = await handler(request)
        if not self._enabled():
            return result
        # 检索腿是同步 IO（lancedb）：搬离事件循环，别让一次裁剪阻塞别的 run（P1-14 同源考虑）
        return await asyncio.to_thread(self._safe_process, result, request)

    # ── 结果拆包（ToolMessage / 带消息的 Command）────────────────

    def _enabled(self) -> bool:
        raw = str(os.environ.get(_ENV_TRIM, "on")).strip().lower()
        return raw not in ("off", "0", "false", "no")

    def _safe_process(self, result: ToolMessage | Command, request: ToolCallRequest) -> ToolMessage | Command:
        """拆包的外层保险：**任何异常都按原样透传**（见模块 docstring 的 fail-open）。"""
        try:
            return self._process_sync(result, request)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[KnowledgeTrim] 处理失败，按原样透传: %s", e)
            return result

    def _process_sync(self, result: ToolMessage | Command, request: ToolCallRequest) -> ToolMessage | Command:
        project = _tool_project(request)
        if project is None:
            return result
        state = getattr(request, "state", None)
        if isinstance(result, ToolMessage):
            return self._rewrite(result, state, project)
        if isinstance(result, Command) and result.update is not None:
            messages, wrapped = _unwrap_command_messages(result.update)
            processed = [
                self._rewrite(m, state, project) if isinstance(m, ToolMessage) else m for m in messages
            ]
            if processed == messages:
                return result
            return Command(
                goto=result.goto,
                graph=result.graph,
                update={**result.update, "messages": _rewrap_command_messages(processed, wrapped=wrapped)},
            )
        return result


def _tool_project(request: ToolCallRequest) -> Path | None:
    """工具上的 wren 项目路径（`mcp_tool.py` 注入的 `_wren_project_path`）。"""
    tool = getattr(request, "tool", None)
    raw = getattr(tool, "_wren_project_path", None)
    if not raw:
        _logger.debug("[KnowledgeTrim] %s 无 _wren_project_path，跳过", getattr(tool, "name", "?"))
        return None
    return Path(str(raw))


# 上面 `_rewrite` 里为「保留哪些文件」多算了一次 `_rules_parts`；这里是同一个纯函数，
# 保留一个名字供验证脚本直接调用（避免脚本重新实现一遍拼接口径）。
def rules_parts(project: Path) -> list[tuple[str, str]]:
    """见 `_rules_parts`（对验证脚本暴露的公开别名）。"""
    return _rules_parts(Path(project))
