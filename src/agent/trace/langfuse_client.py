"""Langfuse 公共模块 — 集中管理客户端与回调 handler 的创建。

所有 trace 埋点经由本模块获取，避免业务代码散落 langfuse 初始化。
SDK: langfuse 4.14.4（自带 langchain/langgraph 集成，无需单独 langfuse-langchain）。
配置从 .env 读取（LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL）。

用法：
    from agent.trace.langfuse_client import get_langfuse_handler, auth_check

    graph.with_config({"recursion_limit": 500, "callbacks": [get_langfuse_handler()]})

M4 版本管理：prompt 装配处用 get_prompt_text(name, label, fallback) 从 Langfuse 拉
system prompt（失败回退本地文件）；同步脚本用 create_prompt 上传本地 prompt 文件。
"""
from __future__ import annotations

import atexit
import logging
import os
import random
import re
from contextvars import ContextVar

_logger = logging.getLogger(__name__)

_client = None
_handler = None

# M-T3b：trace_id → root observation_id 映射（CallbackHandler on_chain_start 后写入，
# LangfuseSpanMiddleware._start_span 读取作为 parent_span_id，让 skill span 嵌套到
# chat_agent observation 下而非平级 root observation）。进程级，线程安全靠 GIL。
_ROOT_OBS_MAP: dict[str, str] = {}

# M-T3d：thread_id → (trace_id, root_observation_id) 映射。仅在「新查询」的 root
# chain 写入（子 agent / auto-continue 不写），记录该 thread 当前查询的主 trace。
# auto-continue run 启动时读取它，把续跑 observations 复用回原 trace（而非新开
# 一个 chat-turn trace）。进程级，线程安全靠 GIL。
_THREAD_TRACE_MAP: dict[str, tuple[str, str]] = {}

# M-T5：task_id → (main_thread_id, trace_id, root_observation_id, user_question,
# description) 映射。task_id 即异步子任务的独立 LangGraph thread_id（deepagents
# start_async_task 在 client.threads.create() 后把返回 thread_id 既作 task_id 又作
# runs.create 参数）。在 _wrap_runs_create 派发时写入，auto-continue 续跑到达时
# 用它按「任务」路由回原查询的 trace——解决连问场景：任务 N 完成通知落在问题
# N+1 的 run 活跃期时，不再误挂到 N+1 的 trace。进程级，线程安全靠 GIL。
_TASK_TRACE_MAP: dict[str, tuple[str, str, str, str, str]] = {}
_TASK_TRACE_CAP = 2000

# M5 灰度：本进程解析到的 prompt label（进程级，import 时掷一次）+ 各 label 已拉到的版本
_CANARY_RESOLVED: str | None = None
_PROMPT_VERSIONS: dict[str, int] = {}
# M4 评估后新增：prompt 来源追踪（"name@label" → langfuse|local），供 metadata.prompt.source
_PROMPT_SOURCE: dict[str, str] = {}

# ── M-T7：叶子归巢（治理 Tracing 孤儿 root trace 污染）───────────
# 背景：deepagents 异步子 agent / 自动续跑在 worker 线程/异步任务里执行 model/tool
# 叶子时，langchain run-tree 祖先（parent_run_id）不在 handler._runs（祖先 run 的
# observation 已 detach/reset），SDK 落到裸 client start_observation；该线程/任务
# 的 OTel contextvar 又为空 → v4 把每条 GENERATION/TOOL 提升成独立 root trace
# （以模型/工具名命名，如 ChatQwen/run_sql），session_id 全空，真 trace 树只剩骨架。
# 方案：叶子 start 包装器检测「父失联」时按 metadata 解析锚点，经 ContextVar 传给
# 被包装的 _get_parent_observation；命中则返回 _AnchoredClient，强制 start_observation
# 携带 trace_context={trace_id, parent_span_id} 归巢（复用 langfuse_span path A/C
# 已验证机制）。仅父失联 **且** 锚点可解析才干预，其余路径与未 patch 逐字节一致。
# 开关 LANGFUSE_LEAF_NESTING_ENABLE（默认 1；置 0 整段跳过）——比 LANGFUSE_ENABLE
# 更细的保底回滚手段。
_LEAF_ANCHOR: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "langfuse_leaf_anchor", default=None
)
# 与 langfuse_span._MAIN_TRACE_NAME 同值：显式 trace_context 的 span 若父观测跨批
# 导出，v4 可能把它判成新 root、用 span 名覆盖 trace 名（M-T6b 修复 A 同款防护）。
_LEAF_TRACE_NAME = "chat-turn"
_M_LEAF_NAMES = (
    "on_chat_model_start",
    "on_llm_start",
    "on_tool_start",
    "on_retriever_start",
)


def _leaf_nesting_enabled() -> bool:
    v = (os.getenv("LANGFUSE_LEAF_NESTING_ENABLE", "1") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _resolve_leaf_anchor(metadata) -> tuple[str, str, str] | None:
    """叶子「父失联」时解析归巢锚点 → (trace_id_hex, root_obs_hex, case) | None。

    定位键（生产实测，observations.metadata Map 键齐全）：
      1. langfuse_parent_trace_id —— 子 run（deepagents 异步子任务）/续跑 run 叶子
         （deepagents_async_config_patch._wrap_runs_create 在 runs.create 前注入并
         下传）。root obs 优先 _ROOT_OBS_MAP[tid]：子 run root chain 启动时已把自己
         root obs 刷进该 trace 槽 → 叶子挂到子 run root obs（nl2sql_agent 下、层级
         贴近现状）；槽空兜底 metadata.langfuse_parent_obs_id（runs.create 时快照的
         主 trace root obs，同样归入 chat-turn）。
      2. thread_id / langfuse_session_id（== 会话线程）→ _THREAD_TRACE_MAP[thr]
         —— 主 run / 自动续跑叶子：挂回 chat-turn trace 的 root obs。langgraph
         configurable.thread_id 部分 SDK 版本在 metadata.configurable 内，一并兜底。
      3. 两者皆空 → None（离线实验 / eval 等无父可归 run 保持原样，零回归）。
    """
    md = metadata or {}
    try:
        ptid = str(md.get("langfuse_parent_trace_id") or "").strip()
        if ptid:
            p_oid = _ROOT_OBS_MAP.get(ptid) or ""
            if not p_oid:
                p_oid = str(md.get("langfuse_parent_obs_id") or "").strip()
            return (ptid, p_oid, "subagent")
        for k in ("thread_id", "langfuse_session_id"):
            thr = str(md.get(k) or "").strip()
            if thr:
                rec = _THREAD_TRACE_MAP.get(thr)
                if rec and rec[0]:
                    return (rec[0], rec[1], f"thread:{k}")
        cfg = md.get("configurable")
        if isinstance(cfg, dict):
            thr = str(cfg.get("thread_id") or "").strip()
            if thr:
                rec = _THREAD_TRACE_MAP.get(thr)
                if rec and rec[0]:
                    return (rec[0], rec[1], "thread:configurable")
    except Exception:  # noqa: BLE001
        return None
    return None


class _AnchoredClient:
    """裸 client 薄包装：强制叶子 start_observation 携带显式 trace_context 归巢。

    SDK 调用方对返回对象做 `isinstance(x, Langfuse)` 判断——本类非 Langfuse → 走
    else 分支直接 `.start_observation(...)`（不传 trace_context）→ 本类注入
    {trace_id, parent_span_id}（hex 字符串，与 langfuse_span path A/C 同格式）。
    span 另补 langfuse.trace.name（_LEAF_TRACE_NAME）防 v4 用 span 名覆盖 trace 名。
    """

    __slots__ = ("_client", "_tid_hex", "_oid_hex")

    def __init__(self, client, tid_hex, oid_hex):
        self._client = client
        self._tid_hex = tid_hex
        self._oid_hex = oid_hex

    def start_observation(self, trace_context=None, **kwargs):
        if not trace_context:
            trace_context = {"trace_id": self._tid_hex}
            if self._oid_hex:
                trace_context["parent_span_id"] = self._oid_hex
        span = self._client.start_observation(trace_context=trace_context, **kwargs)
        try:
            span._otel_span.set_attribute("langfuse.trace.name", _LEAF_TRACE_NAME)
        except Exception:  # noqa: BLE001
            pass
        return span


def langfuse_enabled() -> bool:
    """总开关：LANGFUSE_ENABLE（默认 true，保持既有行为）。

    设 0/false/no/off 时停用一切运行时 Langfuse 埋点/打分/prompt 拉取
    （监控旁路整体一键关停），但 get_client() 本体仍可用——
    collect_badcase 等管理工具需独立读历史 trace，不受此开关影响。
    这是回滚的最顶层手段（比 LANGFUSE_PROMPT_ENABLED 范围更大）。
    """
    v = (os.getenv("LANGFUSE_ENABLE", "true") or "true").strip().lower()
    return v not in ("0", "false", "no", "off")


def get_client():
    """返回全局 Langfuse 客户端单例（惰性创建，配置走 .env）。

    注意：本函数不 gate 总开关（管理工具独立调用需要真实客户端）；
    运行时埋点由 get_langfuse_callbacks / create_score / get_prompt_text 等各自 gate。
    """
    global _client
    if _client is None:
        from langfuse import get_client as _get_client

        _client = _get_client()
        _apply_release(_client)
    return _client


def _apply_release(client) -> None:
    """把 LANGFUSE_RELEASE 设到客户端全局 release（trace 属性 → Release 页分组）。

    4.14.4 CallbackHandler 不读 metadata 里的 release 键，但 trace 创建时统一取
    `client._release`（init 参数或 CI 公共 env 兜底）——这里从 .env 显式注入。
    """
    rel = os.getenv("LANGFUSE_RELEASE", "") or ""
    if not rel:
        return
    try:
        client._release = rel
        _logger.info("[langfuse] release=%s（LANGFUSE_RELEASE）", rel)
    except Exception as e:  # noqa: BLE001
        _logger.debug("[langfuse] 设 release 失败: %s", e)


# ── M5 灰度：A/B 分流（进程级）────────────────────────────

def resolve_prompt_label() -> str:
    """返回本进程应使用的 prompt label（进程级，只决定一次，import 时掷骰）。

    canary 部署标准模型：N 个实例里约 ratio*N 个跑新版本。优先级：
    1. `LANGFUSE_PROMPT_LABEL` —— 显式指定（run_experiment A/B、灰度演练直接用）。
    2. `LANGFUSE_CANARY_LABEL` + `LANGFUSE_CANARY_RATIO` —— 按比例掷骰，命中走 canary。
    3. 默认 `production`（全局旧版，回滚即清空 canary 环境变量）。
    """
    global _CANARY_RESOLVED
    if _CANARY_RESOLVED is not None:
        return _CANARY_RESOLVED
    explicit = (os.getenv("LANGFUSE_PROMPT_LABEL", "") or "").strip()
    if explicit:
        _CANARY_RESOLVED = explicit
    else:
        label = (os.getenv("LANGFUSE_CANARY_LABEL", "") or "").strip()
        try:
            ratio = float(os.getenv("LANGFUSE_CANARY_RATIO", "0") or "0")
        except ValueError:
            ratio = 0.0
        if label and ratio > 0 and random.random() < ratio:
            _CANARY_RESOLVED = label
        else:
            _CANARY_RESOLVED = "production"
    _logger.info("[langfuse] prompt label=%s（A/B 分流）", _CANARY_RESOLVED)
    return _CANARY_RESOLVED


def prompt_label_info() -> dict:
    """当前进程的 prompt label + 版本 + 来源（供 trace metadata 注入，A→B 切换可见分组）。

    - `source`：当前 label 下所有已装配 prompt 的来源聚合——全部 Langfuse → langfuse；
      全部本地 → local；有 Langfuse 有本地（同进程混用）→ mixed。排查「主 Langfuse、
      子本地」这类半回退一眼可见。
    """
    label = resolve_prompt_label()
    info = {"prompt_label": label}
    ver = _PROMPT_VERSIONS.get(label)
    if ver:
        info["prompt_version"] = ver
    sources = {s for k, s in _PROMPT_SOURCE.items() if k.endswith(f"@{label}")}
    if len(sources) == 1:
        info["source"] = next(iter(sources))
    elif len(sources) > 1:
        info["source"] = "mixed"
    return info


def get_langfuse_handler():
    """返回 LangChain/LangGraph 回调 handler 单例；总开关关闭时返回 None。

    用于 graph.with_config({"callbacks": [...]})。langfuse 4.x 的 CallbackHandler
    基于 contextvar 为每次 run 创建独立 trace，可安全跨并发请求共享。

    M-T3：首次创建时 monkey-patch on_chain_start——子 agent root chain 启动时
    从 metadata.langfuse_parent_trace_id 读父 trace_id，激活 OTel NonRecordingSpan
    上下文，使子 agent 的 observations 嵌套到主 agent trace 下。
    """
    global _handler
    if not langfuse_enabled():
        return None
    if _handler is None:
        from langfuse.langchain import CallbackHandler

        _handler = CallbackHandler()
        _patch_handler_for_trace_nesting(_handler)
        # M-T6b-1：run 被取消（runs.cancel / 前端停止 / 审批超时）会让正在执行的
        # model/agent span 以 CancelledError 结束。langfuse 只把 GraphBubbleUp 等
        # 控制流异常当非错误，CancelledError 不在列 → 取消被标成红 ERROR，且
        # status_message 序列化坏（实测出现 <object object at ...>）。把
        # asyncio.CancelledError 也归入控制流异常：取消 → level=DEFAULT（不标红）
        # + status_message 可读（"User interrupted the run"）。
        try:
            import asyncio as _asyncio
            from langfuse.langchain.CallbackHandler import CONTROL_FLOW_EXCEPTION_TYPES

            CONTROL_FLOW_EXCEPTION_TYPES.add(_asyncio.CancelledError)
            _logger.debug(
                "[langfuse] CancelledError 归入控制流异常（run 取消不再标 ERROR）"
            )
        except Exception as e:  # noqa: BLE001
            _logger.debug("[langfuse] 注册 CancelledError 控制流异常失败: %s", e)
    return _handler


def get_root_observation_id(trace_id: str) -> str:
    """查 trace_id 对应的 root observation ID（M-T3b：供 skill span 作 parent_span_id）。

    由 _patch_handler_for_trace_nesting 在 on_chain_start 完成后写入。
    找不到 → 空字符串（skill span 仅用 trace_id，不指定 parent，成为 root observation）。
    """
    return _ROOT_OBS_MAP.get(trace_id, "")


def get_thread_trace_context(thread_id: str) -> tuple[str, str]:
    """查 thread 最近一次「新查询」的 (trace_id, root_observation_id)（M-T3d）。

    auto-continue run 启动时用它把续跑 observations 复用回原 trace。
    找不到 → ("", "")（auto-continue 照常新开 trace，既有行为）。
    """
    return _THREAD_TRACE_MAP.get(thread_id, ("", ""))


# ── M-T5：任务级注册表（task_id → 发起该任务的那次查询）────────────────

def register_task_trace_context(
    task_id: str,
    main_thread_id: str,
    trace_id: str,
    root_obs_id: str,
    question: str = "",
    description: str = "",
) -> bool:
    """派发异步子任务时登记：task_id → 所属查询的 trace 上下文（M-T5）。

    task_id 即子任务的独立 LangGraph thread_id。幂等：已存在不覆盖（保护原绑定，
    避免 update_async_task 等重派发误覆盖）；cap 用 FIFO 驱逐最旧条目。
    进程级，线程安全靠 GIL。返回 True=新登记，False=跳过。
    """
    if not task_id or not trace_id:
        return False
    if task_id in _TASK_TRACE_MAP:
        return False
    while len(_TASK_TRACE_MAP) >= _TASK_TRACE_CAP:
        # FIFO 驱逐最旧条目：dict 按插入序，next(iter()) 即最旧 key
        _oldest = next(iter(_TASK_TRACE_MAP), None)
        if _oldest is None:
            break
        del _TASK_TRACE_MAP[_oldest]
    _TASK_TRACE_MAP[task_id] = (
        main_thread_id or "",
        trace_id,
        root_obs_id or "",
        question or "",
        description or "",
    )
    _logger.info(
        "[langfuse_m5] task=%s registered → trace=%s obs=%s thread=%s q=%s desc=%s",
        task_id[:12], trace_id[:16], (root_obs_id or "∅")[:16],
        main_thread_id[:12], (question or "∅")[:20], (description or "∅")[:30],
    )
    return True


def get_task_trace_context(task_id: str) -> tuple[str, str, str, str, str]:
    """按 task_id 查发起该任务的那次查询的 trace 上下文（M-T5）。

    支持前缀匹配（auto-continue 正文里「任务 X（」是短 id 前 8 位）。
    找不到 → ("", "", "", "", "")（调用方回退线程级映射）。
    """
    if not task_id:
        return ("", "", "", "", "")
    hit = _TASK_TRACE_MAP.get(task_id)
    if hit is not None:
        return hit
    if len(task_id) < 36:
        for tid, entry in _TASK_TRACE_MAP.items():
            if tid.startswith(task_id):
                return entry
    return ("", "", "", "", "")


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_AC_CHECK_RE = re.compile(r'check_async_task\s*\(\s*["\']?([0-9a-fA-F-]{36})["\']?\s*\)')
_AC_TASK_RE = re.compile(r"任务\s*([0-9a-fA-F-]{8,36})\s*（")


def _extract_task_id_from_auto_continue(inputs):
    """从 auto-continue 输入中解析 task_id（M-T5）。

    三级解析，命中即返回：
      1. 消息 id：`auto-continue-{完整uuid}-{ms}`；
      2. 正文：`check_async_task("完整uuid")`；
      3. 正文：`任务 {uuid 前 8 位或完整}（`（短 id 由 get_task_trace_context 前缀匹配）。
    全部未命中 → None。
    """
    try:
        messages = inputs.get("messages") if isinstance(inputs, dict) else None
        if not isinstance(messages, (list, tuple)) or not messages:
            return None
        last = messages[-1]
        mid = ""
        content = ""
        if isinstance(last, dict):
            mid = str(last.get("id", "") or "")
            content = last.get("content", "") or ""
        else:
            mid = str(getattr(last, "id", "") or "")
            content = getattr(last, "content", "") or ""
        if isinstance(content, list):
            text = ""
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "") or ""
                    break
            content = text
        content = str(content)

        if mid.startswith("auto-continue-"):
            m = _UUID_RE.match(mid[len("auto-continue-"):])
            if m:
                return m.group(0)
        m = _AC_CHECK_RE.search(content)
        if m:
            return m.group(1)
        m = _AC_TASK_RE.search(content)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return None


def _is_auto_continue(inputs) -> bool:
    """判断 root chain 输入是否为前端 auto-continue 自动续跑通知（M-T3d）。

    auto-continue 消息特征：message.id 以 "auto-continue" 开头，或 content 以
    "[系统" 开头（系统自动通知）。命中即视为同一查询的续跑，应复用原 trace。
    """
    try:
        messages = inputs.get("messages") if isinstance(inputs, dict) else None
        if not isinstance(messages, (list, tuple)) or not messages:
            return False
        last = messages[-1]
        mid = ""
        content = ""
        if isinstance(last, dict):
            mid = str(last.get("id", "") or "")
            content = last.get("content", "") or ""
        else:
            mid = str(getattr(last, "id", "") or "")
            content = getattr(last, "content", "") or ""
        if isinstance(content, list):
            text = ""
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "") or ""
                    break
            content = text
        content = str(content)
        return mid.startswith("auto-continue") or content.startswith("[系统")
    except Exception:  # noqa: BLE001
        return False


def _fallback_thread_route(metadata: dict) -> tuple[str, str, str]:
    """auto-continue 无任务级命中时的线程级兜底路由（M-T5，行为等同原 M-T3d）。

    返回 (parent_trace_id, parent_obs_id, case)。case 区分来源便于日志定位。
    """
    _ac_tid = str((metadata or {}).get("langfuse_session_id", "") or "")
    if _ac_tid:
        _rec_trace, _rec_obs = get_thread_trace_context(_ac_tid)
        if _rec_trace:
            return _rec_trace, _rec_obs, "auto-continue(thread)"
        _logger.info(
            "[langfuse_m3d] auto-continue 命中但 thread=%s 无原 "
            "trace 记录（跨进程/重启后首查不在此进程）→ 照常新开",
            _ac_tid,
        )
    return "", "", "auto-continue(no-thread)"


def _patch_handler_for_trace_nesting(handler) -> None:
    """M-T3/T3b/T3d：monkey-patch CallbackHandler.on_chain_start。

    M-T3：子 agent root chain 启动时，从 metadata.langfuse_parent_trace_id 读
    父 trace_id，激活 OTel NonRecordingSpan 上下文 → 子 agent observations
    嵌套到主 agent trace 下。

    M-T3b：on_chain_start 完成后，抓 root observation ID 存入 _ROOT_OBS_MAP
    → LangfuseSpanMiddleware._start_span 读取作为 parent_span_id → skill span
    嵌套到 chat_agent observation 下（而非平级 root observation）。

    M-T3d：auto-continue 续跑检测（_is_auto_continue）→ 从 _THREAD_TRACE_MAP
    读该 thread 最近一次「新查询」的 (trace_id, root_obs)，注入同样的
    NonRecordingSpan context → 续跑 observations 归入原 trace，不再新开
    第二个 chat-turn trace。「新查询」（无父且非续跑）时把 (trace, root_obs)
    写入 _THREAD_TRACE_MAP。

    主 agent 新查询场景：不注入 OTel context → CallbackHandler 照常创建新
    trace，但仍记录 root observation ID + thread 映射。
    """
    original_on_chain_start = handler.on_chain_start

    def _patched_on_chain_start(
        serialized, inputs, *, run_id, parent_run_id=None,
        tags=None, metadata=None, **kwargs,
    ):
        # M-T3/T3d：父 context 检测——两类场景复用父 trace，避免新开 trace
        #   A（M-T3）：子 agent —— runs.create 注入的 metadata 带 langfuse_parent_trace_id
        #   B（M-T3d）：auto-continue 续跑 —— 末条消息是自动续跑通知，
        #     从 _THREAD_TRACE_MAP 读该 thread「新查询」的原 trace，续跑
        #     observations 归入原 trace（而非新开一个 chat-turn trace）
        parent_tid = ""
        parent_oid = ""
        parent_case = ""
        if parent_run_id is None:
            if metadata and metadata.get("langfuse_parent_trace_id"):
                parent_tid = str(metadata["langfuse_parent_trace_id"])
                parent_oid = str(metadata.get("langfuse_parent_obs_id", "") or "")
                parent_case = "subagent"
            elif _is_auto_continue(inputs):
                # M-T5：优先按 task_id 路由到发起任务的那次查询（解决连问场景
                # 任务 N 完成通知落在问题 N+1 的 run 活跃期时误挂到 N+1 的 trace）；
                # 未命中回退 M-T3d 的线程级路由（跨进程/重启/旧格式兜底）。
                _ac_task = _extract_task_id_from_auto_continue(inputs)
                if _ac_task:
                    _m5 = get_task_trace_context(_ac_task)
                    if _m5[1]:  # trace_id 非空 → 任务级命中
                        parent_tid = _m5[1]
                        parent_oid = _m5[2]
                        parent_case = f"auto-continue-task:{_ac_task[:12]}"
                    else:
                        parent_tid, parent_oid, parent_case = _fallback_thread_route(metadata)
                else:
                    parent_tid, parent_oid, parent_case = _fallback_thread_route(metadata)

        has_parent = bool(parent_tid)
        token = None
        if has_parent:
            try:
                from opentelemetry import trace as otel_trace
                from opentelemetry import context as otel_context

                # M-T3 修复（2026-08-26）：span_id 必须用父 agent 的 root
                # observation id（真实存在的 observation），不能用占位 1。
                # 用 1 时子 agent root observation 的 parent_span_id 指向不存在
                # 的 span → Langfuse 丢弃该 observation（API 404），连带其整棵
                # 子树（子 agent 的 model/tools/middleware）全部丢失。
                # _wrap_runs_create 已在 orig_create 之前把主 agent root obs id
                # 快照进 metadata.langfuse_parent_obs_id，直接用。
                #
                # M-T3c 修复 3（2026-08-26，重启后实测仍 404 的根因）：
                # trace_flags 必须置 SAMPLED(0x01)！默认 TraceFlags(0)=未采样，
                # OTel ParentBased(AlwaysOn) 采样器对未采样父 → 子 span 全部
                # NonRecordingSpan → end() 不触发 on_end → 整棵子树从未导出到
                # Langfuse 服务端（进程内 _runs 有记录、API 恒 404）。与 SDK
                # 内部 Langfuse._create_remote_parent_span 的构造参数对齐
                # （trace_flags=TraceFlags(0x01)、is_remote=False）。
                try:
                    parent_span_id = int(parent_oid, 16) if parent_oid else 1
                except (ValueError, TypeError):
                    parent_span_id = 1
                span_context = otel_trace.SpanContext(
                    trace_id=int(parent_tid, 16),
                    span_id=parent_span_id,
                    trace_flags=otel_trace.TraceFlags(0x01),  # sampled！
                    is_remote=False,
                )
                non_recording = otel_trace.NonRecordingSpan(span_context)
                ctx = otel_trace.set_span_in_context(non_recording)
                token = otel_context.attach(ctx)
                _logger.info(
                    "[langfuse_m3] %s root chain → 注入父 trace context %s "
                    "parent_obs=%s",
                    parent_case or "parent",
                    parent_tid[:16],
                    parent_oid[:16] if parent_oid else "(none→span_id=1)",
                )
            except Exception as e:
                _logger.debug("[langfuse_m3] 注入父 trace context 失败: %s", e)
                token = None

        try:
            result = original_on_chain_start(
                serialized, inputs, run_id=run_id,
                parent_run_id=parent_run_id, tags=tags,
                metadata=metadata, **kwargs,
            )
        finally:
            if token is not None:
                try:
                    from opentelemetry import context as otel_context
                    otel_context.detach(token)
                except Exception:
                    pass

        # M-T3b：root chain 完成后，记录 trace_id → root observation_id
        if parent_run_id is None:
            try:
                obs = handler._runs.get(run_id)
                if obs is not None:
                    tid = getattr(obs, "trace_id", "") or ""
                    oid = getattr(obs, "id", "") or getattr(obs, "observation_id", "") or ""
                    otel_span_id = ""
                    try:
                        ctx = obs._otel_span.get_span_context()
                        otel_span_id = format(ctx.span_id, "016x")
                    except Exception:
                        pass
                    if tid and oid:
                        _ROOT_OBS_MAP[tid] = oid
                        # M-T3d：仅「新查询」记录 thread → (trace, root_obs)，
                        # 供后续 auto-continue 复用；子 agent / auto-continue
                        # （has_parent=True）不写，避免覆盖原查询记录。
                        if not has_parent:
                            _thr = str(
                                (metadata or {}).get("langfuse_session_id", "") or ""
                            )
                            if _thr:
                                _THREAD_TRACE_MAP[_thr] = (tid, oid)
                        _logger.info(
                            "[langfuse_m3b] root obs recorded: trace=%s obs=%s "
                            "otel_span_id=%s handler.last_trace_id=%s "
                            "obs_type=%s case=%s map_size=%d",
                            tid, oid, otel_span_id,
                            getattr(handler, 'last_trace_id', '?'),
                            type(obs).__name__,
                            parent_case or "new-query", len(_ROOT_OBS_MAP),
                        )
                else:
                    _logger.info(
                        "[langfuse_m3b] _runs.get(%s) returned None! "
                        "_runs keys: %s",
                        run_id, list(handler._runs.keys())[-5:],
                    )
            except Exception as e:
                _logger.warning("[langfuse_m3b] 记录 root obs 失败: %s", e)

        return result

    handler.on_chain_start = _patched_on_chain_start

    # M-T7：叶子归巢补丁。整段仅当开关开启时装配；关闭即返回，on_chain_start 既有
    # 补丁不受影响。_LEAF_ANCHOR ContextVar 在同一线程/任务同步调用栈内由叶子包装器
    # set、_gpo 消费，天然按并发隔离（多 run 不串号）。
    if not _leaf_nesting_enabled():
        return
    _orig_gpo = handler._get_parent_observation

    def _gpo(parent_run_id):
        obs = _orig_gpo(parent_run_id)
        anchor = _LEAF_ANCHOR.get()
        # 仅当 SDK 解析到裸 client（父失联）且锚点可解析才包装；父在 _runs →
        # 正常归巢，与未 patch 逐字节一致。handler._trace_context 非空时保留
        # langgraph resume 语义，不强行覆盖。
        if (
            anchor
            and obs is getattr(handler, "_langfuse_client", None)
            and getattr(handler, "_trace_context", None) is None
        ):
            return _AnchoredClient(obs, anchor[0], anchor[1])
        return obs

    handler._get_parent_observation = _gpo

    def _leaf_wrapper(mname: str, orig):
        def w(*args, **kwargs):
            pr = kwargs.get("parent_run_id")
            lost = False
            try:
                lost = pr is None or pr not in handler._runs
            except Exception:  # noqa: BLE001
                pass
            anchor = _resolve_leaf_anchor(kwargs.get("metadata")) if lost else None
            if anchor is None:
                return orig(*args, **kwargs)
            _logger.info(
                "[langfuse_leaf] %s 父失联 → 锚定 trace=%s obs=%s case=%s",
                mname, anchor[0][:16], (anchor[1] or "(none)")[:16], anchor[2],
            )
            token = _LEAF_ANCHOR.set(anchor)
            try:
                return orig(*args, **kwargs)
            finally:
                _LEAF_ANCHOR.reset(token)

        return w

    for _m in _M_LEAF_NAMES:
        _orig_m = getattr(handler, _m, None)
        if _orig_m is None:
            continue
        setattr(handler, _m, _leaf_wrapper(_m, _orig_m))
        _logger.info("[langfuse_leaf] 已包装 %s（父失联时锚定归巢）", _m)


def get_langfuse_callbacks() -> list:
    """graph.with_config 用的 callbacks 列表（总开关关闭时为空，不挂 trace）。

    deepagents graph 挂载点统一用它，避免 LANGFUSE_ENABLE=false 时传 [None] 报错。
    """
    handler = get_langfuse_handler()
    return [handler] if handler is not None else []


def auth_check() -> bool:
    """校验与 Langfuse Cloud 的连通性（同步调用，返回 bool）。

    总开关关闭时直接返回 False；其余失败返回 False，不抛异常。
    """
    if not langfuse_enabled():
        return False
    try:
        return bool(get_client().auth_check())
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse] auth_check 失败: %s", e)
        return False


def create_score(
    name: str,
    value: float,
    trace_id: str = "",
    observation_id: str = "",
    comment: str = "",
    data_type: str = "NUMERIC",
    config: dict | None = None,
    metadata: dict | None = None,
) -> bool:
    """写一条 Langfuse Score（M3 五维评分 / 用户反馈打标）。

    create_score 入队后由 SDK 后台线程异步刷新，调用本身开销很小；任何异常
    仅 debug 告警，不影响主流程（评估是旁路，见方案文档 §3.1）。

    Args:
        name: 评分名（如 sql_valid_score / user-feedback）。
        value: 数值分（data_type="NUMERIC" 时 0~1 或 0~100）。
        trace_id: 写分目标 trace；与 observation_id 二选一（精确到 span 用后者）。
        comment: 一句话理由 / 原文。
    """
    if not langfuse_enabled():
        return False
    try:
        kwargs: dict = {"name": name, "value": value, "data_type": data_type}
        if trace_id:
            kwargs["trace_id"] = trace_id
        if observation_id:
            kwargs["observation_id"] = observation_id
        if comment:
            kwargs["comment"] = comment
        if config:
            kwargs["config"] = config
        if metadata:
            kwargs["metadata"] = metadata
        get_client().create_score(**kwargs)
        return True
    except Exception as e:  # noqa: BLE001
        _logger.debug("[langfuse] create_score(%s) 失败: %s", name, e)
        return False


# ── P0 评估可靠交付：显式 flush + atexit 兜底 ───────────────
# create_score / span 由 SDK 后台线程批量上送；进程退出前不 flush 会丢最近窗口
# 分数/trace（设计文档 NL2SQL-评估精准化设计方案.md §8.2）。flush 幂等、永不抛。

def flush_langfuse() -> None:
    """显式刷新 Langfuse SDK 上送队列（分数/trace 后台缓冲）。

    幂等：从未创建 client/handler 时 no-op；任何异常仅告警，绝不影响调用方
    （uvicorn lifespan 停机 / 脚本收尾）。同 run_experiment.py:697 的
    `get_client().flush()` 先例——handler 与 get_client 共享同一全局单例。
    """
    if _client is None and _handler is None:
        return
    try:
        _flush_sdk_clients()
        _logger.info("[langfuse] SDK 队列已显式 flush")
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse] flush 失败: %s", e)


def _flush_sdk_clients() -> None:
    """flush 进程内所有 Langfuse SDK client 实例（各自持有上送队列）。

    get_client() 与 CallbackHandler 内部共享全局单例，但各持有队列引用；
    逐个 flush 兜底 SDK 内部实现差异。单个失败不影响其余。
    """
    clients = []
    if _client is not None:
        clients.append(_client)
    if _handler is not None:
        hc = getattr(_handler, "_langfuse_client", None)
        if hc is not None and hc not in clients:
            clients.append(hc)
    for c in clients:
        try:
            c.flush()
        except Exception:  # noqa: BLE001
            pass


def _flush_on_exit() -> None:
    """atexit 兜底：覆盖不经 uvicorn lifespan 的进程（collect_badcase / feedback /
    离线实验等脚本进程）。只在建过 client/handler 时动作；异常吞掉。"""
    try:
        if _client is None and _handler is None:
            return
        _flush_sdk_clients()
    except Exception:  # noqa: BLE001
        pass


atexit.register(_flush_on_exit)


# ── M4 版本管理：Prompt 拉取 / 上传 ────────────────────────
# 策略：Langfuse 是旁路。装配时优先从 Langfuse 拉（label 切换即时回滚），
# 任何失败（未配置/超时/404）回退本地文件，不影响启动与正常装配。

def prompt_enabled() -> bool:
    """M4 开关：LANGFUSE_PROMPT_ENABLED（默认 1=走 Langfuse；0/false=强制本地）。

    回滚演练的第二手段：置 0 后重启即用回本地 prompt 文件，不动 Langfuse。
    """
    v = (os.getenv("LANGFUSE_PROMPT_ENABLED", "1") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def get_prompt_text(
    name: str,
    label: str | None = None,
    fallback: str = "",
    cache_ttl_seconds: int = 60,
    required_markers: list[str] | None = None,
    min_chars: int = 0,
) -> str:
    """拉取 Langfuse text 型 prompt 正文；失败/未启用/内容校验不过则返回 fallback。

    - `label` 缺省时走 M5 进程级 A/B 分流（resolve_prompt_label）：
      显式 LANGFUSE_PROMPT_LABEL → canary 掷骰 → production。
    - 返回 `.prompt` 原始正文（含 {{VAR}} 占位符，供装配处本地替换），
      不用 get_langchain_prompt()（它会把 {{VAR}} 转成 {VAR} 的 Langchain 格式）。
    - `required_markers` / `min_chars`：内容校验——Langfuse 正文若缺关键占位符或
      过短（如 UI 编辑把 {{CHART_SPEC}} 弄丢），视为残缺回退本地，防止带着残缺
      prompt 上线（装配处 .replace() 对缺失占位符是静默 no-op）。仅主/子系统
      prompt 装配传；sync_prompts 对比原文时缺省不做校验。
    - `max_retries=0`：SDK 默认内层重试 2 次（指数退避最长 10s）会放大 Langfuse
      宕机时的启动阻塞——兜底逻辑是我们自己的 except，SDK 重试纯属浪费，关掉。
    - 404（prompt 不存在/被改名）与网络故障分开记日志：前者是配置错误（ERROR），
      后者是可用性降级（WARNING）。
    - 拉到的版本记入 _PROMPT_VERSIONS[label]；来源记 _PROMPT_SOURCE
      （供 prompt_label_info 注入 metadata.prompt.source = langfuse|local|mixed）。
    - 总开关 LANGFUSE_ENABLE=false 时同样回退本地（比 LANGFUSE_PROMPT_ENABLED 更顶层）。
    """
    label = label if label is not None else resolve_prompt_label()
    source_key = f"{name}@{label}"

    def _local(reason: str) -> str:
        _PROMPT_SOURCE[source_key] = "local"
        _logger.debug("[langfuse] prompt %s(label=%s) 本地兜底: %s", name, label, reason)
        return fallback

    if not langfuse_enabled() or not prompt_enabled():
        return _local("未启用（LANGFUSE_ENABLE / LANGFUSE_PROMPT_ENABLED）")
    try:
        client = get_client()
        p = client.get_prompt(
            name,
            label=label,
            type="text",
            cache_ttl_seconds=cache_ttl_seconds,
            max_retries=0,
            fetch_timeout_seconds=3000,
        )
        text = getattr(p, "prompt", "") or ""
        # 内容校验：残缺的 Langfuse 正文比本地还危险，宁可回退本地
        ok_len = len(text.strip()) >= min_chars
        ok_markers = not required_markers or all(m in text for m in required_markers)
        if not text or not ok_len or not ok_markers:
            miss = [m for m in (required_markers or []) if m not in text]
            detail = miss or ("空正文" if not text else f"过短(len={len(text)})")
            _logger.warning(
                "[langfuse] prompt %s(label=%s) 内容校验不过（%s）→ 回退本地",
                name, label, detail,
            )
            return _local(f"内容校验不过:{detail}")
        ver = getattr(p, "version", "?")
        _PROMPT_VERSIONS[label] = ver
        _PROMPT_SOURCE[source_key] = "langfuse"
        _logger.info("[langfuse] prompt %s(label=%s) v%s 生效", name, label, ver)
        return text
    except Exception as e:  # noqa: BLE001
        try:
            from langfuse.api import NotFoundError

            is_404 = isinstance(e, NotFoundError)
        except Exception:  # noqa: BLE001
            is_404 = False
        if is_404:
            _logger.error(
                "[langfuse] prompt %s(label=%s) 在 Langfuse 不存在(404)：可能被改名/删除，"
                "请检查 Prompt 配置，当前回退本地: %s", name, label, e,
            )
            return _local("404 不存在")
        _logger.warning("[langfuse] prompt %s(label=%s) 拉取失败，回退本地: %s", name, label, e)
        return _local("拉取失败")


def get_prompt_version(name: str, label: str | None = None) -> int | None:
    """拉取 Langfuse text 型 prompt 的当前版本号（M6 skill 版本追踪用）。

    - `label` 缺省走 M5 进程级 A/B 分流（与 system prompt 同 label，A→B 切换一致）。
    - 失败/未启用/label 不存在（404）→ None，调用方回退本地版本（source=local）。
    - 走 SDK 缓存（cache_ttl 60s），服务启动期批量拉 14 个 skill 开销可控。
    """
    if not langfuse_enabled() or not prompt_enabled():
        return None
    if label is None:
        label = resolve_prompt_label()
    try:
        # max_retries=0：M6 启动期串行拉 15 个 skill 版本，SDK 内层重试会放大
        # Langfuse 宕机阻塞；超时 2s（版本查询只取 version，比全文装配更短）。
        p = get_client().get_prompt(
            name,
            label=label,
            type="text",
            cache_ttl_seconds=60,
            max_retries=0,
            fetch_timeout_seconds=2000,
        )
        ver = getattr(p, "version", None)
        return ver if isinstance(ver, int) else None
    except Exception:  # noqa: BLE001
        return None


def create_prompt(
    name: str,
    prompt: str,
    labels: list[str] | None = None,
    commit_message: str = "",
) -> bool:
    """上传/更新 Langfuse text 型 prompt。labels 默认 ["production", "latest"]。

    每次调用即新版本（同名同内容也会递增版本）；调用方（sync_prompts.py）应在
    内容无变化时跳过，避免无意义的版本堆积。
    """
    if not langfuse_enabled():
        return False
    try:
        kwargs: dict = {"name": name, "prompt": prompt, "type": "text"}
        kwargs["labels"] = labels or ["production", "latest"]
        if commit_message:
            kwargs["commit_message"] = commit_message
        get_client().create_prompt(**kwargs)
        return True
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse] create_prompt(%s) 失败: %s", name, e)
        return False


def update_prompt_labels(name: str, version: int, new_labels: list[str]) -> bool:
    """把某个 prompt 版本的标签整组替换（回滚/分流用）。

    Langfuse 的 labels 跨版本唯一：把 `production` 打回旧版本 vN 时，
    新版本上的 production 会被自动移除——即「production 标签随时可切回旧版本」。
    `latest` 由 Langfuse 托管（恒指最新版本），无需也不应手动维护。
    """
    if not langfuse_enabled():
        return False
    try:
        get_client().update_prompt(name=name, version=version, new_labels=new_labels)
        return True
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse] update_prompt_labels(%s v%s) 失败: %s", name, version, e)
        return False
