"""反馈待标注队列 API（P1/P2 优化③）。

路由（经 custom_app.py 合并进 langgraph API，端口 2026）：
    GET  /api/feedback/annotations?status=&limit=               队列列表
    GET  /api/feedback/annotations/bad-types                    错误类型枚举（前端下拉）
    GET  /api/feedback/annotations/{tid}/{mid}                  详情（question/sql 惰性补齐）
    POST /api/feedback/annotations/{tid}/{mid}/judge            {is_valid} → annotating/rejected
    POST /api/feedback/annotations/{tid}/{mid}/execute          {sql, db_name} → 执行预览
    POST /api/feedback/annotations/{tid}/{mid}/confirm          {gold_sql, bad_type, note} → BadCase

状态机（store.ANNOTATION_STATUSES）：
    queued → annotating（is_valid=1）→ validated（金标就绪，execute 后）→ badcase（confirm）
            ↘ rejected（is_valid=0，无效/误报/闲聊）
    validated → good（confirm-good：确认入 Good Set 正向样本，终态）

执行复用 dbmcp 四引擎（_RUNNER_REGISTRY / _load_runner_class / _apply_default_limit /
combine_multi_results），前置 classify_sql 只读硬校验——与运行时同一判据
（见 agent.middlewares.sql_approval）。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from agent.feedback.store import ANNOTATION_STATUSES, AnnotationRecord, get_store
from agent.eval.bad_types import BAD_TYPES, is_valid_bad_type
from api._common import json_response, parse_body

_logger = logging.getLogger(__name__)

store = get_store()

_PREVIEW_LIMIT = 100
_GOLD_RESULT_MAX = 8000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dataset(client, name: str, description: str = "") -> None:
    """幂等建 dataset（云/自托管首次建，已存在则忽略）。"""
    try:
        client.create_dataset(name=name, description=description)
    except Exception as e:  # noqa: BLE001
        _logger.debug("create_dataset(%s) 已存在或失败（忽略）: %s", name, e)


def _json_safe(v):
    """把 runner 返回（pandas/numpy/Decimal/datetime）转成 JSON 安全值。"""
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    if isinstance(v, bool) or isinstance(v, int) or isinstance(v, str) or v is None:
        return v
    try:
        import numpy as np

        if isinstance(v, np.generic):
            return v.item()
    except Exception:  # noqa: BLE001
        pass
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:  # noqa: BLE001
            pass
    return str(v)


async def _run_preview(db_name: str, sql: str, limit: int = _PREVIEW_LIMIT) -> dict:
    """复用 dbmcp 引擎执行人工 SQL，返回预览结果。写/DDL 直接拒绝。

    抛 ValueError（只读拦截/配置缺失）时调用方转 400；runner 执行异常
    原样上抛（调用方记 exec_error 返回）。
    """
    from agent.middlewares.sql_approval import classify_sql
    from mcp_server.db_mcp_server.db.config import McpSqlConfig
    from mcp_server.db_mcp_server.db.db_server import (
        _apply_default_limit,
        _build_tool_context,
        _load_runner_class,
    )
    from mcp_server.db_mcp_server.db.multi_sql import (
        combine_multi_results,
        split_sql_statements,
    )
    from mcp_server.db_mcp_server.db.sql_runner import RunSqlToolArgs

    verdict, detail = classify_sql(sql)
    if verdict == "write":
        raise ValueError(f"仅允许只读查询（检测到 {detail or '写/DDL'} 操作，已拒绝执行）")

    cfg = McpSqlConfig.from_env(db_name)
    runner = _load_runner_class(cfg.db_type)(**cfg.config)
    context = _build_tool_context()

    statements = split_sql_statements(sql)
    all_results: list = []
    for stmt in statements:
        stmt = _apply_default_limit(stmt, limit)
        df = await runner.run_sql(RunSqlToolArgs(sql=stmt), context)
        all_results.append((stmt, df))
    result = combine_multi_results(all_results)
    result["rows"] = [_json_safe(r) for r in result.get("rows", [])]
    result["columns"] = [str(c) for c in result.get("columns", [])]
    return result


def _fill_annotation_from_snapshot(ann) -> "AnnotationRecord":
    """从本地反馈快照补齐标注的 question/bad_sql（SQLite 读，无网络）。

    入队时 question/sql 为空串（异步快照补齐在后台填充 feedback 表，见
    api/message_feedback._schedule_snapshot_backfill），列表页标题依赖 question，
    这里从快照快速补齐并持久化，避免待判断列表显示「无问题摘要」。快照也为空 /
    读取失败则保持原样（详情端惰性补齐兜底）。
    """
    if ann.question and ann.bad_sql:
        return ann
    try:
        rec = store.get(ann.thread_id, ann.message_id)
    except Exception as e:  # noqa: BLE001
        _logger.debug("[annotation] 读反馈快照失败 %s: %s", ann.thread_id[:12], e)
        return ann
    if rec is None or (not rec.question and not rec.sql):
        return ann
    fields: dict = {}
    if not ann.question and rec.question:
        fields["question"] = rec.question[:2000]
    if not ann.bad_sql and rec.sql:
        fields["bad_sql"] = rec.sql[:8000]
    if not fields:
        return ann
    try:
        updated = store.update_annotation(ann.thread_id, ann.message_id, **fields)
        return updated or ann
    except Exception as e:  # noqa: BLE001
        _logger.debug("[annotation] 列表补齐标注失败 %s: %s", ann.thread_id[:12], e)
        return ann


async def _backfill_annotation(thread_id: str, message_id: str) -> dict | None:
    """惰性补齐标注的 question/bad_sql（优先本地反馈快照，再取线程 state）。

    返回补齐后的 to_mapping()；不存在返回 None。
    """
    ann = store.get_annotation(thread_id, message_id)
    if ann is None:
        return None
    dirty = False
    if not ann.question or not ann.bad_sql:
        rec = store.get(thread_id, message_id)
        if rec and (rec.question or rec.sql):
            if not ann.question and rec.question:
                ann.question = rec.question[:2000]
                dirty = True
            if not ann.bad_sql and rec.sql:
                ann.bad_sql = rec.sql[:8000]
                dirty = True
    if not ann.question or not ann.bad_sql:
        try:
            from api.message_feedback import _extract_question_sql

            q, s = await _extract_question_sql(thread_id)
            if not ann.question and q:
                ann.question = q[:2000]
                dirty = True
            if not ann.bad_sql and s:
                ann.bad_sql = s[:8000]
                dirty = True
        except Exception as e:  # noqa: BLE001
            _logger.debug("[annotation] 线程 state 惰性补齐失败: %s", e)
    if dirty:
        ann = store.update_annotation(
            thread_id, message_id, question=ann.question, bad_sql=ann.bad_sql
        ) or ann
    return ann.to_mapping()


async def list_annotations(request: Request):
    status = request.query_params.get("status", "") or None
    try:
        limit = int(request.query_params.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))
    if status and status not in ANNOTATION_STATUSES:
        return json_response({"error": f"status 必须是 {ANNOTATION_STATUSES} 之一"}, status=400)
    records = store.list_annotations(status=status, limit=limit)
    # 列表标题依赖 question：入队时为空，这里从本地快照快速补齐（无网络读取）
    records = [_fill_annotation_from_snapshot(r) for r in records]
    return json_response(
        {
            "count": len(records),
            "annotations": [r.to_mapping() for r in records],
        }
    )


async def get_annotation_detail(request: Request):
    thread_id = request.path_params["thread_id"]
    message_id = request.path_params["message_id"]
    data = await _backfill_annotation(thread_id, message_id)
    if data is None:
        return json_response({"error": "标注不存在"}, status=404)
    return json_response({"annotation": data})


async def judge_annotation(request: Request):
    """人工判断是否有效反馈：
    is_valid=true → annotating；is_valid=false → rejected；
    is_valid=true + direct_good=true（点赞正例）→ 直接入 Good Set（跳过修正/执行，见 _confirm_good_commit）。
    """
    thread_id = request.path_params["thread_id"]
    message_id = request.path_params["message_id"]
    data = await parse_body(request)
    is_valid = data.get("is_valid")
    if not isinstance(is_valid, bool):
        return json_response({"error": "is_valid 必须是布尔值"}, status=400)
    ann = store.get_annotation(thread_id, message_id)
    if ann is None:
        return json_response({"error": "标注不存在"}, status=404)
    if ann.status in ("badcase", "good", "rejected"):
        return json_response({"error": f"状态 {ann.status} 不可再判断"}, status=409)
    annotator = str(data.get("annotator", "") or "")[:64]

    direct_good = bool(data.get("direct_good", False))
    if direct_good:
        if not is_valid:
            return json_response({"error": "direct_good 需要 is_valid=true"}, status=400)
        if ann.rating != "positive":
            return json_response({"error": "直接入 Good Set 仅限点赞反馈"}, status=400)
        sql = ann.gold_sql or ann.bad_sql
        if not sql:
            return json_response(
                {"error": "缺少正确 SQL（模型未生成 SQL 快照），无法直接入 Good Set"}, status=400
            )
        updated = await _confirm_good_commit(ann, sql, ann.db_name or "", annotator)
        if updated is None:
            return json_response({"error": "状态更新失败"}, status=409)
        return json_response({"ok": True, "annotation": updated.to_mapping(), "good": True})

    new_status = "annotating" if is_valid else "rejected"
    updated = store.update_annotation(
        thread_id, message_id,
        status=new_status, is_valid=1 if is_valid else 0,
        annotator=annotator, annotated_at=_now_iso(),
    )
    if updated is None:
        return json_response({"error": "状态更新失败"}, status=409)
    return json_response({"ok": True, "annotation": updated.to_mapping()})


async def execute_annotation(request: Request):
    """执行人工 SQL（只读护栏 + dbmcp 引擎），返回预览；成功更新 bad_sql，失败记 exec_error。"""
    thread_id = request.path_params["thread_id"]
    message_id = request.path_params["message_id"]
    data = await parse_body(request)
    sql = str(data.get("sql", "") or "").strip()
    if not sql:
        return json_response({"error": "sql 不能为空"}, status=400)
    ann = store.get_annotation(thread_id, message_id)
    if ann is None:
        return json_response({"error": "标注不存在"}, status=404)
    db_name = str(data.get("db_name", "") or "") or ann.db_name or ""
    try:
        result = await _run_preview(db_name, sql)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] 执行失败 thread=%s: %s", thread_id[:12], e)
        store.update_annotation(thread_id, message_id, exec_error=str(e)[:2000])
        return json_response({"error": f"执行失败: {e}"}, status=400)
    store.update_annotation(
        thread_id, message_id,
        bad_sql=sql[:8000],
        exec_error="",
        db_name=db_name[:128] if db_name else ann.db_name,
    )
    # 状态推进：queued/annotating → validated（人工 SQL 已验证可执行，金标就绪）
    if ann.status in ("queued", "annotating"):
        store.update_annotation(thread_id, message_id, status="validated")
    return json_response({"ok": True, "result": result})


async def confirm_annotation(request: Request):
    """确认 → 生成 BadCase（三处写入）：
    ① Langfuse Dataset:badcase（带 gold_sql/bad_type/source=user-annotation）
    ② badcase_status.json（status=reviewed + bad_type + gold_sql，进回归集）
    ③ 本地标注状态 → badcase（终态）
    """
    thread_id = request.path_params["thread_id"]
    message_id = request.path_params["message_id"]
    data = await parse_body(request)
    gold_sql = str(data.get("gold_sql", "") or "").strip()
    bad_type = str(data.get("bad_type", "") or "").strip()
    if not gold_sql:
        return json_response({"error": "gold_sql 不能为空"}, status=400)
    if not bad_type:
        return json_response({"error": "bad_type 不能为空"}, status=400)
    if not is_valid_bad_type(bad_type):
        return json_response({"error": f"bad_type 非法（可选: {[k for k, _, _ in BAD_TYPES]}）"}, status=400)
    ann = store.get_annotation(thread_id, message_id)
    if ann is None:
        return json_response({"error": "标注不存在"}, status=404)
    if ann.status in ("badcase", "good", "rejected"):
        return json_response({"error": f"状态 {ann.status} 不可再确认，勿重复操作"}, status=409)
    # 点赞反馈不进 BadCase（折中方案）：有评论仍可入队供人工查看，但确认成
    # badcase 仅限差评。点赞只能被驳回/忽略（进不了 badcase，见 store.revoke）。
    if ann.rating == "positive":
        return json_response(
            {"error": "点赞反馈不能确认成 BadCase（仅差评可入 BadCase，可改为驳回）"},
            status=409,
        )

    db_name = str(data.get("db_name", "") or "") or ann.db_name or ""
    note = str(data.get("note", "") or "") or ann.note or ""
    annotator = str(data.get("annotator", "") or "") or ann.annotator or ""

    # 金标 SQL 必须能跑通（只读校验 + 执行取结果）
    try:
        gold_result = await _run_preview(db_name, gold_sql)
        gold_result_json = json.dumps(gold_result, ensure_ascii=False, default=str)[:_GOLD_RESULT_MAX]
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] 金标 SQL 执行失败 thread=%s: %s", thread_id[:12], e)
        return json_response({"error": f"金标 SQL 执行失败（请先修正）: {e}"}, status=400)

    from api.message_feedback import _find_trace_with_retry

    trace_id = _find_trace_with_retry(thread_id, message_id, attempts=2)
    question = ann.question or ""
    today = datetime.now(timezone.utc).date().isoformat()

    # ① Langfuse Dataset:badcase（旁路：禁用/失败不阻塞本地闭环）
    try:
        from agent.trace.langfuse_client import get_client, langfuse_enabled

        if langfuse_enabled():
            client = get_client()
            _ensure_dataset(client, "badcase", "NL2SQL 人工确认的查询错误（负面样本），供回归/评测")
            client.create_dataset_item(
                dataset_name="badcase",
                input={"question": question or "(未取到问题)", "session_id": thread_id},
                expected_output={"sql": gold_sql},
                metadata={
                    "trace_id": trace_id,
                    "reasons": ["user_feedback=0", "manual_annotation"],
                    "source": "user-annotation",
                    "bad_type": bad_type,
                    "db_name": db_name,
                    "collected_at": today,
                    "gold_sql": gold_sql,
                },
                source_trace_id=trace_id or None,
            )
            _logger.info("[annotation] BadCase 已写入 Dataset:badcase trace=%s type=%s",
                         trace_id[:12] if trace_id else "?", bad_type)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] Dataset:badcase 写入失败（跳过，不影响本地）: %s", e)

    # ② badcase_status.json（reviewed + bad_type + gold_sql，进回归集）
    try:
        from agent.eval.badcase_status import annotate

        annotate(trace_id or thread_id, bad_type, gold_sql=gold_sql, note=note,
                 question=question, db_name=db_name)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] badcase_status 标记失败: %s", e)

    # ③ 本地标注 → badcase（终态）
    updated = store.update_annotation(
        thread_id, message_id,
        status="badcase", is_valid=1,
        gold_sql=gold_sql, gold_result=gold_result_json, bad_type=bad_type,
        annotator=annotator, annotated_at=_now_iso(), badcase_at=_now_iso(),
    )
    return json_response({"ok": True, "annotation": updated.to_mapping()})


async def _confirm_good_commit(
    ann, sql: str, db_name: str = "", annotator: str = ""
):
    """Good Set 共享落库（confirm-good 端点 + judge direct_good 直达共用）：
    ① Langfuse Dataset:goodcase（旁路：禁用/失败不阻塞本地闭环）
    ② 本地标注状态 → good（终态；gold_sql 落正确 SQL 供展示）
    """
    from api.message_feedback import _find_trace_with_retry

    trace_id = _find_trace_with_retry(ann.thread_id, ann.message_id, attempts=2)
    question = ann.question or ""
    today = datetime.now(timezone.utc).date().isoformat()

    try:
        from agent.trace.langfuse_client import get_client, langfuse_enabled

        if langfuse_enabled():
            client = get_client()
            _ensure_dataset(client, "goodcase", "NL2SQL 人工确认的正确查询（正向样本），供回归/评测")
            client.create_dataset_item(
                dataset_name="goodcase",
                input={"question": question or "(未取到问题)", "session_id": ann.thread_id},
                expected_output={"sql": sql},
                metadata={
                    "trace_id": trace_id,
                    "source": "user-annotation",
                    "rating": ann.rating,
                    "feedback_type": ann.feedback_type,
                    "db_name": db_name,
                    "collected_at": today,
                    "good_sql": sql,
                },
                source_trace_id=trace_id or None,
            )
            _logger.info("[annotation] Good 样本已写入 Dataset:goodcase trace=%s",
                         trace_id[:12] if trace_id else "?")
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] Dataset:goodcase 写入失败（跳过，不影响本地）: %s", e)

    now = _now_iso()
    return store.update_annotation(
        ann.thread_id, ann.message_id,
        status="good", is_valid=1,
        gold_sql=sql,
        annotator=annotator, annotated_at=now, badcase_at=now,
    )


async def confirm_good_annotation(request: Request):
    """确认 → 入 Good Set（正向样本，与 BadCase 对称）：
    ① Langfuse Dataset:goodcase（source=user-annotation，sql=模型 SQL/已验证 SQL）
    ② 本地标注状态 → good（终态）

    点赞+评论等正例走这里；SQL 取请求值，缺省回退 gold_sql / bad_sql（模型 SQL 即
    正确 SQL，用户点赞即背书）。不需要金标重执行——页面 execute 预览已人工核对。
    """
    thread_id = request.path_params["thread_id"]
    message_id = request.path_params["message_id"]
    data = await parse_body(request)
    ann = store.get_annotation(thread_id, message_id)
    if ann is None:
        return json_response({"error": "标注不存在"}, status=404)
    if ann.status in ("badcase", "good", "rejected"):
        return json_response({"error": f"状态 {ann.status} 不可再确认，勿重复操作"}, status=409)
    sql = str(data.get("sql", "") or "").strip() or ann.gold_sql or ann.bad_sql
    if not sql:
        return json_response(
            {"error": "缺少正确 SQL（先用执行验证生成，或手动填写）"}, status=400
        )
    db_name = str(data.get("db_name", "") or "") or ann.db_name or ""
    annotator = str(data.get("annotator", "") or "") or ann.annotator or ""

    updated = await _confirm_good_commit(ann, sql, db_name, annotator)
    return json_response({"ok": True, "annotation": updated.to_mapping()})


async def list_bad_types(request: Request):
    return json_response(
        {"bad_types": [{"key": k, "label": label, "desc": desc} for k, label, desc in BAD_TYPES]}
    )


# ── Langfuse Dataset 直读（标注页 BadCase / Good Set 模块，与 Langfuse UI 保持一致）──

def _as_field(v):
    return str(v) if v is not None else ""


def _as_dict_field(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            d = json.loads(v)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def _dataset_item_to_row(it) -> dict:
    """Langfuse Dataset item → 统一展示形状（含来源 source）。

    input={question, session_id}、expected_output={sql}、metadata 带
    source（auto-collect / user-annotation）、db_name、trace_id、bad_type、
    rating、reasons、collected_at。字段可能为 dict 或 JSON 字符串，逐一兜底。
    """
    inp = _as_dict_field(getattr(it, "input", None))
    exp = _as_dict_field(getattr(it, "expected_output", None))
    meta = _as_dict_field(getattr(it, "metadata", None))
    reasons = meta.get("reasons")
    if not isinstance(reasons, list):
        reasons = []
    return {
        "item_id": _as_field(getattr(it, "id", "")),
        "question": _as_field(inp.get("question")),
        "session_id": _as_field(inp.get("session_id")),
        "sql": _as_field(exp.get("sql")),
        "source": _as_field(meta.get("source")),
        "db_name": _as_field(meta.get("db_name")),
        "trace_id": _as_field(meta.get("trace_id") or getattr(it, "source_trace_id", "")),
        "bad_type": _as_field(meta.get("bad_type")),
        "rating": _as_field(meta.get("rating")),
        "reasons": [str(r) for r in reasons],
        "created_at": _as_field(getattr(it, "created_at", "")),
        "collected_at": _as_field(meta.get("collected_at")),
    }


async def list_dataset_items(request: Request):
    """GET /api/feedback/datasets?name=badcase|goodcase&limit=N

    直读 Langfuse Dataset 条目（v4 api.dataset_items.list），返回与 Langfuse UI
    Dataset 完全一致的条目 + 来源。Lanfuse 关闭 / 读取失败 → 空列表，不阻塞页面。
    """
    name = request.query_params.get("name", "") or ""
    if name not in ("badcase", "goodcase"):
        return json_response(
            {"error": "name 必须是 badcase 或 goodcase"}, status=400
        )
    try:
        raw_limit = request.query_params.get("limit", "100") or "100"
        # v4 api.dataset_items.list limit 上限 100，超出直接 400
        limit = max(1, min(int(raw_limit), 100))
    except ValueError:
        limit = 100
    try:
        from agent.trace.langfuse_client import get_client, langfuse_enabled

        if not langfuse_enabled():
            return json_response({"dataset": name, "count": 0, "items": []})
        client = get_client()
        resp = client.api.dataset_items.list(dataset_name=name, limit=limit)
        items = [ _dataset_item_to_row(it) for it in (resp.data or []) ]
        return json_response({"dataset": name, "count": len(items), "items": items})
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] 读 Langfuse Dataset:%s 失败: %s", name, e)
        return json_response({"dataset": name, "count": 0, "items": [], "error": str(e)})


routes: list[BaseRoute] = [
    Route("/api/feedback/annotations", list_annotations, methods=["GET"]),
    Route("/api/feedback/annotations/bad-types", list_bad_types, methods=["GET"]),
    Route("/api/feedback/datasets", list_dataset_items, methods=["GET"]),
    Route("/api/feedback/annotations/{thread_id}/{message_id}",
          get_annotation_detail, methods=["GET"]),
    Route("/api/feedback/annotations/{thread_id}/{message_id}/judge",
          judge_annotation, methods=["POST"]),
    Route("/api/feedback/annotations/{thread_id}/{message_id}/execute",
          execute_annotation, methods=["POST"]),
    Route("/api/feedback/annotations/{thread_id}/{message_id}/confirm",
          confirm_annotation, methods=["POST"]),
    Route("/api/feedback/annotations/{thread_id}/{message_id}/confirm-good",
          confirm_good_annotation, methods=["POST"]),
]
