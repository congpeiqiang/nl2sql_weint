# -*- coding: utf-8 -*-
"""LangfuseSpanMiddleware — M2 ② 结构化层：关键工具边界包 skill 级 span。

背景（docs/langfuse平台/Langfuse接入实现方案.md §2.9）：
- CallbackHandler 已自动捕获每步 LLM/tool 调用（① 自动层），但按产物散落、
  未按 skill 分组，且丢失 OTel 上下文后叶子调用成了无 session 的孤儿 trace。
- 本中间件在关键工具边界包 skill 级 span（② 结构化层）：skill 名作 metadata
  （启发式，用于筛选而非切分）、小中间数据写 span output、vfs_path 写 metadata
  → Langfuse UI 看结构/摘要 + 磁盘文件看全量（③ 存量数据层）。

实现要点：
- skill 执行序列由 LLM 按 SKILL.md 决定，代码无法确定「当前在跑哪个 skill」→
  按确定性的产物/工具边界分段：get_db_info/describe_schema→schema 阶段、
  run_sql→生成/执行阶段、recall_queries 等。
- span 用 `client.start_observation(name=..., trace_context={"session_id": thread_id})`
  建独立 trace 但带 session → 与主/子 trace 在同一 session 分组。
- 所有 Langfuse 调用兜 try/except，异常不影响工具执行（监控旁路）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from langchain.agents.middleware import AgentMiddleware

from agent.eval.evaluators import (
    compute_schema_match_score,
    compute_sql_exec_success,
    compute_sql_valid_score,
    looks_like_exec_error,
    schedule_judge,
    should_sample,
)
from agent.eval import eval_subject  # P1 评估单元：工具边界证据 sidecar + 收尾组装
from agent.trace.langfuse_client import (
    _attach_app_root_claim,
    create_score,
    display_wrenai_tool_name,
    get_client,
    get_thread_trace_context,
    langfuse_enabled,
)

_logger = logging.getLogger(__name__)

# 工具名后缀 → skill 分类（启发式；wrenai_imdb_run_sql / dbmcp_run_sql 均命中后缀）。
# get_context/get_instructions/get_all_knowledge 是澄清/知识检索的真实工作（此前不在
# map 里 → 无 span，clarification 的语义检索在 trace 里完全不可见），补进独立族
# knowledge-retrieval（新族不进 _maybe_score 分支，不产生打分）。
TOOL_SKILL_MAP = {
    "get_db_info": "schema-linking",
    "get_mdl": "schema-linking",
    "describe_schema": "schema-linking",
    "list_models": "schema-linking",
    "list_cubes": "schema-linking",
    "describe_cube": "schema-linking",
    "query_cube": "cube-query",
    "get_context": "knowledge-retrieval",
    "get_instructions": "knowledge-retrieval",
    "get_all_knowledge": "knowledge-retrieval",
    "recall_queries": "recall-queries",
    "run_sql": "sql-execution",
    "dry_run": "sql-generation",
    "dry_plan": "sql-generation",
    "write_file": "artifact-write",
    "read_file": "artifact-read",
}

# 中间产物 VFS 前缀：{active_workspace}/nl2sql_process_data/{thread_id}/{skill}/...
VFS_PROCESS_DATA_PREFIX = "/workspace/nl2sql_process_data/"

# 服务端代写中间产物开关（NL2SQL_PROCESS_DATA_DUMP，默认开）：
# 上下文优先设计下模型不一定 write_file，但 span 已捕获工具 input/output，
# 这里在工具调用边界直接把中间产物落到磁盘，让 vfs_dir 指向的目录真实存在、
# 可排查。走磁盘写不走模型，0 额外 LLM 往返，延迟影响≈毫秒级。
_DUMP_ENABLED = (os.getenv("NL2SQL_PROCESS_DATA_DUMP", "1") or "1").strip().lower() not in (
    "0", "false", "no", "off",
)
# 是否把工具 output（查询结果 columns+rows）写入 process_data 落盘文件。
# 默认关闭：run_sql 等结果可能非常庞大，落盘会撑爆磁盘；调试需要时置 1 临时开启。
# 完整 output 始终在 Langfuse span 里，落盘只留轻量行数标记即可排查。
_DUMP_OUTPUT_ENABLED = (os.getenv("NL2SQL_PROCESS_DATA_DUMP_OUTPUT", "0") or "0").strip().lower() in (
    "1", "true", "yes", "on",
)
# 单字段（input/output）大小上限：超过则只存截断标记 + 头部，防病理大结果撑爆磁盘
_DUMP_MAX_BYTES = 8 * 1024 * 1024
# 不 dump 的文件类工具（write_file/read_file 产物本身已落盘，不重复代写）
_DUMP_SKIP_HEURISTIC = ("artifact-write", "artifact-read")

# span output 截断阈值（与 MessageSlimmerMiddleware 读同一环境变量
# LARGE_RESULT_TRUNCATE_CHARS，默认 8000）：超过才进 vfs 占位。
# 若小于 MessageSlimmer 的落盘阈值，占位符里写的具体文件名就是假的，
# 故两处必须同源对齐（此前 langfuse_span 用 16000、MessageSlimmer 运行时 8000，不一致）。
_LARGE_RESULT_LIMIT = int(
    os.environ.get("LARGE_RESULT_TRUNCATE_CHARS", "8000")
)

# M-T6b-2：主 agent trace 名（与 src/api/langfuse_metadata.py 的
# langfuse_trace_name="chat-turn" 保持一致）。path-A span（子 agent skill span）
# 显式嵌套在主 trace 下：若被 v4 误判为 root，trace 名须解析回主 trace 名。
# 不能用子 agent 上下文的 metadata.langfuse_trace_name（那是 "nl2sql-agent"，
# 子 agent 自己的名），否则主 trace 名会被污染成子 agent 名。
_MAIN_TRACE_NAME = "chat-turn"
# 工具失败时写入 span status_message 的真实错误文本上限（防超长撑爆 UI）
_ERR_MSG_LIMIT = 500


def _err_message(e: BaseException) -> str:
    """工具失败的真实错误文本（截断），替代硬编码 'tool call failed'。"""
    try:
        msg = str(e).strip()
    except Exception:  # noqa: BLE001
        msg = ""
    if not msg:
        msg = e.__class__.__name__
    return msg[:_ERR_MSG_LIMIT]


def _classify_skill(tool_name: str) -> Optional[str]:
    """按工具名后缀分类到 skill（无则 None，跳过 span）。

    注意：这是**启发式分类**（recall-queries / sql-execution 等泛化名），不是真实
    编排 skill（nl2sql-schema-linking / sql-of-thought 等）。同一工具被多个编排
    skill 共用，工具名无法区分——真实信号是 read_file 命中的 SKILL.md 路径
    （deepagents 渐进披露：执行某 skill 前必先读其 SKILL.md）。评分仍走本启发式，
    展示名用 _resolve_display_skill 解析的真实编排 skill。
    """
    for suffix, skill in TOOL_SKILL_MAP.items():
        if tool_name.endswith(suffix):
            return skill
    return None


# 线程最近加载的编排 skill：thread_id → skill 名。deepagents 渐进披露要求模型执行
# 某 skill 前先 read_file 其 SKILL.md（skills.py "How to Use Skills"）——该调用是
# 「当前在跑哪个编排 skill」的权威信号。但 skill 内容也注入系统提示词（SkillsMiddleware），
# 模型常不重读 SKILL.md 就切换 skill（实例：读 clarification 后直接跑 sql-of-thought
# 流水线），导致活动 skill 过期。故后续工具调用不是无条件继承，而是先查「工具→归属
# skill」权威表（见 _resolve_display_skill）。进程级，线程安全靠 GIL。
_THREAD_ACTIVE_SKILL: dict[str, str] = {}
_ACTIVE_SKILL_CAP = 2000
# 匹配 .../{skill}/SKILL.md 路径，提取 skill 目录名（shared/skills、skill_refs 前缀皆可）
_SKILL_MD_RE = re.compile(r"(?:^|/)(?P<skill>[A-Za-z0-9][A-Za-z0-9_.-]*)/SKILL\.md$")


def _skill_name_from_path(path: str) -> str:
    """从 VFS 路径提取编排 skill 名（read_file 命中 SKILL.md 时）。无则空串。"""
    m = _SKILL_MD_RE.search((path or "").rstrip("/"))
    return m.group("skill") if m else ""


def _set_active_skill(thread_id: str, skill: str) -> None:
    if not thread_id:
        return
    if len(_THREAD_ACTIVE_SKILL) >= _ACTIVE_SKILL_CAP:
        oldest = next(iter(_THREAD_ACTIVE_SKILL), None)
        if oldest is not None:
            del _THREAD_ACTIVE_SKILL[oldest]
    _THREAD_ACTIVE_SKILL[thread_id] = skill


# 工具 → 正向使用它的真实编排 skill（从各 SKILL.md 抽取，排除"不要/禁止"否定引用）。
# - sql-of-thought 是顶层编排器，正向执行 run_sql/list_models/query_cube 等
#   （sql-generation 的 run_sql 是"不要执行 run_sql"；schema-linking 已移除 list_models）；
# - 值长度 1 = 唯一归属 → 展示直接采用（覆盖过期活动 skill，修复"模型读完
#   clarification SKILL.md 后未重读其它 SKILL.md 就跑流水线 → list_models/run_sql
#   被误标 clarification"）；长度 >1 = 共享工具 → 活动 skill 是 owner 才继承。
_TOOL_OWNER_SKILLS: dict[str, tuple[str, ...]] = {
    # ── 唯一归属（覆盖过期活动 skill）──
    "list_models": ("nl2sql-sql-of-thought",),
    "list_cubes": ("nl2sql-sql-of-thought",),
    "describe_cube": ("nl2sql-sql-of-thought",),
    "query_cube": ("nl2sql-sql-of-thought",),
    "run_sql": ("nl2sql-sql-of-thought",),
    "dry_plan": ("nl2sql-sql-generation",),
    "get_all_knowledge": ("nl2sql-knowledge-loader",),
    # ── 共享（活动 skill 在 owners 内才继承）──
    "get_context": (
        "nl2sql-clarification", "nl2sql-schema-linking",
        "nl2sql-sql-generation", "nl2sql-sql-of-thought",
    ),
    "get_instructions": (
        "nl2sql-clarification", "nl2sql-knowledge-loader",
        "nl2sql-schema-linking", "nl2sql-sql-of-thought",
    ),
    "recall_queries": (
        "nl2sql-knowledge-loader", "nl2sql-schema-linking",
        "nl2sql-sql-of-thought", "nl2sql-sql-generation",
    ),
    "describe_schema": ("nl2sql-schema-linking", "nl2sql-sql-of-thought"),
    "get_mdl": ("nl2sql-schema-linking", "nl2sql-sql-of-thought"),
    "get_db_info": ("nl2sql-schema-linking", "nl2sql-sql-of-thought"),
    "dry_run": (
        "nl2sql-sql-generation", "nl2sql-sql-of-thought",
        "nl2sql-correction", "nl2sql-performance-optimization",
    ),
}


def _tool_owners(tool_name: str) -> Optional[tuple[str, ...]]:
    """工具名后缀 → 正向使用它的真实编排 skill 元组（无则 None）。"""
    for key, owners in _TOOL_OWNER_SKILLS.items():
        if tool_name == key or tool_name.endswith("_" + key):
            return owners
    return None


def _resolve_display_skill(tool_name: str, args: dict, thread_id: str, heuristic: str) -> str:
    """展示用 skill 名：优先真实编排 skill，回退启发式分类（评分仍走 heuristic）。

    优先级：
    1. read_file/write_file 命中 SKILL.md → 权威信号，返回该 skill 并记为线程活动 skill；
    2. read_file/write_file（未命中 SKILL.md）→ 归属当前活动 skill（文件读写是当前
       skill 执行的一部分，SKILL.md 读取与产物落盘都算）；
    3. 工具唯一归属 skill（list_models/run_sql/query_cube 等只被一个 skill 正向使用）
       → 直接返回（覆盖过期活动 skill）；
    4. 共享工具：活动 skill 是该工具的合法 owner 才继承，否则回退启发式
       （活动 skill 过期 / 不属于该工具时不强行继承，避免张冠李戴）；
    5. 均无 → 启发式分类兜底。
    """
    if tool_name in ("read_file", "write_file"):
        p = _vfs_path_from_args(tool_name, args)
        if p:
            sk = _skill_name_from_path(p)
            if sk:
                _set_active_skill(thread_id, sk)
                return sk
        act = _THREAD_ACTIVE_SKILL.get(thread_id, "") if thread_id else ""
        return act if act else heuristic
    owners = _tool_owners(tool_name)
    act = _THREAD_ACTIVE_SKILL.get(thread_id, "") if thread_id else ""
    if owners:
        if len(owners) == 1:
            return owners[0]
        if act and act in owners:
            return act
        return heuristic
    return act if act else heuristic


def _tool_name(request: Any) -> str:
    try:
        tc = getattr(request, "tool_call", None)
        if tc is not None:
            return getattr(tc, "name", "") or (tc.get("name", "") if isinstance(tc, dict) else "")
    except Exception:
        pass
    return ""


def _tool_args(request: Any) -> dict:
    try:
        tc = getattr(request, "tool_call", None)
        if tc is not None:
            # ToolCall 是 TypedDict（dict）：getattr 拿不到 args，须 .get 兜底
            if isinstance(tc, dict):
                args = tc.get("args")
                return args if isinstance(args, dict) else {}
            args = getattr(tc, "args", None)
            return args if isinstance(args, dict) else {}
    except Exception:
        pass
    return {}


def _tool_call_id(request: Any) -> str:
    """取 LangGraph tool_call 的 id（= MessageSlimmer 落盘文件名 `<tool_call_id>` 的来源）。"""
    try:
        tc = getattr(request, "tool_call", None)
        if tc is not None:
            if isinstance(tc, dict):
                return str(tc.get("id") or "")
            return str(getattr(tc, "id", "") or "")
    except Exception:
        pass
    return ""


def _sanitize_tool_call_id(tool_call_id: str) -> str:
    """文件名安全化，与 deepagents `_message_eviction.sanitize_tool_call_id` 对齐。"""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", tool_call_id)


def _thread_id(request: Any) -> str:
    """取「会话线程 id」用于 Langfuse session 分组。

    优先级（重要：子 agent 的 execution_info.thread_id 是子线程 id，会
    把 skill span 拆进另一个 session，故必须先取会话级来源）：
    1. config.metadata.langfuse_session_id —— LangfuseMetadataMiddleware 在 HTTP
       层注入的主会话 id，随 config 进入主/子 run，是分组的唯一权威来源。
    2. configurable.trace_parent_thread_id —— deepagents 子 run 透传的父线程 id。
    3. configurable.thread_id —— 主 agent 场景即主线程。
    4. request.runtime.execution_info.thread_id —— 兜底（子 agent 场景是子线程）。
    """
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        if cfg:
            meta = cfg.get("metadata") or {}
            if meta.get("langfuse_session_id"):
                return str(meta["langfuse_session_id"])
            configurable = cfg.get("configurable") or {}
            if configurable.get("trace_parent_thread_id"):
                return str(configurable["trace_parent_thread_id"])
            if configurable.get("thread_id"):
                return str(configurable["thread_id"])
    except Exception:
        pass
    try:
        runtime = getattr(request, "runtime", None)
        exec_info = getattr(runtime, "execution_info", None)
        if exec_info is not None:
            return str(getattr(exec_info, "thread_id", "") or "")
    except Exception:
        pass
    return ""


def _parent_trace_id() -> str:
    """从 LangGraph config.metadata 读取主 agent 的 trace_id（M-T2 方案 B）。

    deepagents 异步工作线程丢失 OTel context（已验证），故 _wrap_runs_create
    在主线程抓 OTel trace_id 注入 metadata.langfuse_parent_trace_id，子线程
    的 skill span 用它构造 TraceContext → start_observation 嵌套到主 trace 下。
    """
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        if cfg:
            meta = cfg.get("metadata") or {}
            tid = meta.get("langfuse_parent_trace_id", "")
            if tid:
                return str(tid)
    except Exception:
        pass
    return ""


def _question_id() -> str:
    """当前问题标识：主 run 的 Langfuse trace id（每问题 = 一条 chat-turn trace）。

    同会话多问题时，子 run 的 skill span/dump 都要区分归属哪个问题，靠
    config.metadata 里透传的主 run trace id。优先级：
    1. config.metadata.langfuse_parent_trace_id —— 子 run 场景（deepagents patch 注入）
    2. config.metadata.langfuse_trace_id —— 预留（HTTP 层未注入）
    3. 当前 OTel 活跃 span 的 trace id —— 主 run 直调工具场景兜底
    """
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        if cfg:
            meta = cfg.get("metadata") or {}
            for key in ("langfuse_parent_trace_id", "langfuse_trace_id"):
                v = meta.get(key, "")
                if v:
                    return str(v)
    except Exception:
        pass
    try:
        from opentelemetry import trace as _otel_trace
        _span = _otel_trace.get_current_span()
        _ctx = _span.get_span_context() if _span else None
        if _ctx and _ctx.is_valid:
            return format(_ctx.trace_id, "032x")
    except Exception:
        pass
    return ""


def _user_question() -> str:
    """当前问题文本（LangfuseMetadataMiddleware 每 run 注入的 user_question）。"""
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        if cfg:
            return str((cfg.get("metadata", {}) or {}).get("user_question", "") or "")
    except Exception:
        pass
    return ""


def _parent_obs_id() -> str:
    """从 LangGraph config.metadata 读取主 agent 的 root observation ID（M-T3b 修复）。

    _wrap_runs_create 在 orig_create 之前捕获主 agent 的 root observation ID
    并注入 metadata.langfuse_parent_obs_id。这避免了 _ROOT_OBS_MAP 的竞态：
    runs.create 同步执行子 agent 时，子 agent 的 on_chain_start 覆盖
    _ROOT_OBS_MAP（主/子 agent 共享 trace_id），导致后续读取拿到错误 obs_id。

    优先读 metadata（在 runs.create 前快照的值）；metadata 无值时兜底读
    _ROOT_OBS_MAP（异步 runs.create 场景下仍有效）。
    """
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        if cfg:
            meta = cfg.get("metadata") or {}
            oid = meta.get("langfuse_parent_obs_id", "")
            if oid:
                return str(oid)
    except Exception:
        pass
    return ""


def _trace_name() -> str:
    """从 LangGraph config.metadata 读取 trace 名（主 agent 为 chat-turn）。

    langfuse_metadata.py 为每个 agent run 注入 langfuse_trace_name（主 agent
    "chat-turn" / 子 agent "nl2sql-agent"）。M-T6b 用它给 path-C 的
    trace_context span 补 langfuse.trace.name，避免 v4 拿 skill span 名当 trace 名。
    """
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        if cfg:
            meta = cfg.get("metadata") or {}
            tn = meta.get("langfuse_trace_name", "")
            if tn:
                return str(tn)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _exec_thread_id(request: Any) -> str:
    """取「实际执行线程 id」用于 LLM-judge 读用户问题。

    子 agent 场景 = 子线程（其 state 首条 human 即该次查询的问题）；
    主 agent 场景 = 主线程（last human 即当轮问题）。区别于 _thread_id 的
    会话分组来源（session 永远归主线程）。
    """
    try:
        runtime = getattr(request, "runtime", None)
        exec_info = getattr(runtime, "execution_info", None)
        if exec_info is not None:
            return str(getattr(exec_info, "thread_id", "") or "")
    except Exception:
        pass
    return ""


def _vfs_path_from_args(tool_name: str, args: dict) -> str:
    """从工具参数推导 vfs 路径（write_file/read_file 的真实路径优先）。"""
    p = args.get("path") or args.get("file_path") or ""
    if p:
        return str(p)
    # 无路径参数的查询类工具：给 skill 中间产物目录占位
    return ""


def _is_report_artifact(vfs_path: str) -> bool:
    """是否为报表/报告类产物（路径含 report 或以 .md/.txt 结尾）。

    M3 报告维 judge 只对这类文件打分，避免对中间产物（如 SQL 草稿）误打。
    """
    p = (vfs_path or "").lower()
    return "report" in p or p.endswith((".md", ".txt"))


def _result_text(result: Any) -> str:
    try:
        if hasattr(result, "content"):
            return str(result.content)
        if hasattr(result, "update"):
            return str(result.update)
    except Exception:
        pass
    return ""


def _result_payload(result: Any) -> Any:
    """工具返回 → span output 的载荷（优先结构化，供 UI 折叠展示）。

    DB 工具返回 AIMessage（content=[{'type':'text','text':'<结果JSON>'},...]），
    _result_text 的 str(content) 是 Python repr 字符串（单引号），Langfuse UI
    无法把它当 JSON 折叠、整段平铺很占空间。这里解析 content-block 内层 text
    为 dict/list → UI 渲染成可折叠 JSON 树；解析失败/非 JSON 退回原字符串。
    _result_text 保留给打分用（looks_like_exec_error 等需要纯文本）。
    """
    try:
        if hasattr(result, "content"):
            raw = result.content
        elif hasattr(result, "update"):
            raw = result.update
        else:
            return ""
    except Exception:
        return ""
    # 取 content-block 里第一个 text 块的内容
    if isinstance(raw, list):
        for blk in raw:
            if isinstance(blk, dict) and blk.get("type") == "text":
                raw = blk.get("text", "")
                break
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return raw
        try:
            parsed = json.loads(s)
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            pass
        return raw
    return str(raw)


def _output_marker(result: Any) -> dict:
    """工具 output 的轻量占位（默认不落盘完整结果，防查询结果撑爆磁盘）。

    保留行数/条数便于排查归属；完整 output 在 Langfuse span 里可随时回看。
    """
    try:
        payload = _result_payload(result)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        rows = payload.get("rows")
        if isinstance(rows, list):
            return {"_omitted": True, "rows_count": len(rows)}
        return {"_omitted": True}
    if isinstance(payload, list):
        return {"_omitted": True, "count": len(payload)}
    return {"_omitted": True}


class LangfuseSpanMiddleware(AgentMiddleware):
    """关键工具边界包 skill 级 span（② 结构化层）。

    agent_name：挂到哪个 agent 就在 span metadata 里标哪个（nl2sql_agent / chat_agent）。
    """

    def __init__(self, db_name_default: str = "", agent_name: str = "nl2sql_agent"):
        self._db_name_default = db_name_default
        self._agent_name = agent_name

    # ── 同步 ─────────────────────────────────────────────

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        skill = _classify_skill(_tool_name(request))
        if not skill:
            return handler(request)
        return self._invoke(request, handler, skill)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        skill = _classify_skill(_tool_name(request))
        if not skill:
            result = handler(request)
            if hasattr(result, "__await__"):
                return await result
            return result
        return await self._invoke_async(request, handler, skill)

    # ── 公共执行 ─────────────────────────────────────────

    def _span_meta(self, tool_name: str, skill: str, thread_id: str, db_name: str,
                   args: dict, heuristic: str = "") -> dict:
        meta = {
            "skill": skill,
            "tool": tool_name,
            "thread_id": thread_id,
            "agent": self._agent_name,
        }
        # 展示名 skill 是真实编排 skill 时，补启发式分类供按原维度筛选/评分定位
        if heuristic and heuristic != skill:
            meta["heuristic"] = heuristic
        if db_name:
            meta["db_name"] = db_name
        vfs = _vfs_path_from_args(tool_name, args)
        if vfs:
            meta["vfs_path"] = vfs
        else:
            # 查询类工具无文件路径，标出 skill 产物目录供排查
            meta["vfs_dir"] = f"{VFS_PROCESS_DATA_PREFIX}{thread_id}/{skill}/"
        return meta

    def _invoke(self, request: Any, handler: Callable[[Any], Any], heuristic: str) -> Any:
        tool_name = _tool_name(request)
        args = _tool_args(request)
        thread_id = _thread_id(request)
        exec_thread = _exec_thread_id(request)
        db_name = self._db_name_default or args.get("db_name", "")
        # 展示用 skill 名：真实编排 skill（read_file SKILL.md 信号 / 线程最近加载），
        # 回退启发式；评分仍走 heuristic（_maybe_score 的 sql-execution 等判断不变）。
        display = _resolve_display_skill(tool_name, args, thread_id, heuristic)
        span = self._start_span(tool_name, display, thread_id, db_name, args,
                                heuristic=heuristic)
        result = None
        try:
            result = handler(request)
        except Exception as e:
            self._end_span(span, level="ERROR", status_message=_err_message(e))
            self._maybe_score(tool_name, heuristic, args, result, ok=False, span=span,
                              exec_thread=exec_thread, error=e)
            raise
        self._finish_span(span, result, tool_name, _tool_call_id(request))
        self._dump_process_data(tool_name, args, result, thread_id, display, heuristic)
        self._record_subject_evidence(tool_name, heuristic, display, args, result, thread_id)
        self._maybe_score(tool_name, heuristic, args, result, ok=True, span=span,
                          exec_thread=exec_thread, error="")
        return result

    async def _invoke_async(self, request: Any, handler: Callable[[Any], Any], heuristic: str) -> Any:
        tool_name = _tool_name(request)
        args = _tool_args(request)
        thread_id = _thread_id(request)
        exec_thread = _exec_thread_id(request)
        db_name = self._db_name_default or args.get("db_name", "")
        display = _resolve_display_skill(tool_name, args, thread_id, heuristic)
        span = self._start_span(tool_name, display, thread_id, db_name, args,
                                heuristic=heuristic)
        result = None
        try:
            result = handler(request)
            if hasattr(result, "__await__"):
                result = await result
        except Exception as e:
            self._end_span(span, level="ERROR", status_message=_err_message(e))
            self._maybe_score(tool_name, heuristic, args, result, ok=False, span=span,
                              exec_thread=exec_thread, error=e)
            raise
        self._finish_span(span, result, tool_name, _tool_call_id(request))
        self._dump_process_data(tool_name, args, result, thread_id, display, heuristic)
        self._record_subject_evidence(tool_name, heuristic, display, args, result, thread_id)
        self._maybe_score(tool_name, heuristic, args, result, ok=True, span=span,
                          exec_thread=exec_thread, error="")
        return result

    # ── M3 五维评分：span 结束时写确定性分 + 抽样调度 LLM-judge ──
    # 全部旁路容错：任何异常仅 debug 日志，不影响工具执行（评估是旁路）。
    def _maybe_score(self, tool_name: str, skill: str, args: dict, result: Any,
                     ok: bool, span, exec_thread: str, error: Any) -> None:
        if not langfuse_enabled() or span is None:
            return
        try:
            trace_id = getattr(span, "trace_id", "") or ""
            if not trace_id:
                return
            if skill == "sql-execution":
                sql = str(args.get("sql", "") or "")
                if sql:
                    s, comment = compute_sql_valid_score(sql)
                    create_score(name="sql_valid_score", value=s, trace_id=trace_id,
                                 comment=comment)
                # dbmcp_run_sql 等工具把 DB 报错作为返回值（非异常）——须从结果文本识别
                result_txt = _result_text(result)
                err = str(error or "")
                if not err and result_txt and looks_like_exec_error(result_txt):
                    err = result_txt
                exec_ok = bool(ok) and not looks_like_exec_error(result_txt)
                ex_ok, ex_comment = compute_sql_exec_success(exec_ok, err)
                create_score(name="sql_exec_success", value=ex_ok, trace_id=trace_id,
                             comment=ex_comment)
                # 执行失败不再调 LLM-judge（省成本；badcase 采集按 sql_exec_success=0 命中）
                if exec_ok and should_sample():
                    schedule_judge("sql_biz_correct", trace_id, exec_thread,
                                   sql=sql, result=result_txt)
            elif skill == "schema-linking":
                s, comment = compute_schema_match_score(ok, str(error or ""))
                create_score(name="schema_match_score", value=s, trace_id=trace_id,
                             comment=comment)
            elif skill in ("artifact-write", "artifact-read") and ok:
                # 报表/报告产物 span → report 维度 LLM-judge（采样）。
                # 实测报表由子 agent 写入（主 agent 异步委派后即返回「查询已提交」），
                # 故不限 agent；只对报告类文件打分，且报告正文取 write_file 的 content 参数
                #（工具返回值只是「已写入」确认，不含正文）。
                vfs = _vfs_path_from_args(tool_name, args)
                # 编排 skill 的 SKILL.md 读取/加载不是报表产物（_is_report_artifact
                # 把一切 .md 都当 report），须排除，否则读 skill 指令会打 report 分
                if _skill_name_from_path(vfs):
                    return
                if _is_report_artifact(vfs):
                    report_text = str(args.get("content") or "") or _result_text(result)
                    if report_text and should_sample():
                        schedule_judge("report", trace_id, exec_thread,
                                       report=report_text)
        except Exception as e:  # noqa: BLE001
            _logger.debug("[langfuse_span] 评分写入失败: %s", e)

    # ── P1 评估单元：工具边界证据 sidecar + 主 agent run 收尾组装 ──
    # （docs/langfuse平台/NL2SQL-评估精准化设计方案.md §4，P1 片1。旁路容错：
    # 任何异常仅 debug 日志，不改 agent 状态、不改现有 span/分数/process_data。）

    def _record_subject_evidence(self, tool_name: str, heuristic: str, display: str,
                                 args: dict, result: Any, thread_id: str) -> None:
        """工具成功返回后，把 run_sql 完整数字载荷 / report 正文头部写入证据 sidecar。

        主线程看不到子 agent 线程 run_sql 的 row_count/head_rows（check 摘要只有
        文本且 MessageSlimmer 已瘦身）→ 在此边界（完整 payload 还在）落盘，主 run
        收尾组装评估单元时读盘补齐。subject_id 用 _question_id()：子线程工具执行时
        返回主 run trace id → 同一次查询（= 一条 chat-turn trace）的证据归一到同一
        sidecar。
        """
        if not langfuse_enabled():
            return
        try:
            subject_id = _question_id()
            root = _active_workspace_path()
            if not subject_id or not thread_id or not root:
                return
            if heuristic == "sql-execution":
                sql = str(args.get("sql", "") or "")
                if not sql:
                    return
                payload = _result_payload(result)
                result_txt = _result_text(result)
                err = ""
                if result_txt and looks_like_exec_error(result_txt):
                    err = result_txt  # dbmcp 等把 DB 报错当返回值
                entry = eval_subject.run_sql_evidence_entry(
                    sql, ok=not looks_like_exec_error(result_txt), error=err,
                    skill=display, payload=payload,
                )
                eval_subject.append_evidence(root, thread_id, subject_id, run_sql=entry)
            elif heuristic in ("artifact-write", "artifact-read"):
                # report 类产物（镜像 _maybe_score report 分支守卫：排除 SKILL.md 读取）
                vfs = _vfs_path_from_args(tool_name, args)
                if _skill_name_from_path(vfs):
                    return
                if _is_report_artifact(vfs):
                    rep = eval_subject.report_evidence_entry(args)
                    if rep:
                        eval_subject.append_evidence(root, thread_id, subject_id, report=rep)
        except Exception as e:  # noqa: BLE001
            _logger.debug("[langfuse_span] 评估单元证据捕获失败: %s", e)

    def after_agent(self, state: Any, runtime: Any) -> dict | None:
        try:
            self._assemble_subject(state, runtime)
        except Exception as e:  # noqa: BLE001
            _logger.debug("[langfuse_span] 评估单元组装失败: %s", e)
        return None  # 恒不改 agent state（本中间件不做状态更新）

    async def aafter_agent(self, state: Any, runtime: Any) -> dict | None:
        try:
            self._assemble_subject(state, runtime)
        except Exception as e:  # noqa: BLE001
            _logger.debug("[langfuse_span] 评估单元组装失败: %s", e)
        return None

    def _assemble_subject(self, state: Any, runtime: Any) -> None:
        """主 agent（chat_agent）run 收尾：读证据 + 组评估单元 + 落盘。

        auto-continue 多 run 幂等：非终结 run（子任务未完成，主消息无产出 SQL）
        组不出 subject → 不写；终结 run（成功 check + 最终回答在）→ 写出。
        subject_id = chat-turn trace id（_THREAD_TRACE_MAP 只在「新查询」覆盖 →
        auto-continue run 也返回本查询原 trace）。
        """
        if self._agent_name != "chat_agent":
            return  # nl2sql 子 agent 实例空转（给子图加了个空节点，安全）
        if not langfuse_enabled():
            return
        try:
            messages = state.get("messages") if isinstance(state, dict) else getattr(
                state, "messages", None,
            )
            if not messages:
                return
            meta, configurable = self._assemble_config(runtime)
            session_thread_id = str(meta.get("langfuse_session_id", "")
                                    or configurable.get("thread_id", "") or "")
            if not session_thread_id:
                return
            subject_id = get_thread_trace_context(session_thread_id)[0]
            if not subject_id:
                subject_id = str(meta.get("langfuse_parent_trace_id", "") or "")
            if not subject_id:
                subject_id = _question_id()
            if not subject_id:
                return
            root = _active_workspace_path()
            if not root:
                return
            question = str(meta.get("user_question", "") or "")
            if not question:
                turn = eval_subject.messages_since_last_human(list(messages))
                for m in reversed(turn):
                    if eval_subject._msg_role(m) not in ("human", "user"):
                        continue
                    question = eval_subject._msg_content_text(m).strip()
                    if question:
                        break
            db_name = str(meta.get("db_name", "") or configurable.get("db_name", "") or "")
            evidence = eval_subject.read_evidence(root, session_thread_id, subject_id)
            subject = eval_subject.compose_subject(
                context={
                    "subject_id": subject_id,
                    "ts": eval_subject._now(),
                    "question": question,
                    "db_name": db_name,
                    "session_thread_id": session_thread_id,
                },
                messages=list(messages),
                evidence=evidence,
            )
            if subject:
                eval_subject.write_subject(root, session_thread_id, subject_id, subject)
                _logger.info(
                    "[langfuse_span] 评估单元已组装 subject=%s sql=%s", subject_id[:8],
                    (subject.get("final_sql") or "")[:60].replace("\n", " "),
                )
        except Exception as e:  # noqa: BLE001
            _logger.debug("[langfuse_span] 评估单元组装失败: %s", e)

    @staticmethod
    def _assemble_config(runtime: Any) -> tuple[dict, dict]:
        """after_agent 内读请求 metadata/configurable（get_config 存活则用，否则 runtime.config）。"""
        cfg: Any = None
        try:
            from langgraph.config import get_config as _lg_get_config
            cfg = _lg_get_config()
        except Exception:  # noqa: BLE001
            cfg = None
        if cfg is None:
            cfg = getattr(runtime, "config", None)
        if isinstance(cfg, dict):
            return dict(cfg.get("metadata") or {}), dict(cfg.get("configurable") or {})
        meta = getattr(cfg, "metadata", None) or {}
        conf = getattr(cfg, "configurable", None) or {}
        return dict(meta), dict(conf)

    # ── Langfuse 封装（全部容错，监控旁路）────────────────

    def _start_span(self, tool_name: str, skill: str, thread_id: str, db_name: str, args: dict,
                    heuristic: str = ""):
        if not langfuse_enabled():
            return None
        try:
            client = get_client()
            # M-T2 方案 B：deepagents 异步线程丢失 OTel context，
            # 从 config.metadata 读主线程注入的 parent trace_id，
            # 通过 trace_context 让 skill span 嵌套到主 agent trace 下。
            # 2026-09-04：span 显示名里 wrenai 净化前缀换回库名全名（display-only；
            # metadata["tool"] 仍是真实工具名，打分/分类/落盘不受影响）。
            obs_kwargs = {
                "name": f"skill:{skill}:{display_wrenai_tool_name(tool_name)}",
                "as_type": "span",
                "input": _compact(args, 2000),
                "metadata": self._span_meta(tool_name, skill, thread_id, db_name, args, heuristic),
            }
            parent_tid = _parent_trace_id()
            _path_c = False  # M-T6b：路径 C 标志（主 agent 直接调工具）
            _path_a = False  # M-T6b-2：路径 A 标志（子 agent skill span，嵌套主 trace）
            if parent_tid:
                _path_a = True
                tc = {"trace_id": parent_tid}
                # M-T3b：查 root observation ID 作为 parent，让 skill span 嵌套到
                # agent root observation 下（而非平级 root observation）。
                #
                # 优先读 _ROOT_OBS_MAP：skill span 在子 agent 内执行，此时 map 里
                # 是子 agent 的 root observation（子 agent on_chain_start 已写入，
                # M-T3 修复后该 observation 真实存在）→ skill span 嵌套到子 agent
                # 下，形成 chat_agent → nl2sql_agent → skill 的标准层级。
                # 兜底读 metadata（_wrap_runs_create 在 orig_create 前快照的主
                # agent root obs）：map 未命中（异步时序/无子 agent）时仍嵌套到
                # 主 agent 下，不会成为孤儿。
                root_oid = ""
                source = "(none)"
                try:
                    from agent.trace.langfuse_client import get_root_observation_id
                    root_oid = get_root_observation_id(parent_tid)
                    if root_oid:
                        source = "map"
                except Exception:
                    pass
                if not root_oid:
                    root_oid = _parent_obs_id()
                    if root_oid:
                        source = "metadata"
                if root_oid:
                    tc["parent_span_id"] = root_oid
                _logger.info(
                    "[langfuse_span] M-T3b: skill=%s parent_tid=%s "
                    "parent_obs_id=%s source=%s",
                    skill, parent_tid[:16], root_oid[:16] if root_oid else "(none)",
                    source,
                )
                obs_kwargs["trace_context"] = tc
            elif thread_id:
                # M-T6（路径 C）：主 agent 直接调工具场景 —— metadata 没有
                # langfuse_parent_trace_id（该值仅 deepagents 子 run 注入）。主 agent
                # 工具调用若 OTel 上下文已丢失，skill span 会裸开新 root trace 成孤儿，
                # 用进程内「会话线程 → 最近一次新查询的 (trace, root_obs)」映射显式嵌套。
                #
                # 优先级：
                #   1. _THREAD_TRACE_MAP[thread_id] —— 该会话最近一次新查询的
                #      (trace_id, root_observation_id)（主/auto-continue run 的
                #      on_chain_start 写入/保留，按会话线程 key，无跨会话串号）。
                #   2. handler.last_trace_id 兜底 —— CallbackHandler 单例最近一次
                #      root chain 的 trace_id（跨会话竞态风险，仅线程 map 未命中时）。
                # 两者皆空 → 保持原行为（裸 start_observation + session 属性兜底）。
                #
                # M-T6b（修复 B）：先探 OTel 上下文。若仍活跃（get_current_span 是
                # recording span，主 agent 链路未断），span 会自然挂到当前父 span 下
                # → root=False、与父同批导出（v4 不再误判 root）。只有 OTel 上下文
                # 已丢失时才用 trace_context 显式嵌套（该路径的 span 会被 v4 判为
                # root，见下方修复 A 的 trace_name 补偿）。
                _path_c = True
                c_tid, c_oid = "", ""
                try:
                    from agent.trace.langfuse_client import get_thread_trace_context
                    c_tid, c_oid = get_thread_trace_context(thread_id)
                except Exception:  # noqa: BLE001
                    pass
                if not c_tid:
                    try:
                        from agent.trace.langfuse_client import get_langfuse_handler
                        _h = get_langfuse_handler()
                        _lt = getattr(_h, "last_trace_id", "") if _h is not None else ""
                        if _lt:
                            c_tid = str(_lt)
                            from agent.trace.langfuse_client import get_root_observation_id
                            c_oid = get_root_observation_id(c_tid)
                    except Exception:  # noqa: BLE001
                        pass
                otel_active = False
                try:
                    from opentelemetry.trace import get_current_span
                    otel_active = bool(get_current_span().is_recording())
                except Exception:  # noqa: BLE001
                    pass
                if c_tid and not otel_active:
                    tc = {"trace_id": c_tid}
                    if c_oid:
                        tc["parent_span_id"] = c_oid
                    obs_kwargs["trace_context"] = tc
                    _logger.info(
                        "[langfuse_span] M-T6: skill=%s 主agent 显式嵌套 "
                        "→ trace=%s obs=%s otel_active=%s",
                        skill, c_tid[:16], c_oid[:16] if c_oid else "(none)",
                        otel_active,
                    )
            # M-T8b：显式 trace_context 归巢（path A 子 agent skill span / path C
            # 主 agent 工具 span）创建的观测，父观测早于它导出（跨线程/已 end）→
            # SDK 无 parent_expected_exported 抑制 → 误判 is_app_root=true → v4
            # Traces 列表同一 trace 多一行。与 M-T7 归巢同机制：创建瞬间注入
            # langfuse_trace_id baggage 认领，触发 suppressed_by_parent_claim，
            # 让归巢 skill span 不再成为独立 UI 行（真 root main_agent 不受影响）。
            _tc = obs_kwargs.get("trace_context") or {}
            _claim_tid = _tc.get("trace_id") if isinstance(_tc, dict) else None
            _claim_token = _attach_app_root_claim(_claim_tid) if _claim_tid else None
            try:
                span = client.start_observation(**obs_kwargs)
            finally:
                if _claim_token is not None:
                    try:
                        from opentelemetry import context as otel_context

                        otel_context.detach(_claim_token)
                    except Exception:  # noqa: BLE001
                        pass
            # M-T6b（修复 A）：trace_context 显式嵌套的 span 其父 observation 早于它
            # 导出、不在同一批 → v4 把它们判为 root observation，而 v4 的 trace 名 =
            # 最新 root observation 的 trace_name → 会用 skill span 名当 trace 名。
            # 给 span 补 langfuse.trace.name，让 v4 trace 名解析回正确值。与 session.id
            # 同理：start_observation 后设 span 属性，导出时随 span 上送（已用
            # langfuse.trace.tags 同机制验证）。
            #
            # path-C（主 agent）：读当前 agent 上下文 metadata.langfuse_trace_name
            # （主 agent 即 chat-turn）。
            # path-A（子 agent skill span）：嵌套在主 trace 下，且子 agent 上下文里
            # metadata.langfuse_trace_name 是 "nl2sql-agent"（子 agent 自己的名）——
            # 必须用 _MAIN_TRACE_NAME（主 trace 名 chat-turn），否则 v4 若把该 span
            # 判 root，主 trace 名会被污染成子 agent 名。
            if _path_c or _path_a:
                try:
                    _tn = _MAIN_TRACE_NAME if _path_a else _trace_name()
                    if _tn:
                        span._otel_span.set_attribute("langfuse.trace.name", _tn)
                except Exception as e:  # noqa: BLE001
                    _logger.debug("[langfuse_span] set trace_name attr failed: %s", e)
            # session 分组：span 若落在 agent run 的 OTel 上下文内会自动继承其 session；
            # 若上下文丢失（deepagents 异步工作线程的孤儿场景，实测 get_current_trace_id
            # 为空），span 会成为新 root trace——给根 span 的 otel 属性设 session.id，
            # Langfuse exporter 会把它作为该 trace 的 session_id（已隔离验证）。
            # 对已继承 session 的子 span 设置同名属性无害（trace 级字段只从根 span 读）。
            if thread_id:
                try:
                    span._otel_span.set_attribute("session.id", thread_id)
                    span._otel_span.set_attribute("langfuse.trace.tags", ["nl2sql"])
                except Exception as e:  # noqa: BLE001
                    _logger.debug("[langfuse_span] set session attr failed: %s", e)
            return span
        except Exception as e:  # noqa: BLE001
            _logger.debug("[langfuse_span] start failed: %s", e)
            return None

    def _finish_span(self, span, result: Any, tool_name: str, tool_call_id: str = "") -> None:
        if span is None:
            return
        try:
            payload = _result_payload(result)
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            # 阈值与 MessageSlimmer 读同一环境变量 LARGE_RESULT_TRUNCATE_CHARS：
            # ≤阈值直接进 output（结构化 payload 让 UI 渲染可折叠 JSON 树）；
            # 超过则 MessageSlimmer 已落盘 {active_workspace}/large_tool_results/<tool_call_id>
            if len(text) <= _LARGE_RESULT_LIMIT:
                span.update(output=payload)
            else:
                vfs = (
                    f"/workspace/large_tool_results/{_sanitize_tool_call_id(tool_call_id)}"
                    if tool_call_id else "/workspace/large_tool_results/"
                )
                span.update(
                    output=f"[large result truncated, see {vfs}]",
                    metadata={"vfs_path": vfs},
                )
            span.end()
        except Exception as e:  # noqa: BLE001
            _logger.debug("[langfuse_span] finish failed: %s", e)

    def _end_span(self, span, level: str = "DEFAULT", status_message: str = "") -> None:
        if span is None:
            return
        try:
            span.update(level=level, status_message=status_message)
            span.end()
        except Exception:  # noqa: BLE001
            pass

    def _dump_process_data(self, tool_name: str, args: dict, result: Any,
                           thread_id: str, skill: str, heuristic: str) -> None:
        """服务端把中间产物落盘（0 模型开销，替代模型 write_file 的高成本方案）。

        上下文优先设计下模型不一定 write_file，但工具调用边界 span 已捕获完整
        input/output——这里直接代写为 process_data/{thread}/{skill}/{tool}-{seq}.json，
        让 vfs_dir 指向的目录真实存在、可排查。纯磁盘写（毫秒级），失败仅 debug，
        不影响工具执行。目录名/文件名与 span metadata 的 vfs_dir 同源（同一 display skill）。
        """
        if not _DUMP_ENABLED or not thread_id or not skill:
            return
        if heuristic in _DUMP_SKIP_HEURISTIC:
            return  # 文件类工具产物本身已落盘，不重复
        try:
            root = _active_workspace_path()
            if not root:
                return
            skill_dir = Path(root) / "nl2sql_process_data" / thread_id / skill
            skill_dir.mkdir(parents=True, exist_ok=True)
            seq = 1
            try:
                seq = len([f for f in skill_dir.iterdir() if f.suffix == ".json"]) + 1
            except Exception:  # noqa: BLE001
                seq = 1
            # 问题维度：同会话多问题 → 用主 run trace id（每问题=一条 chat-turn trace）
            # + 问题文本区分归属，文件名加问题短 id 前缀（同一问题的产物排在一起）
            question_id = _question_id()
            user_question = _user_question()
            payload = {
                "tool": tool_name,
                "skill": skill,
                "heuristic": heuristic,
                "thread_id": thread_id,
                "question_id": question_id,
                "user_question": user_question,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "input": _cap_json(args, _DUMP_MAX_BYTES),
                "output": _cap_json(_result_payload(result), _DUMP_MAX_BYTES)
                if _DUMP_OUTPUT_ENABLED
                else _output_marker(result),
            }
            blob = json.dumps(payload, ensure_ascii=False, default=str)
            qprefix = question_id[:8] if question_id else ""
            fname = f"{qprefix}_{tool_name}-{seq}.json" if qprefix else f"{tool_name}-{seq}.json"
            (skill_dir / fname).write_text(blob, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            _logger.debug("[langfuse_span] process_data dump 失败: %s", e)


def _compact(data: Any, limit: int) -> Any:
    """截断大 dict/list，避免 span input 撑爆。"""
    try:
        if isinstance(data, dict):
            return {k: _compact(v, limit) for k, v in list(data.items())[:10]}
        if isinstance(data, str):
            return data[:limit] + ("..." if len(data) > limit else "")
        if isinstance(data, (list, tuple)):
            return [_compact(v, limit) for v in list(data)[:10]]
    except Exception:  # noqa: BLE001
        return str(data)[:limit]
    return data


def _cap_json(value: Any, limit: int) -> Any:
    """值序列化后超限 → 替换为截断标记 + 头部（保持文件是合法 JSON）。"""
    try:
        s = json.dumps(value, ensure_ascii=False, default=str)
        if len(s) <= limit:
            return value
        return {"_truncated": True, "size_chars": len(s), "head": s[:2000]}
    except Exception:  # noqa: BLE001
        return str(value)[:limit]


def _active_workspace_path() -> str:
    """读当前请求的 active workspace 磁盘路径。

    优先读 config.metadata.workspace.path（LangfuseMetadataMiddleware 注入的
    请求级权威值，磁盘路径）；无则回退 workspace_manager 的 active workspace。
    """
    try:
        from langgraph.config import get_config as _lg_get_config
        cfg = _lg_get_config()
        if cfg:
            ws = (cfg.get("metadata") or {}).get("workspace") or {}
            p = ws.get("path") if isinstance(ws, dict) else ""
            if p:
                return str(p)
    except Exception:  # noqa: BLE001
        pass
    try:
        from agent.workspace_manager import get_workspace_manager
        return str(get_workspace_manager().active_workspace)
    except Exception:  # noqa: BLE001
        return ""
