"""FailureCircuitMiddleware — 同类工具失败熔断（「对象不存在」反复重试）。

## 背景（trace ec484c693f，2026-10-09，project proj_6nMyvFmN）

语义库里**删掉 model `do_bug`** 后，问一个必须查它的指标。模型拿到
``table 'wren.public.do_bug' not found`` 之后**没有收手**：改 SQL 写法、在
``dry_run`` → ``run_sql`` → ``query_cube`` → ``dry_plan`` 之间来回换、甚至把知识库
注释里出现过的 ``do_bug_wide`` / ``do_story_wide`` 当成对象去探。实测：

    98 次工具调用 / 26 次失败，其中 **18 次是同一个对象**；
    首败 02:42:59 → 末败 02:49:28 = **406.7s**，整个 turn 才 433.9s。
    这 18 次调用**自身**耗时合计仅 33.5s ⇒ 其余全是围着同一个错误转圈。

而同一批 ``dry_plan`` 里 ``do_project`` / ``do_annex`` / ``do_problem`` 全部**成功**
并产出 CTE —— 语义层是通的，**两次就足以判死「只有 do_bug 这条路径坏」**。

## 为什么既有护栏一个都没拦住

1. ``nl2sql_agent.dynamic_prompt`` 那句「若误用语义层 run_sql 且报 not found，
   立即改用 dbmcp_run_sql，**不要进入纠错循环**」**只在 db 未建模的分支**注入；
   本库**建了模** ⇒ 这条护栏压根不在提示词里（见 nl2sql_agent.py:196）。
2. ``MessageSlimmerMiddleware`` 的去重只认**逐字节完全相同**的结果 —— 模型每次
   微调 SQL，md5 就变了，永不触发；而且它只省 context，**不省调用**。
3. 全仓没有「同类失败计数」。

本中间件补第 3 条。

## 签名口径（关键设计）

签名 = **(错误族, 归一化对象名)**，**不含工具名**。

不带工具名是刻意的：同一根因会被 ``dry_run`` / ``run_sql`` / ``query_cube`` /
``dry_plan`` 四种工具各撞一次（实测正是如此）。若把工具名并入签名，每种工具各自
计数 ⇒ 熔断形同虚设，18 次只会变成 8 次。

对象名取**最后一段**（``wren.public.do_bug`` → ``do_bug``），跨工具对齐。

## 作用域

计数来源是 **state 里「最后一条 HumanMessage 之后」的 ToolMessage**，不落任何进程内
账本（同 ``MessageSlimmerMiddleware._dedup`` 的口径：从会话本身推导）。好处：

* 子 agent 线程即使被复用，新问题从新的 HumanMessage 起算，**不误伤**；
* 无进程内状态 ⇒ 重启 / 多 worker 都不会错乱。

同一批并行调用互相看不见对方的结果（``request.state`` 里还没有），所以**并发扇出
天然不会被算作重复** —— 这是想要的：两个不同对象并行探一次不算浪费。

## 升级与硬闸

| 第几次 | 行为 |
|---|---|
| 1 | 放行原错误，但改写为**可收手的信息**：语义层没有该对象、换写法/换工具都不会让它出现、先去 `list_models()` 核实、核实不了就如实告诉用户 |
| ≥ WARN（默认 2） | 改写为「这是本 run 第 N 次撞同一对象，**停止重试**」 |
| ≥ STOP（默认 3） | 改写为终态措辞；此后该对象进**硬闸** —— 任何 args 里再出现这个对象名的调用**直接拒绝、不再执行**（`_blocked`） |

另有**失败预算**一档：本轮「对象不存在」类失败**跨不同对象**的总量 ≥ TOTAL（默认 6）时，
在消息尾追加「已达失败预算，立刻收尾」。单对象熔断挡不住「换个不存在的对象就重置计数」的
试探（trace 里模型另探了 `do_bug_wide` / `do_story_wide`，都是知识库注明已停用的幻觉名）。
这一档**只加措辞、不硬拦** —— 跨对象硬拦的误伤面太大。

改写后的消息内嵌机读标记 ``【fail-circuit:<族>:<对象>】``，让下一轮的签名解析不依赖
自由文本（模型可能转述、中间件可能改写措辞）。

``status="error"`` 保留（langfuse_span 依赖它把该次记成 exec 失败而不是
sql_exec_success=1）。

## 挂载

``nl2sql_agent._middleware``，紧挨 ``LangfuseSpanMiddleware`` **之前**（= 外层）。

位置讲究：``wrap_tool_call`` 链 first=outermost、**内层后处理先跑**，故本层后处理在
LangfuseSpan 落 span 之后、在 MessageSlimmer 之前跑 ⇒
① span 与 process_data dump 吃到的是**原始**报错；② Slimmer 体积判据/md5 去重作用在
**改写后**进 state 的那份文本上（与 Slimmer 自己的注释同一套口径）。

## 开关

* ``NL2SQL_FAIL_CIRCUIT=off`` —— 一键回退（整个中间件零动作）。
* ``NL2SQL_FAIL_CIRCUIT_WARN`` / ``NL2SQL_FAIL_CIRCUIT_STOP`` / ``NL2SQL_FAIL_CIRCUIT_TOTAL``
  —— 三个阈值，非法值一律回落默认。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

_logger = logging.getLogger(__name__)

# ── 机读标记：改写过的失败消息自带签名，下一轮不必再从自由文本里猜 ──────────
_MARKER_RE = re.compile(r"【fail-circuit:([a-z_]+):([^】]*)】")

# ── 「对象不存在」族。顺序要紧：先具体（table/column）后笼统 ────────────────
# wren 语义层（规划期）：Error during planning: table 'wren.public.do_bug' not found
_SIG_RES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"table\s+'([^']+)'\s+not found", re.I), "table"),
    (re.compile(r"Table\s+'([^']+)'\s+doesn't\s+exist", re.I), "table"),
    (re.compile(r"column\s+'([^']+)'\s+not found", re.I), "column"),
    (re.compile(r"Unknown column\s+'([^']+)'", re.I), "column"),
)

_LABEL = {"table": "表", "column": "字段"}

# 默认阈值：第 2 次开始明确「停止重试」，第 3 次起进硬闸；本轮跨对象总量 6 次触发失败预算。
_DEFAULT_WARN = 2
_DEFAULT_STOP = 3
_DEFAULT_TOTAL = 6


def _env_int(name: str, default: int) -> int:
    """读正整数环境变量；非法值一律回落默认（不因配置写错而改变行为）。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        _logger.warning("[fail_circuit] %s=%r 不是整数，按默认 %d", name, raw, default)
        return default
    return val if val >= 1 else default


def _enabled() -> bool:
    return (os.getenv("NL2SQL_FAIL_CIRCUIT") or "").strip().lower() not in {"off", "0", "false", "no"}


def _norm_object(raw: str) -> str:
    """对象名归一：剥引号/反引号，取最后一段（wren.public.do_bug → do_bug）。"""
    s = (raw or "").strip().strip("`\"' ")
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    return s.strip("`\"' ").strip()


def _signature(text: str) -> tuple[str, str] | None:
    """从一段文本里取 (错误族, 对象名)；取不到返回 None（= 不归本中间件管）。

    标记优先：改写过的消息即便被模型转述、被其它中间件加壳，签名仍然稳定。
    """
    m = _MARKER_RE.search(text)
    if m:
        obj = _norm_object(m.group(2))
        if obj:
            return (m.group(1), obj)
    for rx, family in _SIG_RES:
        hit = rx.search(text)
        if hit:
            obj = _norm_object(hit.group(1))
            if obj:
                return (family, obj)
    return None


def _extract_text(message: ToolMessage) -> str:
    """ToolMessage.content → 纯文本（content 可能是 str 或 block 列表）。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                txt = block.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content or "")


def _state_messages(state: Any) -> list[Any]:
    """从 ToolCallRequest.state（dict / BaseModel）安全取消息列表。"""
    if state is None:
        return []
    if isinstance(state, dict):
        return list(state.get("messages") or [])
    msgs = getattr(state, "messages", None)
    if msgs is not None:
        return list(msgs)
    if isinstance(state, (list, tuple)):
        return list(state)
    return []


def _current_turn_messages(state: Any) -> list[Any]:
    """取「最后一条 HumanMessage 起」的消息 —— 本 run 的作用域。

    子 agent 线程可能被复用；从最新的 HumanMessage（委派 prompt）起算，上一问的
    失败不会算进这一问。取不到 HumanMessage 时退化为整段（宁可少拦不误伤）。
    """
    msgs = _state_messages(state)
    for idx in range(len(msgs) - 1, -1, -1):
        if isinstance(msgs[idx], HumanMessage):
            return msgs[idx + 1:]
    return msgs


def _sig_counts(state: Any) -> dict[tuple[str, str], int]:
    """本轮各签名的出现次数（同一遍扫描同时供「同一对象」与「本轮总量」两个判据）。"""
    counts: dict[tuple[str, str], int] = {}
    for m in _current_turn_messages(state):
        if not isinstance(m, ToolMessage):
            continue
        sig = _signature(_extract_text(m))
        if sig:
            counts[sig] = counts.get(sig, 0) + 1
    return counts


def _tool_name(request: ToolCallRequest) -> str:
    tc = getattr(request, "tool_call", None) or {}
    if isinstance(tc, dict):
        return str(tc.get("name", "") or "")
    return str(getattr(tc, "name", "") or "")


def _tool_call_id(request: ToolCallRequest) -> str:
    return getattr(getattr(request, "runtime", None), "tool_call_id", None) or ""


def _args_text(request: ToolCallRequest) -> str:
    """把 tool_call.args 压成一段文本，用于硬闸里找对象名。"""
    tc = getattr(request, "tool_call", None) or {}
    args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", None)
    if isinstance(args, dict):
        try:
            return "\n".join(str(v) for v in args.values())
        except Exception:  # noqa: BLE001
            return ""
    return str(args or "")


class FailureCircuitMiddleware(AgentMiddleware):
    """同一「对象不存在」错误反复出现时改写提示并熔断（见模块 docstring）。"""

    def __init__(
        self,
        *,
        warn_at: int | None = None,
        stop_at: int | None = None,
        total_at: int | None = None,
    ) -> None:
        self._warn_at = warn_at if warn_at is not None else _env_int(
            "NL2SQL_FAIL_CIRCUIT_WARN", _DEFAULT_WARN
        )
        self._stop_at = stop_at if stop_at is not None else _env_int(
            "NL2SQL_FAIL_CIRCUIT_STOP", _DEFAULT_STOP
        )
        self._total_at = total_at if total_at is not None else _env_int(
            "NL2SQL_FAIL_CIRCUIT_TOTAL", _DEFAULT_TOTAL
        )

    # ── 文案 ────────────────────────────────────────────────────

    def _first_text(self, sig: tuple[str, str], tool: str) -> str:
        family, obj = sig
        return (
            f"Error: 语义层里**不存在**{_LABEL.get(family, '对象')}「{obj}」—— {tool} 因此失败。\n"
            f"换 SQL 写法、换引号、换参数都不会让「{obj}」出现；改叫 dry_run / dry_plan / "
            f"run_sql / query_cube 也只是同一个根因的另一种撞法。\n"
            f"请先 `list_models()` 或 `get_mdl()` **核实真实对象名**"
            f"（不要凭知识库注释或记忆里的名字猜——已停用的对象不在语义库里）。核实后二选一：\n"
            f"  1) 改用确实存在的对象重写查询；\n"
            f"  2) 该数据当前确实取不到 → **如实向用户说明**，不要继续试。\n"
            f"【fail-circuit:{family}:{obj}】"
        )

    def _repeat_text(self, sig: tuple[str, str], n: int) -> str:
        family, obj = sig
        return (
            f"Error: {_LABEL.get(family, '对象')}「{obj}」在语义层不存在 —— 这是本 run 第 {n} 次"
            f"撞同一个对象。**停止重试**：同一个对象不会因为换工具、换写法而变得存在。\n"
            f"现在只做两件事之一：\n"
            f"  1) 用 `list_models()` 列出真实对象，换一个确实存在的继续；\n"
            f"  2) 直接收尾，并向用户如实说明「{obj}」当前不可查。\n"
            f"继续尝试属于无效消耗（实测同类 trace：18 次重复失败烧掉 7 分钟）。\n"
            f"【fail-circuit:{family}:{obj}】"
        )

    def _stop_text(self, sig: tuple[str, str], n: int) -> str:
        family, obj = sig
        label = _LABEL.get(family, "对象")
        return (
            f"Error: 已对{label}「{obj}」尝试 {n} 次，全部失败 —— 本对象**已被熔断**，"
            f"后续任何用到它的调用都会被直接拒绝、不再执行。\n"
            f"不要再调用任何会命中「{obj}」的工具。请立刻二选一：\n"
            f"  1) 换用 `list_models()` 里的真实对象；\n"
            f"  2) 收尾并向用户如实说明该数据当前不可得。\n"
            f"【fail-circuit:{family}:{obj}】"
        )

    # ── 硬闸：对象已达 STOP 且本次调用又引用它 ────────────────────

    def _blocked_sig(self, request: ToolCallRequest) -> tuple[str, str, int] | None:
        """本次 args 引用了已达 STOP 的熔断对象 → 返回 (族, 对象, 已失败次数)。"""
        args_text = _args_text(request)
        if not args_text:
            return None
        seen = _sig_counts(getattr(request, "state", None))
        for sig, n in seen.items():
            if n < self._stop_at:
                continue
            obj = sig[1]
            if re.search(rf"(?<![\w]){re.escape(obj)}(?![\w])", args_text):
                return (sig[0], obj, n)
        return None

    # ── 主流程 ──────────────────────────────────────────────────

    def _after(self, request: ToolCallRequest, result: Any) -> Any:
        """工具返回后：认签名 → 数本轮 → 改写/升级。"""
        if not isinstance(result, ToolMessage):
            return result
        text = _extract_text(result)
        if not text:
            return result
        sig = _signature(text)
        if sig is None:
            return result

        counts = _sig_counts(getattr(request, "state", None))
        n = counts.get(sig, 0) + 1
        total = sum(counts.values()) + 1
        tool = _tool_name(request) or "该工具"
        if n >= self._stop_at:
            new_text = self._stop_text(sig, n)
            level = "warning"
        elif n >= self._warn_at:
            new_text = self._repeat_text(sig, n)
            level = "info"
        else:
            new_text = self._first_text(sig, tool)
            level = "info"

        # 失败预算：本轮「对象不存在」类失败**跨不同对象**的总量也封顶。
        # 单对象熔断挡不住「每换一个不存在的对象就重置计数」的试探（trace 里模型
        # 另探了 do_bug_wide / do_story_wide —— 都是知识库注明已停用的幻觉名）。
        # 只加措辞、不硬拦：跨对象的硬拦误伤面太大。
        if total >= self._total_at:
            new_text += (
                f"\n（本轮「对象不存在」类失败已累计 {total} 次、跨越 {len(counts) + (0 if sig in counts else 1)}"
                f" 个不同对象，已达失败预算）—— 立刻停止试探并收尾：能查的查完，"
                f"查不到的对象**如实向用户说明**。"
            )

        getattr(_logger, level)(
            "[fail_circuit] %s 第 %d 次失败（对象=%s 族=%s，工具=%s；本轮累计 %d）→ 改写提示",
            "熔断" if n >= self._stop_at else "计数",
            n,
            sig[1],
            sig[0],
            tool,
            total,
        )
        # 保留 tool_call_id / name / status —— LangGraph 的 tool 配对与
        # langfuse_span 的「exec 失败」判定都靠它们。
        return result.model_copy(update={"content": new_text})

    def _deny(self, request: ToolCallRequest, hit: tuple[str, str, int]) -> ToolMessage:
        family, obj, n = hit
        _logger.warning(
            "[fail_circuit] 硬闸拒绝 %s：args 引用了已熔断对象 %s（已失败 %d 次）",
            _tool_name(request) or "?",
            obj,
            n,
        )
        return ToolMessage(
            content=self._stop_text((family, obj), n),
            name=_tool_name(request),
            tool_call_id=_tool_call_id(request),
            status="error",
        )

    # ── 中间件接口 ──────────────────────────────────────────────

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        """同步：先过硬闸，执行后按签名计数改写。任何异常都 fail-open 透传。"""
        if not _enabled():
            return handler(request)
        try:
            hit = self._blocked_sig(request)
            if hit is not None:
                return self._deny(request, hit)
        except Exception:  # noqa: BLE001 —— 护栏本身绝不能弄坏工具调用
            _logger.warning("[fail_circuit] 前置硬闸异常，放行", exc_info=True)
        result = handler(request)
        try:
            return self._after(request, result)
        except Exception:  # noqa: BLE001
            _logger.warning("[fail_circuit] 后处理异常，透传原结果", exc_info=True)
            return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> ToolMessage:
        """异步：同 `wrap_tool_call`。"""
        if not _enabled():
            result = handler(request)
            if hasattr(result, "__await__"):
                return await result
            return result
        try:
            hit = self._blocked_sig(request)
            if hit is not None:
                return self._deny(request, hit)
        except Exception:  # noqa: BLE001
            _logger.warning("[fail_circuit] 前置硬闸异常，放行", exc_info=True)
        result = handler(request)
        if hasattr(result, "__await__"):
            result = await result
        try:
            return self._after(request, result)
        except Exception:  # noqa: BLE001
            _logger.warning("[fail_circuit] 后处理异常，透传原结果", exc_info=True)
            return result
