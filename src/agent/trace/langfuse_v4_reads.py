# -*- coding: utf-8 -*-
"""Langfuse v4 原生读 API 封装（events_only/dual 兼容，2026-08-24 新增）。

背景：自托管 Langfuse 升到 v4 平台后，写模式默认 events_only——新的
trace/observation/score 全部落 events 新表（ClickHouse events_core/events_full），
legacy 读接口（GET /api/public/traces、GET /api/public/scores）读不到这些数据
（events_only 下直接 404；dual 下返回 200 但空）。评估/打标/采集脚本必须改走
v4 原生读接口：

  - GET /api/public/v2/observations  —— trace/observation 数据（events 表）
  - GET /api/public/v3/scores        —— score 数据（events 表），SDK scores_v3.get_many_v3

v4 没有独立的 trace 资源：主 trace 由 root observation 表达。本应用埋点中
主 trace 是 AGENT 根 observation（name=chat_agent / 子 nl2sql_agent），
sessionId=thread_id，trace 级业务元数据（prompt.label / workspace / skills / db_name /
thread_id）落在该 root observation 的 metadata 上（已实测 8-24 数据验证）。
本模块把「按 session 定位主 trace / 取 trace metadata / 取 score」等 v4 语义
收敛成原 legacy 读接口的同名功能，供 message_feedback / feedback_gate /
collect_badcase 无痛替换。

约定：调用方统一经 agent.trace.langfuse_client.get_client() 拿客户端；任何失败
返回空结构，调用方自行兜底（评估/打标/采集是旁路，见方案文档 §3.1）。
"""
from __future__ import annotations

import ast
import json
import logging
from datetime import datetime, timezone

_logger = logging.getLogger(__name__)

# v4 events 表没有 score 删除 API（legacy DELETE 只删 legacy 表，实测删不到
# events 数据），「撤销反馈」用哨兵分表达：value<0 = 已撤销。读取端约定
# 「最新一条 user-feedback 即当前状态」：-1=已撤销（忽略）、0=差评、1=好评。
USER_FEEDBACK_REVOKED = -1.0


def is_revoked(value) -> bool:
    """value<0 视为撤销哨兵（正常反馈只可能是 0/1）。"""
    try:
        return float(value) < 0
    except (TypeError, ValueError):
        return False


# ── 小工具 ──────────────────────────────────────────────────

def _obs_filter(*conds: dict) -> str:
    """构造 v2/observations 的 filter JSON（多个条件 AND）。"""
    return json.dumps(list(conds))


def _root_obs_filter(trace_id: str) -> str:
    """按 traceId 取该 trace 的 root observation。"""
    return _obs_filter(
        {"type": "string", "column": "traceId", "operator": "=", "value": trace_id},
        {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True},
    )


def _session_root_obs_filter(thread_id: str) -> str:
    """按 sessionId=thread_id 取该会话所有 root observation。"""
    return _obs_filter(
        {"type": "string", "column": "sessionId", "operator": "=", "value": thread_id},
        {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True},
    )


def _as_dict(v):
    """observations 的 metadata/input 可能是 dict 或 JSON 字符串，统一成 dict。"""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except Exception:  # noqa: BLE001
            pass
    return {}


def _parse_str_dict(v) -> dict:
    """Langfuse 注入的业务 metadata 可能被字符串化（如 prompt="{...}"），还原 dict。"""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        for fn in (json.loads, ast.literal_eval):
            try:
                r = fn(v)
                if isinstance(r, dict):
                    return r
            except Exception:  # noqa: BLE001
                continue
    return {}


def _subject_trace_id(score) -> str:
    """v3 score 挂载目标：subject.kind=trace → id；observation → trace_id。"""
    sub = getattr(score, "subject", None)
    if sub is None:
        return ""
    kind = getattr(sub, "kind", "") or ""
    if kind == "trace":
        return getattr(sub, "id", "") or ""
    if kind == "observation":
        return getattr(sub, "trace_id", None) or ""
    return ""


def _iter_cursor(fetch_fn, limit_each: int = 100):
    """通用 v4 游标分页迭代。fetch_fn(cursor) 返回 (batch, next_cursor|None)。"""
    cursor = None
    guard = 0
    while True:
        guard += 1
        if guard > 200:  # 最多 2 万条，防失控
            _logger.warning("[langfuse_v4] 分页超上限，截断")
            break
        batch, nxt = fetch_fn(cursor)
        if not batch:
            break
        for item in batch:
            yield item
        if not nxt:
            break
        cursor = nxt


# ── 会话 → 主 trace ─────────────────────────────────────────

def find_session_main_trace_id(thread_id: str) -> str:
    """按 sessionId=thread_id 找该会话主 trace id（AGENT 根 chat_agent 取最新）。

    替代 legacy `trace.list(session_id=...)`。一个会话可能多次 run（auto-continue）
    产生多个 chat_agent root，取 startTime 最新的一条作为「当前反馈目标」。
    失败/无 trace 返回空串（调用方跳过打分）。
    """
    try:
        from agent.trace.langfuse_client import get_client

        client = get_client()
        flt = _session_root_obs_filter(thread_id)
        resp = client.api.observations.get_many(
            fields="core,basic",  # core 含 type/startTime；basic 含 name/sessionId
            limit=50,
            filter=flt,
        )
        rows = list(resp.data or [])
        # AGENT 根主 trace 优先（chat_agent 是主 agent；nl2sql_agent 是子 agent，不用）
        main: list[tuple[datetime, str]] = []
        fallback: list[str] = []
        for o in rows:
            tid = getattr(o, "trace_id", "") or ""
            if not tid:
                continue
            if getattr(o, "type", "") == "AGENT" and (getattr(o, "name", "") or "") == "chat_agent":
                st = getattr(o, "start_time", None)
                main.append((st if isinstance(st, datetime) else datetime.min.replace(tzinfo=timezone.utc), tid))
            else:
                fallback.append(tid)
        if main:
            main.sort(key=lambda x: x[0], reverse=True)
            return main[0][1]
        if fallback:
            return fallback[0]
        return ""
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse_v4] 查 session %s 主 trace 失败: %s", thread_id[:12], e)
        return ""


# ── trace 级元数据 / input ─────────────────────────────────

def get_trace_metadata(trace_id: str) -> dict:
    """取某 trace root observation 的 metadata（trace 级业务元数据）。失败返回 {}。"""
    try:
        from agent.trace.langfuse_client import get_client

        client = get_client()
        resp = client.api.observations.get_many(
            fields="core,basic,metadata", limit=10, filter=_root_obs_filter(trace_id),
        )
        for o in (resp.data or []):
            if not getattr(o, "is_root_observation", False):
                continue
            return _as_dict(getattr(o, "metadata", None)) or {}
        return {}
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse_v4] 取 trace %s metadata 失败: %s", trace_id[:12], e)
        return {}


def get_trace_input(trace_id: str) -> str:
    """取某 trace root observation 的 input（原始 JSON 字符串）。失败返回空串。"""
    try:
        from agent.trace.langfuse_client import get_client

        client = get_client()
        resp = client.api.observations.get_many(
            fields="core,basic,io", limit=10, filter=_root_obs_filter(trace_id),
        )
        for o in (resp.data or []):
            if not getattr(o, "is_root_observation", False):
                continue
            return getattr(o, "input", "") or ""
        return ""
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse_v4] 取 trace %s input 失败: %s", trace_id[:12], e)
        return ""


def get_trace_scores(trace_id: str) -> list[dict]:
    """取某 trace 上的全部 score（含子 observation 挂的，按 subject 归属解析）。"""
    rows: list[dict] = []
    try:
        from agent.trace.langfuse_client import get_client

        client = get_client()

        def _page(cursor):
            resp = client.api.scores_v3.get_many_v3(
                trace_id=trace_id, limit=100, cursor=cursor,
                fields="details,subject,annotation",
            )
            meta = getattr(resp, "meta", None)
            nxt = getattr(meta, "next_cursor", None) if meta is not None else None
            return list(resp.data or []), nxt

        for s in _iter_cursor(_page):
            if _subject_trace_id(s) != trace_id:
                # v3 的 trace_id 参数可能返回子 observation 归属，仅保留本 trace 的
                pass
            rows.append(
                {
                    "name": getattr(s, "name", "") or "",
                    "value": getattr(s, "value", None),
                    "comment": getattr(s, "comment", None) or "",
                    "timestamp": getattr(s, "timestamp", None),
                    "source": str(getattr(s, "source", "") or ""),
                    "trace_id": _subject_trace_id(s),
                }
            )
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse_v4] 取 trace %s scores 失败: %s", trace_id[:12], e)
    return rows


def session_root_traces(session_id: str) -> list[dict]:
    """该会话全部 root observation（filter sessionId + root）。

    一个 v4「会话」是多条扁平独立 trace（chat_agent 主 / nl2sql_agent 子 /
    skill 工具各一条），跨 trace 打分归属要逐个 trace 汇总。返回：
    [{trace_id, name, type, start_time}]。
    """
    try:
        from agent.trace.langfuse_client import get_client

        client = get_client()
        resp = client.api.observations.get_many(
            fields="core,basic",
            limit=200,
            filter=_session_root_obs_filter(session_id),
        )
        out: list[dict] = []
        for o in (resp.data or []):
            tid = getattr(o, "trace_id", "") or ""
            if not tid:
                continue
            out.append(
                {
                    "trace_id": tid,
                    "name": getattr(o, "name", "") or "",
                    "type": getattr(o, "type", "") or "",
                    "start_time": getattr(o, "start_time", None),
                }
            )
        return out
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse_v4] 列 session %s roots 失败: %s", session_id[:12], e)
        return []


def session_scores_map(session_id: str) -> dict[str, float]:
    """汇总该会话全部 trace 上的 score 为 {name: value}（collect_badcase 用）。

    v4 里五维分挂在 skill 工具执行 trace（subject.kind=trace 或 observation），
    主 chat_agent trace 通常无分——必须按 session 取全部 root trace_id 后
    逐个 get_many_v3(trace_id=...) 汇总，跨 root 归属统一。

    同名 score 可能多条（同 session 多次 run 各打一次分；user-feedback 点赞后
    改评/撤销各写一条）：v3/scores 实测按 timestamp **倒序**（最新在前）返回，
    不能靠 dict 覆盖（会取到最旧）——显式按 timestamp 取每个 name 最新一条。
    """
    out: dict[str, float] = {}
    _latest_ts: dict[str, object] = {}  # name -> 已记录的最新 timestamp
    try:
        from agent.trace.langfuse_client import get_client

        client = get_client()
        tids = {t["trace_id"] for t in session_root_traces(session_id)}

        def _scores_for(tid: str):
            def _page(cursor):
                resp = client.api.scores_v3.get_many_v3(
                    trace_id=tid, limit=100, cursor=cursor,
                    fields="details,subject,annotation",
                )
                meta = getattr(resp, "meta", None)
                nxt = getattr(meta, "next_cursor", None) if meta is not None else None
                return list(resp.data or []), nxt

            for s in _iter_cursor(_page):
                name = getattr(s, "name", "") or ""
                if not name:
                    continue
                ts = getattr(s, "timestamp", None)
                prev = _latest_ts.get(name)
                # timestamp 为 None 视为最旧（不覆盖已有最新）；已有值且不比新则跳过
                if prev is not None and ts is not None and ts < prev:
                    continue
                try:
                    out[name] = float(getattr(s, "value", 0) or 0)
                    _latest_ts[name] = ts
                except (TypeError, ValueError):
                    pass

        for tid in tids:
            _scores_for(tid)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse_v4] 汇总 session %s scores 失败: %s", session_id[:12], e)
    return out


def session_question(session_id: str) -> str:
    """从该会话任一 agent trace 的 input 提取用户问题（取第一个非系统 human）。

    v4 会话含多次 run：最新 chat_agent root 的 input 常是 auto-continue 系统通知
    （正文带问题，但不应作为提问原文），真实问题在更早 run 的 input 里。
    遍历 chat_agent/nl2sql_agent root 逐个解析 input，取第一个命中。
    """
    try:
        for t in session_root_traces(session_id):
            if t["type"] != "AGENT":
                continue
            q = extract_question_from_input(get_trace_input(t["trace_id"]))
            if q:
                return q
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse_v4] 提取 session %s 问题失败: %s", session_id[:12], e)
    return ""


# ── score 全局拉取（feedback_gate 用）───────────────────────

def list_scores(
    name: str,
    since: datetime | None = None,
    limit: int = 1000,
) -> list[dict]:
    """拉某 score name 的近期记录（v3 cursor 分页，单页上限 100）。

    替代 legacy `scores.get_many(...)`。返回字段与原脚本对齐：
    {trace_id, value, comment, timestamp, source}。timestamp 是 datetime。
    """
    rows: list[dict] = []
    try:
        from agent.trace.langfuse_client import get_client

        client = get_client()

        def _page(cursor):
            resp = client.api.scores_v3.get_many_v3(
                name=name, from_timestamp=since, limit=100, cursor=cursor,
                fields="details,subject,annotation",
            )
            meta = getattr(resp, "meta", None)
            nxt = getattr(meta, "next_cursor", None) if meta is not None else None
            return list(resp.data or []), nxt

        for s in _iter_cursor(_page):
            if len(rows) >= limit:
                break
            rows.append(
                {
                    "trace_id": _subject_trace_id(s),
                    "value": float(getattr(s, "value", 0) or 0),
                    "comment": (getattr(s, "comment", None) or "") or "",
                    "timestamp": getattr(s, "timestamp", None),
                    "source": str(getattr(s, "source", "") or ""),
                }
            )
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse_v4] 拉 score %s 失败: %s", name, e)
    return rows


# ── 用户会话主 trace 列表（collect_badcase 用）──────────────

def list_user_traces(
    since: datetime,
    limit: int = 100,
) -> list[dict]:
    """列近 since 起的用户 AGENT 主 trace（type=AGENT 且 root）。

    替代 legacy `trace.list(tags="nl2sql", ...)`。返回：
    [{trace_id, session_id, name, start_time, end_time}]。
    skill 根 observation（SPAN，也是 root）会被 type=AGENT 过滤掉，
    但 ChatDeepSeek 等 model 层 GENERATION root 也在，调用方按 name/session 归组。
    """
    out: list[dict] = []
    try:
        from agent.trace.langfuse_client import get_client

        client = get_client()
        flt = json.dumps(
            [
                {"type": "string", "column": "type", "operator": "=", "value": "AGENT"},
                {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True},
            ]
        )

        def _page(cursor):
            resp = client.api.observations.get_many(
                fields="core,basic,trace_context",
                limit=100,
                cursor=cursor,
                filter=flt,
                from_start_time=since,
            )
            meta = getattr(resp, "meta", None) or getattr(resp, "metadata", None)
            nxt = getattr(meta, "next_cursor", None) if meta is not None else None
            return list(resp.data or []), nxt

        for o in _iter_cursor(_page):
            if len(out) >= limit:
                break
            out.append(
                {
                    "trace_id": getattr(o, "trace_id", "") or "",
                    "session_id": getattr(o, "session_id", None) or "",
                    "name": getattr(o, "name", "") or "",
                    "start_time": getattr(o, "start_time", None),
                    "end_time": getattr(o, "end_time", None),
                }
            )
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse_v4] 列用户 traces 失败: %s", e)
    return out


# ── 兼容工具 ────────────────────────────────────────────────

def metadata_prompt_label(metadata: dict) -> str:
    """从 trace root metadata 提取 prompt.prompt_label（feedback_gate 分组键）。"""
    prompt = metadata.get("prompt")
    pd = _parse_str_dict(prompt)
    return str(pd.get("prompt_label", "") or "")


def extract_question_from_input(input_str: str) -> str:
    """从 root observation input（{messages:[...]} JSON）提用户问题正文。"""
    data = _as_dict(input_str)
    msgs = data.get("messages") or []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        if m.get("type") not in ("human", "user"):
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        text = str(content).strip()
        if text and not text.startswith("[系统"):
            return text[:500]
    return ""
