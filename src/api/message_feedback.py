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
import time

import httpx
from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from agent.feedback.store import (
    MAX_NOTE_BYTES,
    MAX_SNAPSHOT_SQL,
    VALID_RATINGS,
    VersionConflictError,
    get_store,
)
from api._common import json_response, parse_body

_logger = logging.getLogger(__name__)

# 一次批量删除请求最多为多少条写撤销哨兵。每条哨兵都要做一次带重试的 trace
# 定位（走代理读大 payload），所以按条数封顶，超出的由响应里的 scores_queued
# 与实际排队数之差如实体现，不假装写完。
_REVOKE_BATCH_LIMIT = 100

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
    """从主线程消息尽力提取 SQL（工具调用 args.sql 优先，多段以 '; ' 拼接）。

    覆盖 wren 语义层（``wrenai_<库>_run_sql``）与直连（``dbmcp_run_sql``）两条通道 ——
    两者的 SQL 都在 ``tool_calls[].args.sql``。**Cube 通道是结构性例外**：入参是
    cube/measures/dimensions，没有 sql 字段，故走 ``_extract_sql_with_cube`` 的复算兜底。
    """
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


async def _extract_sql_with_cube(messages: list) -> tuple[str, dict]:
    """``_extract_sql`` 之上补 Cube 通道兜底。返回 ``(sql, cube_spec)``。

    Cube 的 SQL 由 wren 引擎在服务端编译，模型的调用里根本没有 —— 只能按查询定义
    在进程内**复算**（见 agent/utils/wren_call_extract）。那条路要建 wren 引擎
    （冷启约 0.9s、热 60~370ms），所以丢到线程里跑，不占事件循环。

    **为什么连定义一起回传**：复算本来就同时产出规范化查询定义（``cube_spec``：
    measures/dimensions/filters…）。分开取会把引擎编译跑两遍；而定义正是入
    BadCase/Good Set 时要带的「聚合口径」——比看物理 SQL 更容易发现「同题两次
    聚合口径不一致」（监控与评测的核心信号）。

    fail-open：复算失败返回 ``("", {})``，行为与修复前一致。
    """
    sql = _extract_sql(messages)
    if sql:
        return sql, {}
    try:
        from agent.utils.wren_call_extract import cube_snapshot
    except Exception as e:  # noqa: BLE001  不该发生，但不能因 import 失败丢快照
        _logger.warning("[feedback] Cube 快照模块加载失败: %s", e)
        return "", {}
    try:
        snap = await asyncio.to_thread(cube_snapshot, messages)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[feedback] Cube 快照复算失败: %s", e)
        return "", {}
    return (snap.get("sql") or ""), (snap.get("spec") or {})


async def _extract_question_sql(thread_id: str) -> tuple[str, str, dict]:
    """读主线程+子线程 state，返回 (question, sql, cube_spec) 快照。失败/缺失返回空值。

    - question：最后一条「非系统」human 消息文本（对齐 sync 的 _extract_user_query 语义）。
    - sql：主线程消息的 tool_calls.args.sql 优先；若主线程无 SQL（如 start_async_task
      只委派不执行），则从 async_tasks 找到子线程 ID，读子线程 state 提取 SQL。
    - cube_spec：仅 Cube 通道非空（规范化聚合口径），与 sql 同一次复算产出。
    """
    base = _base_url()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as http:
            r = await http.get(f"{base}/threads/{thread_id}/state")
            if r.status_code != 200:
                return "", "", {}
            state = r.json()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[feedback] 读线程 state 失败，question/sql 留空: %s", e)
        return "", "", {}
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
    sql, spec = await _extract_sql_with_cube(messages)
    # 主线程无 SQL → 从子线程提取（主线程委派 start_async_task → nl2sql 子线程执行 SQL）
    if not sql:
        sql, spec = await _extract_sql_from_sub_threads(base, state)
    return question, sql, spec


async def _extract_sql_from_sub_threads(base: str, state: dict) -> tuple[str, dict]:
    """从主线程 async_tasks 找到所有子线程，读子线程 state 提取 SQL 与 Cube 定义。

    多子线程时 SQL 以 ``"; "`` 拼接（既有行为）；``cube_spec`` 取**第一个非空**——
    一个提问正常只对应一个查询子任务，多个时拼接定义没有可比对的语义（评测要的是
    「这一题用了什么聚合口径」，不是所有子任务的并集）。
    """
    async_tasks = (state.get("values") or {}).get("async_tasks") or {}
    if not async_tasks:
        return "", {}
    sub_ids = list(async_tasks.keys())
    # 只取最新一个子线程的 SQL（通常一个提问只对应一个子任务）
    sqls: list[str] = []
    spec: dict = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as http:
            for sub_id in sub_ids:
                try:
                    r = await http.get(f"{base}/threads/{sub_id}/state")
                    if r.status_code != 200:
                        continue
                    sub_state = r.json()
                    sub_msgs = (sub_state.get("values") or {}).get("messages") or []
                    sub_sqls, sub_spec = await _extract_sql_with_cube(sub_msgs)
                    if sub_sqls:
                        sqls.append(sub_sqls)
                    if sub_spec and not spec:
                        spec = sub_spec
                except Exception as e:  # noqa: BLE001
                    _logger.debug("[feedback] 读子线程 %s state 失败: %s", sub_id[:12], e)
                    continue
    except Exception as e:  # noqa: BLE001
        _logger.warning("[feedback] 读子线程 state 失败: %s", e)
    if not sqls:
        return "", {}
    return "; ".join(sqls), spec


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
        question, sql, spec = await _extract_question_sql(thread_id)
        if not question and not sql:
            return
        try:
            # spec 传 {} 而非 None：走到这里说明确实读过 state、确认过通道，空就是
            # 「这条不是 Cube 通道」，与「本次没算」（None → 不覆盖）是两回事。
            snapshot_ok = store.update_snapshot(thread_id, message_id, question, sql,
                                                cube_spec=spec or {})
            # 标注记录同步回填：入队时 question/sql 为空串（见 put_feedback），
            # 这里一并补齐，让待标注队列列表页标题有值（否则恒显示「无问题摘要」，
            # 直到详情端惰性补齐才持久化）。
            if snapshot_ok:
                ann = store.get_annotation(thread_id, message_id)
                if ann is not None:
                    fields = {}
                    if not ann.question and question:
                        fields["question"] = question[:2000]
                    if not ann.bad_sql and sql:
                        fields["bad_sql"] = sql[:MAX_SNAPSHOT_SQL]
                    if not ann.cube_spec and spec:
                        fields["cube_spec"] = spec
                    if fields:
                        store.update_annotation(thread_id, message_id, **fields)
                    # 点赞自动入 Good Set：快照刚落地，正是 SQL 可用的第一时刻。
                    # 门槛与写入全在 maybe_auto_good（含 fail-open），这里只负责把它
                    # 挪到线程里——内部是阻塞调用（trace 解析带 sleep + 同步 Langfuse
                    # SDK），在事件循环里跑会卡住别的请求。
                    ann2 = store.get_annotation(thread_id, message_id)
                    if ann2 is not None:
                        from api.feedback_annotation import maybe_auto_good

                        await asyncio.to_thread(
                            maybe_auto_good, ann2, sql, ann2.db_name
                        )
        except Exception as e:  # noqa: BLE001
            _logger.warning("[feedback] 快照补齐失败: %s", e)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 同步上下文（单测等）无事件循环，跳过
    loop.create_task(_backfill())


def _find_session_trace_id(thread_id: str, message_id: str = "") -> str:
    """定位用户反馈应挂载的 Langfuse trace id。

    M3 起用户反馈写分：反馈针对的是**具体某条回答**（message_id），必须落在
    产生它的那次 chat-turn trace 上——否则同一会话里对不同问题的反馈会全部
    归到最后一次查询的 trace（实测 01a03e3c 全落 7a03f9，Q1~Q3 trace 没分）。

    解析顺序：
      1. message→trace：升序扫 chat_agent 根 output，第一个含 message_id 的
         = 创建该消息的 trace（find_message_trace_id，精确归属）；
      2. 无 message_id / 未命中：回退「会话最新主 trace」（历史行为，跨进程/
         重启兜底）。
    失败/无 trace 返回空串（调用方跳过打分，不影响本地反馈落库）。
    """
    try:
        from agent.trace.langfuse_v4_reads import (
            find_message_trace_id,
            find_session_main_trace_id,
        )

        if message_id:
            tid = find_message_trace_id(thread_id, message_id)
            if tid:
                return tid
        return find_session_main_trace_id(thread_id)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[feedback] 查 Langfuse trace 失败: %s", e)
    return ""


def _find_trace_with_retry(thread_id: str, message_id: str, attempts: int = 3) -> str:
    """定位反馈应挂载的 Langfuse trace，带重试。

    2026-08-27 实测：反馈打分/撤销的 trace 定位走 v4 observations 接口（message
    路由取 io 字段，载荷大），经代理间歇性超时——单次失败会让「取消点赞」的撤销
    哨兵分被静默丢弃（Langfuse 永远停留在旧状态）。加重试（线性退避）+ 失败
    WARNING 日志，保证打分/撤销不被瞬时网络抖动吞掉。
    """
    for i in range(attempts):
        tid = _find_session_trace_id(thread_id, message_id)
        if tid:
            return tid
        if i < attempts - 1:
            time.sleep(0.4 * (i + 1))
    _logger.warning(
        "[feedback] %d 次重试仍未定位 Langfuse trace（thread=%s msg=%s），跳过写分/撤销",
        attempts, thread_id[:12], message_id[:12],
    )
    return ""


def _schedule_langfuse_score(thread_id: str, message_id: str, rating: str, note: str) -> None:
    """把用户反馈同步写进 Langfuse Score（user-feedback：好评 1 / 差评 0）。

    M3 §3.3：反馈以本地 store 为准（前端回显/导出），Langfuse 打分是旁路——
    后台线程尽力而为，先按 message_id 定位产生该消息的 trace（精确归属），
    create_score 入队异步刷新。失败仅告警，不影响反馈保存接口的毫秒级返回。

    M-feedback（2026-08-26）：调用方保证只对「首次写入或评分变化」打分。
    2026-08-27 补丁：**note 变化也打分**——用户点赞后再补的评论必须反映到
    Langfuse（否则 Scores 界面永远显示旧的默认文案）。v4 无 score 更新/删除
    API，多次打分靠「最新一条即当前状态」约定收口（feedback_gate._dedupe_scores
    按 trace 取最新，撤销用 value<0 哨兵，见 langfuse_v4_reads.USER_FEEDBACK_REVOKED）。
    """
    def _run() -> None:
        try:
            from agent.trace.langfuse_client import langfuse_enabled

            if not langfuse_enabled():
                return
            trace_id = _find_trace_with_retry(thread_id, message_id)
            if not trace_id:
                return  # 已由 _find_trace_with_retry 记 WARNING
            value = 1.0 if rating == "positive" else 0.0
            # comment 只用用户真实评语（note）；无评语留空——不伪造「有帮助/有问题」文案，
            # 否则 Scores 界面会把系统生成的文本当成用户评论。评分语义由 value 表达。
            comment = note
            # 优化①：反馈类型（query/chat）判定 + 本地回填。打分线程已定位 trace_id，
            # 直接复用（免二次 find_message_trace_id）。类型写入 score metadata 供
            # feedback_gate 按类型过滤、collect_badcase/看板按类型归组。
            from agent.feedback.feedback_type import classify_feedback_type

            rec = store.get(thread_id, message_id)
            ftype = classify_feedback_type(
                thread_id, message_id,
                sql_snapshot=(rec.sql if rec else ""),
                trace_id=trace_id,
            )
            if ftype:
                store.set_feedback_type(thread_id, message_id, ftype)
            from agent.trace.langfuse_client import create_score

            create_score(
                name="user-feedback",
                value=value,
                trace_id=trace_id,
                comment=comment,
                metadata={
                    "message_feedback": "local-store-backed",
                    "message_id": message_id,
                    "feedback_type": ftype,
                },
            )
            # 2026-09-18：不再另写一档 CATEGORICAL 分类分 feedback_type。
            # 那档分（query/chat/revoked）当初只为 Langfuse 仪表板做「query 数 ÷
            # chat 数」widget，而平台自己的 /feedback 页已算同一指标
            # （api/feedback_stats.py 的 signal_noise_ratio，口径读本地 SQLite）。
            # 全仓无任何代码读它——feedback_gate 读的是**本分自己的 metadata**
            # （上面 metadata={..., "feedback_type": ftype}，见 feedback_gate
            # ._filter_chat），collect_badcase/看板读本地列。故它在 trace 上纯是
            # 噪音（值域多出 revoked 一档，比 user-feedback 的 -1 哨兵更难解释）。
            # 类型信息仍由本分 metadata 承载，判定链完全不受影响。
            _logger.info(
                "[feedback] Langfuse user-feedback=%.1f trace=%s msg=%s type=%s",
                value, trace_id[:12], message_id[:12], ftype or "?",
            )
        except Exception as e:  # noqa: BLE001
            _logger.warning("[feedback] Langfuse user-feedback 打分失败: %s", e)

    try:
        threading.Thread(target=_run, daemon=True, name="langfuse-feedback").start()
    except Exception as e:  # noqa: BLE001
        _logger.debug("[feedback] 启动打分线程失败: %s", e)


def _revoke_one(thread_id: str, message_id: str) -> None:
    """在某条消息的 trace 上写一条「已撤销」哨兵 user-feedback score（同步、阻塞）。

    v4 events 表没有 score 删除 API（legacy DELETE 只删 legacy 表，实测删不到
    events 数据），代码侧无法真删残留分。改为软删除语义：撤销时写 value=-1
    哨兵分（USER_FEEDBACK_REVOKED），读取端约定「最新一条即当前状态」——
    feedback_gate 按 trace 取最新并把 value<0 视为无反馈，collect_badcase
    取最新且 -1≠0 不算差评。这样撤销后旧点赞分不会被计入好评率。

    自带 try/except：批量路径靠它做到「单条失败不拖垮整批」。
    """
    try:
        from agent.trace.langfuse_client import create_score, langfuse_enabled
        from agent.trace.langfuse_v4_reads import USER_FEEDBACK_REVOKED

        if not langfuse_enabled():
            return
        trace_id = _find_trace_with_retry(thread_id, message_id)
        if not trace_id:
            return  # 已由 _find_trace_with_retry 记 WARNING
        create_score(
            name="user-feedback",
            value=USER_FEEDBACK_REVOKED,
            trace_id=trace_id,
            comment="已撤销",
            metadata={
                "message_feedback": "revoked",
                "message_id": message_id,
            },
        )
        # 2026-09-18：配套去掉 feedback_type="revoked" 那档 CATEGORICAL 哨兵
        # （理由同上：无人读，且让撤销这件事在 trace 上各写两条分）。撤销的
        # 权威信号就是上面 value=-1 的 user-feedback 分——feedback_gate
        # 按 trace 取最新、value<0 视为无反馈，语义完整。
        _logger.info("[feedback] Langfuse user-feedback 已撤销 trace=%s", trace_id[:12])
    except Exception as e:  # noqa: BLE001
        _logger.warning("[feedback] Langfuse 撤销哨兵打分失败: %s", e)


def _schedule_langfuse_revoke(thread_id: str, message_id: str) -> None:
    """单条撤销哨兵：起一个后台线程去写（不阻塞请求）。"""
    def _run() -> None:
        _revoke_one(thread_id, message_id)

    try:
        threading.Thread(target=_run, daemon=True, name="langfuse-feedback-revoke").start()
    except Exception as e:  # noqa: BLE001
        _logger.debug("[feedback] 启动撤销打分线程失败: %s", e)


def _schedule_langfuse_revoke_many(
    pairs: list[tuple[str, str]], limit: int = _REVOKE_BATCH_LIMIT
) -> int:
    """批量撤销哨兵：**一个**后台线程顺序写，返回实际排队的条数。

    不复用 _schedule_langfuse_revoke 逐条起线程，是因为每条的 _find_trace_with_retry
    最多 3 轮、每轮走代理读很大的 v4 observations payload——几百条就是几百个线程
    加几百次代理请求。这里串行跑，单条失败只 WARNING，不影响其余。

    langfuse_enabled() 提到循环外：Langfuse 没接时一次检查就够，不必每条查一遍。
    返回的是**已排队**条数而非已写入条数——写是后台线程的活，此刻还没干。
    """
    items = [(t, m) for (t, m) in pairs if t and m][:limit]
    if not items:
        return 0

    def _run() -> None:
        try:
            from agent.trace.langfuse_client import langfuse_enabled

            if not langfuse_enabled():
                return
        except Exception as e:  # noqa: BLE001
            _logger.debug("[feedback] Langfuse 未接入，跳过批量撤销哨兵: %s", e)
            return
        for thread_id, message_id in items:
            _revoke_one(thread_id, message_id)

    try:
        threading.Thread(
            target=_run, daemon=True, name="langfuse-feedback-revoke-batch"
        ).start()
    except Exception as e:  # noqa: BLE001
        _logger.debug("[feedback] 启动批量撤销打分线程失败: %s", e)
        return 0
    return len(items)


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
        prev = store.get(thread_id, message_id)
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
    # 优化① 快路径：SQL 快照非空 → 直接标 query（零成本，慢路径由打分线程兜底）
    if rec.sql and rec.sql.strip():
        store.set_feedback_type(thread_id, message_id, "query")
    # 优化③ 入队：所有评分都进待标注队列（幂等）。纯点赞人工可「有效→直接入
    # Good Set」，差评走标注流程。question/sql 在快照补齐前可能为空，标注详情
    # 端会惰性补齐（见 api/feedback_annotation.py）。
    if rating:
        store.enqueue_annotation(
            thread_id, message_id,
            rating=rating, note=note,
            question=rec.question, sql=rec.sql,
            feedback_type=rec.feedback_type or "",
            db_name=str((rec.context or {}).get("db_name", "") or ""),
        )
    # M3：转发 Langfuse user-feedback score（后台旁路，不阻塞返回）。
    # M-feedback（2026-08-26）：仅「首次写入」或「评分变化」时打分。
    # 2026-08-27：补 note 变化也打分——点赞后再补的评论要反映到 Langfuse
    # Scores。v4 无 score 更新/删除 API，多次打分靠「最新一条即当前状态」
    # 约定收口（feedback_gate._dedupe_scores 已按 trace 取最新，见其 docstring）。
    rating_changed = prev is not None and prev.rating != rating
    note_changed = prev is not None and prev.note != note
    if is_first_write or rating_changed or note_changed:
        _schedule_langfuse_score(thread_id, message_id, rating, note)
    return json_response({"ok": True, "feedback": rec.to_mapping()})


def purge_feedback(
    thread_id: str,
    message_id: str,
    *,
    if_version: int | None = None,
    withdraw: bool = True,
    revoke: bool = True,
    score: bool = True,
) -> dict:
    """删掉一条用户反馈，并处理它的三处下游牵连（同步、阻塞）。返回各步结果。

    **顺序不可调换**：withdraw_auto_good 必须在 revoke_annotations_for_message 之前
    ——后者会把非终态标注改成 rejected，之后就读不到 status=good 了（withdraw 内部
    要读它）。这个顺序是这段代码最容易改坏的地方，所以整条级联只写在这里一处，
    delete_feedback 与标注页的「删除队列条目」共用。

    三个开关的存在理由（两条调用路径的需求不同）：
      * revoke=False —— 标注页那条路径马上要**真删**这一行标注，没有「回滚状态」
        可言；开着它反而有害：delete_annotation 万一失败，一条 validated 行会被
        静默降级成 rejected，用户凭空丢了「已验证」这个状态。
      * score 以「反馈行真的删掉了」为门（见下）——store.delete 对不存在的行返回
        False，而「已驳回」Tab 里大多是反馈早被 delete_feedback 删过、哨兵也早已
        写过一遍的行；无条件重发就是白跑一次带重试的 trace 定位。

    返回 {"feedback": 是否真删到反馈行, "withdrawn": 收回的自动入集条数, "score_queued": 是否已排队哨兵}。
    VersionConflictError 原样抛出，由调用方翻译成 409。
    """
    deleted = False
    try:
        deleted = store.delete(thread_id, message_id, if_version=if_version)
    except VersionConflictError:
        raise
    except Exception as e:  # noqa: BLE001
        _logger.warning("[feedback] 删除反馈行失败: %s", e)

    # 自动入集的 Good Set 要一并收回（2026-09-19）：那条 entry 的唯一依据就是这个
    # 点赞，点赞没了还留着，等于把用户已否定的答案钉成金标。人工确认过的 Good Set
    # 不动——人的判断依据不止这个 👍（见 withdraw_auto_good 的说明）。
    # 三处下游牵连只在「确实删掉了一条反馈行」时才谈得上：反馈行本就不存在时
    # （「已驳回」Tab 的常态），它们要么无从谈起，要么在第一次删除时就做过了。
    # 这也让 delete_feedback 重构后的行为与重构前逐字一致——行不存在即 404，
    # 不做任何级联（否则重复 DELETE 会开始做旧代码不做的事）。
    withdrawn = 0
    if deleted and withdraw:
        try:
            from api.feedback_annotation import withdraw_auto_good

            withdrawn = withdraw_auto_good(thread_id, message_id)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[feedback] 收回自动入集失败（数据集条目可能残留）: %s", e)

    # 优化③：反馈撤销后，该消息非终态标注回滚为 rejected（不打扰已确认工作）
    if deleted and revoke:
        try:
            store.revoke_annotations_for_message(thread_id, message_id)
        except Exception as e:  # noqa: BLE001
            _logger.debug("[feedback] 撤销标注回滚失败: %s", e)

    # M8：Langfuse 侧无 score 删除 API，撤销用哨兵分表达（软删除，见 _schedule_langfuse_revoke）
    score_queued = False
    if deleted and score:
        _schedule_langfuse_revoke(thread_id, message_id)
        score_queued = True
    return {"feedback": deleted, "withdrawn": withdrawn, "score_queued": score_queued}


async def delete_feedback(request: Request):
    thread_id = request.path_params["thread_id"]
    message_id = request.path_params["message_id"]
    data = await parse_body(request)
    try:
        if_version = _if_version(data)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    try:
        result = await asyncio.to_thread(
            purge_feedback, thread_id, message_id, if_version=if_version
        )
    except VersionConflictError as e:
        return json_response({"error": str(e)}, status=409)
    if not result["feedback"]:
        return json_response({"error": "反馈不存在"}, status=404)
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
