"""消息反馈 API 路由（P1-1，随 langgraph API 同进程/同端口提供）。

对标 deepseek-harness `dsh-message-feedback`，经 `custom_app.py`（LANGGRAPH_HTTP
钩子）合并进 langgraph API（端口 2026）。

路由：
    PUT    /api/threads/{thread_id}/messages/{message_id}/feedback   新建/更新（CAS）
    DELETE /api/threads/{thread_id}/messages/{message_id}/feedback   撤销
    GET    /api/threads/{thread_id}/feedback                         会话内列表（回显）
    GET    /api/feedback/export                                      全量导出（评测回流）

PUT body：
    {
      "rating": "positive" | "negative",   # 必填
      "note": str,                          # 可选，≤ 2KB（UTF-8 字节）
      "if_version": int,                    # 可选，CAS；不匹配 → 409
      "context": {"db_name": ...}           # 可选，仅首次写入时快照
    }
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

import httpx
from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from agent.feedback.store import (
    MAX_NOTE_BYTES,
    VALID_RATINGS,
    VersionConflictError,
    get_store,
)
from api._common import json_response, parse_body

_logger = logging.getLogger(__name__)

store = get_store()


def _base_url() -> str:
    return (os.environ.get("LANGGRAPH_API_URL") or "http://localhost:2026").rstrip("/")


def _msg_text(msg: dict) -> str:
    """取消息正文纯文本（兼容 str / block 列表两种 content 形态）。"""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                t = (blk.get("text") or "").strip()
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _extract_sql(messages: list) -> str:
    """从主线程消息尽力提取 SQL（工具调用 args.sql 优先，多段以 '; ' 拼接）。"""
    sqls: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or msg.get("type")
        if role not in ("ai", "assistant"):
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            args = tc.get("args") or {}
            if isinstance(args, dict) and args.get("sql"):
                sqls.append(str(args["sql"]))
    return "; ".join(sqls)


async def _extract_question_sql(thread_id: str) -> tuple[str, str]:
    """读主线程+子线程 state，返回 (question, sql) 快照。失败/缺失时返回空串。

    - question：最后一条「非系统」human 消息文本（对齐 sync 的 _extract_user_query 语义）。
    - sql：主线程消息的 tool_calls.args.sql 优先；若主线程无 SQL（如 start_async_task
      只委派不执行），则从 async_tasks 找到子线程 ID，读子线程 state 提取 SQL。
    """
    base = _base_url()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as http:
            r = await http.get(f"{base}/threads/{thread_id}/state")
            if r.status_code != 200:
                return "", ""
            state = r.json()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[feedback] 读线程 state 失败，question/sql 留空: %s", e)
        return "", ""
    messages = (state.get("values") or {}).get("messages") or []
    question = ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or msg.get("type")
        if role not in ("human", "user"):
            continue
        text = _msg_text(msg)
        if not text or text.startswith("[系统自动通知]") or text.startswith("[系统通知]"):
            continue
        question = text
        break
    # 主线程消息直接提取 SQL（主线程直接跑 SQL 的流程，如直连模式）
    sql = _extract_sql(messages)
    # 主线程无 SQL → 从子线程提取（主线程委派 start_async_task → nl2sql 子线程执行 SQL）
    if not sql:
        sql = await _extract_sql_from_sub_threads(base, state)
    return question, sql


async def _extract_sql_from_sub_threads(base: str, state: dict) -> str:
    """从主线程 async_tasks 找到所有子线程，读子线程 state 提取 SQL。"""
    async_tasks = (state.get("values") or {}).get("async_tasks") or {}
    if not async_tasks:
        return ""
    sub_ids = list(async_tasks.keys())
    # 只取最新一个子线程的 SQL（通常一个提问只对应一个子任务）
    sqls: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as http:
            for sub_id in sub_ids:
                try:
                    r = await http.get(f"{base}/threads/{sub_id}/state")
                    if r.status_code != 200:
                        continue
                    sub_state = r.json()
                    sub_msgs = (sub_state.get("values") or {}).get("messages") or []
                    sub_sqls = _extract_sql(sub_msgs)
                    if sub_sqls:
                        sqls.append(sub_sqls)
                except Exception as e:  # noqa: BLE001
                    _logger.debug("[feedback] 读子线程 %s state 失败: %s", sub_id[:12], e)
                    continue
    except Exception as e:  # noqa: BLE001
        _logger.warning("[feedback] 读子线程 state 失败: %s", e)
    if not sqls:
        return ""
    return "; ".join(sqls)


def _validate(data: dict) -> tuple[str, str] | None:
    """返回 (rating, note)；非法时抛 ValueError。"""
    rating = str(data.get("rating", ""))
    if rating not in VALID_RATINGS:
        raise ValueError(f"rating 必须是 {VALID_RATINGS} 之一")
    note = str(data.get("note", "") or "")
    if len(note.encode("utf-8")) > MAX_NOTE_BYTES:
        raise ValueError(f"note 超过 {MAX_NOTE_BYTES} 字节上限")
    return rating, note


def _if_version(data: dict) -> int | None:
    v = data.get("if_version")
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError("if_version 必须是整数")


def _schedule_snapshot_backfill(thread_id: str, message_id: str) -> None:
    """后台补齐 question/sql 快照（不阻塞反馈写入、不 bump version）。

    首次写入（新建记录）时才需要这两项（store.upsert 只在首次落快照，更新不覆盖）。
    读主线程 state 实测 1~9s（state 已膨胀到 100KB+），若在 put_feedback 里同步等待，
    会让「点赞 → 立即写批注」卡住（点赞期间批注按钮一直 disabled，保存批注再卡几秒）。
    """
    async def _backfill() -> None:
        question, sql = await _extract_question_sql(thread_id)
        if not question and not sql:
            return
        try:
            store.update_snapshot(thread_id, message_id, question, sql)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[feedback] 快照补齐失败: %s", e)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 同步上下文（单测等）无事件循环，跳过
    loop.create_task(_backfill())


def _find_session_trace_id(thread_id: str) -> str:
    """按 Langfuse sessionId=thread_id 查该会话的主 trace id（AGENT 根取最新）。

    M3 用户反馈写分目标：主 trace（chat_agent root，多次 run 取最新一次）——
    v4 平台 events_only 下 legacy `trace.list` 读不到事件新表，改走
    v2/observations（sessionId filter + root），见 langfuse_v4_reads。
    失败/无 trace 返回空串（调用方跳过打分，不影响本地反馈落库）。
    """
    try:
        from agent.trace.langfuse_v4_reads import find_session_main_trace_id

        return find_session_main_trace_id(thread_id)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[feedback] 查 Langfuse session trace 失败: %s", e)
    return ""


def _schedule_langfuse_score(thread_id: str, rating: str, note: str) -> None:
    """把用户反馈同步写进 Langfuse Score（user-feedback：好评 1 / 差评 0）。

    M3 §3.3：反馈以本地 store 为准（前端回显/导出），Langfuse 打分是旁路——
    后台线程尽力而为，先按 session_id 找主 trace，create_score 入队异步刷新。
    失败仅告警，不影响反馈保存接口的毫秒级返回。
    """
    def _run() -> None:
        try:
            from agent.trace.langfuse_client import langfuse_enabled

            if not langfuse_enabled():
                return
            trace_id = _find_session_trace_id(thread_id)
            if not trace_id:
                _logger.debug(
                    "[feedback] 未找到 thread %s 的 Langfuse trace，跳过 user-feedback 打分",
                    thread_id,
                )
                return
            value = 1.0 if rating == "positive" else 0.0
            comment = note or ("👍 有帮助" if rating == "positive" else "👎 有问题")
            from agent.trace.langfuse_client import create_score

            create_score(
                name="user-feedback",
                value=value,
                trace_id=trace_id,
                comment=comment,
                metadata={"message_feedback": "local-store-backed"},
            )
            _logger.info("[feedback] Langfuse user-feedback=%.1f trace=%s", value, trace_id[:12])
        except Exception as e:  # noqa: BLE001
            _logger.warning("[feedback] Langfuse user-feedback 打分失败: %s", e)

    try:
        threading.Thread(target=_run, daemon=True, name="langfuse-feedback").start()
    except Exception as e:  # noqa: BLE001
        _logger.debug("[feedback] 启动打分线程失败: %s", e)


def _schedule_langfuse_revoke(thread_id: str) -> None:
    """撤销反馈后，在 Langfuse 写一条「已撤销」哨兵 user-feedback score。

    v4 events 表没有 score 删除 API（legacy DELETE 只删 legacy 表，实测删不到
    events 数据），代码侧无法真删残留分。改为软删除语义：撤销时写 value=-1
    哨兵分（USER_FEEDBACK_REVOKED），读取端约定「最新一条即当前状态」——
    feedback_gate 按 trace 取最新并把 value<0 视为无反馈，collect_badcase
    取最新且 -1≠0 不算差评。这样撤销后旧点赞分不会被计入好评率。
    """
    def _run() -> None:
        try:
            from agent.trace.langfuse_client import create_score, langfuse_enabled
            from agent.trace.langfuse_v4_reads import USER_FEEDBACK_REVOKED

            if not langfuse_enabled():
                return
            trace_id = _find_session_trace_id(thread_id)
            if not trace_id:
                _logger.debug(
                    "[feedback] 未找到 thread %s 的 Langfuse trace，跳过撤销哨兵", thread_id,
                )
                return
            create_score(
                name="user-feedback",
                value=USER_FEEDBACK_REVOKED,
                trace_id=trace_id,
                comment="已撤销",
                metadata={"message_feedback": "revoked"},
            )
            _logger.info("[feedback] Langfuse user-feedback 已撤销 trace=%s", trace_id[:12])
        except Exception as e:  # noqa: BLE001
            _logger.warning("[feedback] Langfuse 撤销哨兵打分失败: %s", e)

    try:
        threading.Thread(target=_run, daemon=True, name="langfuse-feedback-revoke").start()
    except Exception as e:  # noqa: BLE001
        _logger.debug("[feedback] 启动撤销打分线程失败: %s", e)


async def put_feedback(request: Request):
    thread_id = request.path_params["thread_id"]
    message_id = request.path_params["message_id"]
    data = await parse_body(request)
    try:
        rating, note = _validate(data)
        if_version = _if_version(data)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    # question/sql 只在首次写入时后台补齐（见 _schedule_snapshot_backfill 说明）。
    # 这里先以空串落库，保证反馈写入 <几十毫秒 返回，前端批注交互不再被卡住。
    is_first_write = if_version is None
    try:
        rec = store.upsert(
            thread_id,
            message_id,
            rating,
            note=note,
            context=dict(data.get("context", {}) or {}),
            question="",
            sql="",
            if_version=if_version,
        )
    except VersionConflictError as e:
        return json_response({"error": str(e)}, status=409)
    except Exception as e:  # noqa: BLE001
        _logger.exception("[feedback] 保存失败")
        return json_response({"error": f"保存失败: {e}"}, status=500)
    if is_first_write:
        _schedule_snapshot_backfill(thread_id, message_id)
    # M3：转发 Langfuse user-feedback score（后台旁路，不阻塞返回）
    _schedule_langfuse_score(thread_id, rating, note)
    return json_response({"ok": True, "feedback": rec.to_mapping()})


async def delete_feedback(request: Request):
    thread_id = request.path_params["thread_id"]
    message_id = request.path_params["message_id"]
    data = await parse_body(request)
    try:
        if_version = _if_version(data)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    try:
        ok = store.delete(thread_id, message_id, if_version=if_version)
    except VersionConflictError as e:
        return json_response({"error": str(e)}, status=409)
    if not ok:
        return json_response({"error": "反馈不存在"}, status=404)
    # M8：Langfuse 侧无 score 删除 API，撤销用哨兵分表达（软删除，见 _schedule_langfuse_revoke）
    _schedule_langfuse_revoke(thread_id)
    return json_response({"ok": True})


async def list_thread_feedback(request: Request):
    thread_id = request.path_params["thread_id"]
    records = store.list_thread(thread_id)
    return json_response({"feedback": [r.to_mapping() for r in records]})


async def export_feedback(request: Request):
    """全量导出（bad case 评测集回流用；含 👍 正例）。"""
    records = store.export_all()
    return json_response(
        {
            "count": len(records),
            "feedback": [r.to_mapping() for r in records],
        }
    )


# ── 路由表（custom_app.py 聚合）──────────────────────────
routes: list[BaseRoute] = [
    Route(
        "/api/threads/{thread_id}/messages/{message_id}/feedback",
        put_feedback,
        methods=["PUT"],
    ),
    Route(
        "/api/threads/{thread_id}/messages/{message_id}/feedback",
        delete_feedback,
        methods=["DELETE"],
    ),
    Route("/api/threads/{thread_id}/feedback", list_thread_feedback, methods=["GET"]),
    Route("/api/feedback/export", export_feedback, methods=["GET"]),
]
