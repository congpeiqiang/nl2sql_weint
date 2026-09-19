"""反馈待标注队列 API（P1/P2 优化③）。

路由（经 custom_app.py 合并进 langgraph API，端口 2026）：
    GET  /api/feedback/annotations?status=&limit=               队列列表
    GET  /api/feedback/annotations/bad-types                    错误类型枚举（前端下拉）
    GET  /api/feedback/annotations/{tid}/{mid}                  详情（question/sql 惰性补齐）
    POST /api/feedback/annotations/{tid}/{mid}/judge            {is_valid} → annotating/rejected
    POST /api/feedback/annotations/{tid}/{mid}/execute          {sql, db_name} → 执行预览
    POST /api/feedback/annotations/{tid}/{mid}/preview-cube     {cube_spec, db_name} → 口径试算
    POST /api/feedback/annotations/{tid}/{mid}/confirm          {gold_sql, bad_type, note} → BadCase
    POST /api/feedback/annotations/{tid}/{mid}/confirm-good     {sql} → Good Set（正向样本）
    POST /api/feedback/annotations/{tid}/{mid}/revoke-good      撤回入集（删数据集条目 + 回 queued）
    GET  /api/feedback/dataset-stats                            数据集规模 + 金标缺口 + 队列深度

状态机（store.ANNOTATION_STATUSES）：
    queued → annotating（is_valid=1）→ validated（金标就绪，execute 后）→ badcase（confirm）
            ↘ rejected（is_valid=0，无效/误报/闲聊）
    validated → good（confirm-good：确认入 Good Set 正向样本，终态）

执行复用 dbmcp 四引擎（_RUNNER_REGISTRY / _load_runner_class / _apply_default_limit /
combine_multi_results），前置 classify_sql 只读硬校验——与运行时同一判据
（见 agent.middlewares.sql_approval）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from agent.feedback.store import (
    ANNOTATION_DELETABLE,
    ANNOTATION_STATUSES,
    MAX_SNAPSHOT_SQL,
    AnnotationRecord,
    get_store,
)
from agent.eval.bad_types import BAD_TYPES, is_valid_bad_type
from api._common import json_response, parse_body

_logger = logging.getLogger(__name__)

store = get_store()

_PREVIEW_LIMIT = 100
_GOLD_RESULT_MAX = 8000
# wren ``query_cube`` 真正接收的参数（实测签名，见 wren/mcp_server.py）——试算端点
# 只放这几个进去；``CUBE_ARG_KEYS`` 是「模型可能写什么」的宽容集合（含
# granularity/segments/order_by），拿来**显示**可以，拿来**编译**会静默丢参数。
_CUBE_SPEC_COMPILED = ("cube", "dimensions", "measures", "filters", "time_dimension")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dataset(client, name: str, description: str = "") -> None:
    """幂等建 dataset（云/自托管首次建，已存在则忽略）。"""
    try:
        client.create_dataset(name=name, description=description)
    except Exception as e:  # noqa: BLE001
        _logger.debug("create_dataset(%s) 已存在或失败（忽略）: %s", name, e)


# ── Dataset 条目字段契约（完整表见 docs/langfuse平台/Dataset字段契约.md）──────
# 权威顺序：**`expected_output.*` 是机器读的唯一权威**；`metadata.*` 里的同名内容是
# 给人看的镜像 + 兜底。读 metadata 只该发生在两处：① Langfuse UI 里人浏览；
# ② `expected_output` 缺席或没有对应槽位时（自动采集条 `expected_output` 恒为
# None —— 它的口径只存在于 `metadata.cube_spec`，故有 `exp.cube or meta.cube_spec`
# 这条兜底）。重复是刻意的，别顺手去重：
#   expected_output.sql   ≡ metadata.gold_sql（badcase）/ good_sql（goodcase）
#   expected_output.cube  ≡ metadata.cube_spec（后者是单行 JSON 串，给 UI 直接展示）
#                          + metadata.cube_spec_readable（人读文本，只放 metadata）
# 只在 metadata、**没有** expected_output 对应物的是「模型侧留档」三个键
# （physical_sql_original / cube_original / cube_original_readable）——它们回答
# 「模型当时是什么」，与 expected_output 的「人工认定该是什么」互为对照。
# ``_original`` 后缀 = 「模型原本那份」，与当前生效（人工可能改过）的那份成对。
# 为什么不合并：自动采集条 expected_output 恒 None + 存量条目（2026-09-19 前）只有
# metadata 侧 + 两者读者不同（Langfuse 一等字段 vs metadata 面板）。
# ──────────────────────────────────────────────────────────────────────────


def _cube_dataset_fields(spec: dict) -> dict:
    """Cube 通道 → 入数据集的附加字段；非 Cube 通道返回 ``{}``（不塞空壳）。

    - ``cube``：规范化查询定义（measures/dimensions/filters…，列表已排序、已剥
      limit/offset）→ 与 ``sql`` 并列进 ``expected_output``，让「本题该用什么聚合
      口径」可机器比对（Experiment 页的 Output 列同形，可直接并排 diff）。
    - ``cube_spec`` / ``cube_spec_readable``：同一份定义的单行 JSON 与人读文本 →
      进 metadata。JSON 给程序读，文本给 Langfuse UI 里人看（metadata 面板原样展示，
      不用再展开 JSON）；两者由同一份 ``spec`` 渲染，不会漂移。
    """
    spec = spec or {}
    if not spec:
        return {}
    try:
        from agent.utils.wren_call_extract import spec_readable_text

        return {
            "cube": spec,
            "cube_spec": json.dumps(spec, ensure_ascii=False),
            "cube_spec_readable": spec_readable_text(spec),
        }
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] Cube 定义序列化失败（跳过该字段）: %s", e)
        return {}


def _model_sql(ann) -> str:
    """模型**实际下发**的那条 SQL（与人工确认的金标区分开）。

    **不能读 ``ann.bad_sql``**：标注页的「执行校验」会把 ``bad_sql`` **覆盖成标注员
    改过的那条**（见 ``execute_annotation``），此后它已是金标的副本，再拿它当「模型
    跑了什么」就错了。``feedback`` 表的 ``sql`` 才是不可变的那一份 —— 它只由后台
    快照回填补齐（``message_feedback._schedule_snapshot_backfill``），标注流程从不写它。

    Cube 通道尤其依赖这条通路：它的 SQL 不存在于模型调用里（引擎服务端编译），只有
    复算快照这一份记录。反馈记录被撤销删除时退回 ``bad_sql``（有则用、无则空）。
    """
    try:
        rec = store.get(ann.thread_id, ann.message_id)
        if rec is not None and (rec.sql or "").strip():
            return rec.sql
    except Exception as e:  # noqa: BLE001
        _logger.debug("[annotation] 读反馈原始 SQL 失败，退回 bad_sql: %s", e)
    return ann.bad_sql or ""


def _physical_sql_field(sql: str) -> dict:
    """模型**实际下发**的那条 SQL → metadata 单键；空则返回 ``{}``（不塞空壳）。

    ``physical_sql_original`` 的 ``_original`` = 「模型原本那份」，与 ``gold_sql``
    （人工确认过、可能被标注者改过的**金标**）成对——两者不同才是信息：监控要能一眼
    看出「模型下发的是 X，人工认定该是 Y」。取值来源见 ``_model_sql``（Cube 通道为
    复算物理 SQL；物理 SQL 超 ``MAX_SNAPSHOT_SQL`` 时快照本身已降级成语义层 SQL，
    此处如实照搬、不再另算——入集时已拿不到那条；该线 2026-09-19 由 8000 提到
    64000，实测最大 10.9KB，故此路几乎不可达）。
    """
    sql = (sql or "").strip()
    return {"physical_sql_original": sql} if sql else {}


def _model_cube(ann) -> dict:
    """模型**原始**那份 Cube 查询定义（与人工试算改过的区分开）。

    ``_model_sql`` 的口径孪生兄弟，理由也同构：详情端点 ``_backfill_annotation`` 一
    开始就把 ``feedback`` 快照里的定义抄进 ``ann.cube_spec``，而「按新口径试算」端点
    会**就地改写** ``ann.cube_spec`` —— 此后 ``ann.cube_spec`` 里装的已是人工口径，
    再拿它当「模型当初用了什么口径」就错了。``feedback`` 表的 ``cube_spec`` 才是不可
    变的那一份（只由后台快照回填补齐，标注流程从不写它）。

    与 ``_model_sql`` 的唯一差别：**没有回退**。SQL 侧能退回 ``ann.bad_sql``（它在
    执行校验前还是模型那条），Cube 侧退回 ``ann.cube_spec`` 就等于把人工改过的当原始
    —— 那正是本函数要防的事。取不到就返回 ``{}``，让调用方整个键都不写、界面如实说
    「本次拿不到模型原口径」（宁可缺席，不可撒谎）。
    """
    try:
        rec = store.get(ann.thread_id, ann.message_id)
        if rec is not None and rec.cube_spec:
            return rec.cube_spec
    except Exception as e:  # noqa: BLE001
        _logger.debug("[annotation] 读反馈原始 Cube 定义失败: %s", e)
    return {}


def _original_cube_fields(ann) -> dict:
    """模型原始 Cube 定义 → metadata 两个键；拿不到返回 ``{}``（不塞空壳）。

    - ``cube_original`` / ``cube_original_readable``：与**当前生效**的
      ``cube_spec`` / ``cube_spec_readable`` 逐字同构（复用同一渲染，不会漂移），
      只是内容取自不可变快照里的**模型原口径**。
      两者不同 = 「人工改过口径」，相同 = 「模型原样」；一起缺席 = 「本次读不到原始
      那份」（自动采集条没有这两个键——它的 ``cube_spec`` 本身就取自同一份不可变
      快照，无人可改，加一份副本只是噪音）。
    """
    spec = _model_cube(ann)
    if not spec:
        return {}
    fields = _cube_dataset_fields(spec)
    if not fields:
        return {}
    return {
        "cube_original": fields["cube_spec"],
        "cube_original_readable": fields["cube_spec_readable"],
    }


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
    """从本地反馈快照补齐标注的 question/bad_sql/cube_spec（SQLite 读，无网络）。

    入队时 question/sql 为空串（异步快照补齐在后台填充 feedback 表，见
    api/message_feedback._schedule_snapshot_backfill），列表页标题依赖 question，
    这里从快照快速补齐并持久化，避免待判断列表显示「无问题摘要」。快照也为空 /
    读取失败则保持原样（详情端惰性补齐兜底）。
    """
    if ann.question and ann.bad_sql and ann.cube_spec:
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
        fields["bad_sql"] = rec.sql[:MAX_SNAPSHOT_SQL]
    if not ann.cube_spec and rec.cube_spec:
        fields["cube_spec"] = rec.cube_spec
    if not fields:
        return ann
    try:
        updated = store.update_annotation(ann.thread_id, ann.message_id, **fields)
        return updated or ann
    except Exception as e:  # noqa: BLE001
        _logger.debug("[annotation] 列表补齐标注失败 %s: %s", ann.thread_id[:12], e)
        return ann


async def _backfill_annotation(thread_id: str, message_id: str) -> dict | None:
    """惰性补齐标注的 question/bad_sql/cube_spec（优先本地反馈快照，再取线程 state）。

    返回补齐后的 to_mapping()；不存在返回 None。
    """
    ann = store.get_annotation(thread_id, message_id)
    if ann is None:
        return None
    dirty = False
    if not ann.question or not ann.bad_sql or not ann.cube_spec:
        rec = store.get(thread_id, message_id)
        if rec and (rec.question or rec.sql):
            if not ann.question and rec.question:
                ann.question = rec.question[:2000]
                dirty = True
            if not ann.bad_sql and rec.sql:
                ann.bad_sql = rec.sql[:MAX_SNAPSHOT_SQL]
                dirty = True
            if not ann.cube_spec and rec.cube_spec:
                ann.cube_spec = rec.cube_spec
                dirty = True
    if not ann.question or not ann.bad_sql or not ann.cube_spec:
        try:
            from api.message_feedback import _extract_question_sql

            q, s, spec = await _extract_question_sql(thread_id)
            if not ann.question and q:
                ann.question = q[:2000]
                dirty = True
            if not ann.bad_sql and s:
                ann.bad_sql = s[:MAX_SNAPSHOT_SQL]
                dirty = True
            if not ann.cube_spec and spec:
                ann.cube_spec = spec
                dirty = True
        except Exception as e:  # noqa: BLE001
            _logger.debug("[annotation] 线程 state 惰性补齐失败: %s", e)
    if dirty:
        ann = store.update_annotation(
            thread_id, message_id,
            question=ann.question, bad_sql=ann.bad_sql, cube_spec=ann.cube_spec,
        ) or ann
    data = ann.to_mapping()
    # 只读回显：**模型原始**那份口径（不可变快照里那份，见 _model_cube）。cube_spec
    # 一旦被「按新口径试算」改写，详情里就再也看不到它了，而标注页的「重置为模型原
    # 口径」与三态标记必须以此为基准才说得准（否则第二次打开看到的「原口径」其实是
    # 上一位标注员试算保存的那份）。不落库、不入数据集，纯回显。
    data["cube_original"] = _model_cube(ann)
    return data


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
            # 走到这里说明「这条回答本来就没有 SQL」——三类通道都已尽力：
            #   run_sql（wren 语义层 / dbmcp 直连）取 tool_calls[].args.sql；
            #   Cube 通道按查询定义在进程内复算成物理 SQL（见 message_feedback.
            #   _extract_sql_with_cube）。取不到就是真的没有：纯文本回答、澄清、
            #   闲聊，或复算 fail-open 落空。
            # 从前这里只说「模型未生成 SQL 快照」，把「本来没有」与「提不到」混成
            # 一句，运维分不清该查哪。现在给 reason 字段供前端分流，文案指向**现成
            # 通路**（进入标注 → 手工填 SQL → 确认入 Good Set）。
            return json_response(
                {
                    "error": (
                        "本条回答没有 SQL（如纯文本回答/澄清/闲聊），无法直接入 Good Set。"
                        "若它确实是数据查询，请走「进入标注」手工填写 SQL 后确认入 Good Set。"
                    ),
                    "reason": "no_sql",
                },
                status=400,
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
        bad_sql=sql[:MAX_SNAPSHOT_SQL],
        exec_error="",
        db_name=db_name[:128] if db_name else ann.db_name,
    )
    # 状态推进：queued/annotating → validated（人工 SQL 已验证可执行，金标就绪）
    if ann.status in ("queued", "annotating"):
        store.update_annotation(thread_id, message_id, status="validated")
    return json_response({"ok": True, "result": result})


def _parse_cube_spec(raw) -> dict:
    """请求体里的口径定义 → 规范化 spec；非法直接抛 ValueError（调用方转 400）。

    接受 **dict 或 JSON 字符串**：前端是一个文本框（标注员可改），提交上来的自然
    是字符串；但别的前端/脚本会直接给对象，两种都收。

    校验刻意分三层、报错指向具体怎么改：
    ① 结构（必须是对象）→ ② 键名（拼错 ``mesures`` 这类要当场拦，不能默默当成空
    定义丢给引擎，那会得到一句「没有度量」的误导报错）→ ③ 工具边界参数
    （``limit``/``offset`` 显式拒绝：wren 0.13 线上直传必错，线上由 sql_approval
    在工具边界剥离、改平台结果集截窗——试算要和线上同一条路，就不能让它们进来）。

    ②里对 ``granularity``/``segments``/``order_by`` 也**一律拒绝**：它们在
    ``CUBE_ARG_KEYS`` 里（那是「模型可能写什么」的宽容集合，报告照抄不评判），但
    wren ``query_cube`` 根本不收——真收这几个键会**静默不生效**，试算结果与标注员
    写的定义不符，比报错更坏（见 ``_CUBE_SPEC_COMPILED``）。
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ValueError("查询定义不能为空")
        try:
            raw = json.loads(text)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"查询定义不是合法 JSON（{e}）") from e
    if not isinstance(raw, dict):
        raise ValueError("查询定义必须是对象（{\"cube\": …, \"measures\": […]}）")

    from agent.utils.wren_call_extract import CUBE_ARG_KEYS, normalize_cube_spec

    unknown = [k for k in raw if k not in CUBE_ARG_KEYS]
    if unknown:
        raise ValueError(
            f"查询定义含未知字段 {unknown}；可用字段: {list(_CUBE_SPEC_COMPILED)}"
        )
    for k in ("limit", "offset"):
        if k in raw:
            raise ValueError(
                f"查询定义不要写 {k}：线上由平台在工具边界剥离、改结果集截窗，"
                "试算与线上必须同一条路"
            )
    ignored = [k for k in raw if k not in _CUBE_SPEC_COMPILED]
    if ignored:
        raise ValueError(
            f"查询定义含 wren 不支持的字段 {ignored}；Cube 查询只认 "
            f"{list(_CUBE_SPEC_COMPILED)}（时间粒度写在 time_dimension 里，"
            "形如 report_date:month）"
        )
    spec = normalize_cube_spec(raw)
    if not spec.get("cube"):
        raise ValueError("查询定义缺少 cube 名")
    if not spec.get("measures"):
        raise ValueError("查询定义缺少 measures（Cube 查询至少要有一个度量）")
    return spec


async def preview_cube_annotation(request: Request):
    """按**编辑后的** Cube 口径试算：定义 → 物理 SQL → 只读执行 → 结果预览。

    与 ``execute_annotation``（人工 SQL 的执行校验）对称，补上 Cube 通道缺的那一环：
    Cube 通道结构上没有 SQL，标注员改完口径只能靠肉眼判断对不对。这里拿平台的同一
    套复算（``wren_plan.plan_cube_sql_checked``，与报告「执行 SQL（物理）」节同源）
    编译出**真正会下发的那条 SQL** 并执行，等于给口径一个可验证的回路；编译失败时把
    引擎原话（``Unknown measure …`` 之类）返回，顺带就是一次拼写检查。

    副作用**只有**：把试算过的口径落库以便刷新后还在（``cube_spec``，不推进状态机、
    不写 ``bad_sql``/``gold_sql``）——试算通过与否不构成「金标就绪」。

    返回 ``{ok, spec, cube_spec_readable, physical_sql, cube_sql, dialect, result}``；
    失败一律 400 + 中文 ``error``（404 仅当标注不存在）。
    """
    thread_id = request.path_params["thread_id"]
    message_id = request.path_params["message_id"]
    data = await parse_body(request)
    ann = store.get_annotation(thread_id, message_id)
    if ann is None:
        return json_response({"error": "标注不存在"}, status=404)

    try:
        spec = _parse_cube_spec(data.get("cube_spec"))
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)

    db_name = str(data.get("db_name", "") or "") or ann.db_name or ""
    from agent.utils.wren_call_extract import resolve_wren_ctx_by_db, spec_readable_text

    project, conn = resolve_wren_ctx_by_db(db_name)
    if not project or not conn:
        return json_response(
            {"error": f"库 {db_name or '(未指定)'} 不是已建模库（无 wren 语义层项目），无法试算口径"},
            status=400,
        )

    import asyncio

    from agent.utils.wren_plan import CubePlanError, plan_cube_sql_checked

    try:
        # 引擎构建有冷启成本（~0.9s 每进程一次），不能占事件循环
        plan = await asyncio.to_thread(plan_cube_sql_checked, project, conn, spec)
    except CubePlanError as e:
        return json_response({"error": f"口径编译失败：{e}"}, status=400)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] 口径试算编译异常 thread=%s: %s", thread_id[:12], e)
        return json_response({"error": f"口径编译失败: {e}"}, status=400)

    physical_sql = str(plan.get("dialect_sql") or "")
    if not physical_sql:
        return json_response({"error": "口径编译失败：引擎没有产出物理 SQL"}, status=400)
    try:
        # physical_sql 尾部已带 LIMIT（镜像连接器上限），_apply_default_limit 见
        # 「LIMIT」即跳过 → 不会叠成双 LIMIT；执行的行数与线上工具一致。
        result = await _run_preview(db_name, physical_sql)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] 口径试算执行失败 thread=%s: %s", thread_id[:12], e)
        return json_response({"error": f"口径试算执行失败: {e}"}, status=400)

    try:
        # 只落口径，不动任何状态机字段：试算 ≠ 确认。
        # 传 **dict** 不是 JSON 串：记录里 cube_spec 是 dict、序列化在 update_annotation
        # 的绑定处做（那里对 cube_spec 无条件 json.dumps，传串会变成二次转义）。
        store.update_annotation(
            thread_id, message_id,
            cube_spec=spec,
            db_name=db_name[:128] if db_name else ann.db_name,
        )
    except Exception as e:  # noqa: BLE001
        _logger.debug("[annotation] 试算口径落库失败（不影响本次试算）: %s", e)

    return json_response({
        "ok": True,
        "spec": spec,
        "cube_spec_readable": spec_readable_text(spec),
        "physical_sql": physical_sql,
        "cube_sql": str(plan.get("cube_sql") or ""),
        "dialect": str(plan.get("dialect") or ""),
        "result": result,
    })


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
            _cube = _cube_dataset_fields(ann.cube_spec)
            client.create_dataset_item(
                dataset_name="badcase",
                input={"question": question or "(未取到问题)", "session_id": thread_id},
                expected_output={"sql": gold_sql, **({"cube": _cube["cube"]} if _cube else {})},
                metadata={
                    "trace_id": trace_id,
                    "reasons": ["user_feedback=0", "manual_annotation"],
                    "source": "user-annotation",
                    "bad_type": bad_type,
                    # 定位键：一条 trace 可承载同会话多条反馈，撤回/核对要靠它区分
                    "message_id": ann.message_id,
                    "db_name": db_name,
                    "collected_at": today,
                    "gold_sql": gold_sql,
                    # 用户在反馈里写的评论（原先只在本地/标注页可见，入集后
                    # Langfuse UI 里能看到「业务为什么说这条错了」）
                    "note": note,
                    **({"cube_spec": _cube["cube_spec"],
                        "cube_spec_readable": _cube["cube_spec_readable"]} if _cube else {}),
                    # 模型原本那份口径（上面那份可能已被「按新口径试算」改过，
                    # 两者不同才是信息——同 physical_sql_original 与 gold_sql 的关系）
                    **_original_cube_fields(ann),
                    # 模型原本下发的那条（gold_sql 是人改过的金标，两者不同才是信息）
                    **_physical_sql_field(_model_sql(ann)),
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
    if updated is None:
        # 条目在「读 ann → 执行金标 SQL → 写终态」这段时间里被删掉了——标注页的
        # 删除按钮给了这条路（中间隔着一次真实库往返，秒级窗口足够撞上）。
        # ① 的 Dataset:badcase 条目已经写出去了，本地行却没了。那条条目本身是真实
        # 且带金标的产物，保留即可（本地队列行只是簿记），badcase_status 的 reviewed
        # 标记与之一致，故这里**只**需要不崩栈、并把实情说清楚。
        _logger.warning("[annotation] 确认入 BadCase 时条目已被删除 thread=%s", thread_id[:12])
        return json_response(
            {
                "error": "该条目已被删除，无法确认入 BadCase"
                "（Langfuse 里的 badcase 条目已写出，如需清理请到数据集 UI）"
            },
            status=409,
        )
    return json_response({"ok": True, "annotation": updated.to_mapping()})


def _goodcase_item_kwargs(ann, sql: str, db_name: str, trace_id: str, source: str) -> dict:
    """构造一条 Dataset:goodcase 条目的写入参数（纯函数，无副作用、不触网）。

    人工路（source="user-annotation"）与自动路（source="auto-good"）共用，保证
    两条路写出的字段契约完全一致——只有 source 不同。
    """
    today = datetime.now(timezone.utc).date().isoformat()
    cube = _cube_dataset_fields(ann.cube_spec)
    return {
        "dataset_name": "goodcase",
        "input": {
            "question": (ann.question or "") or "(未取到问题)",
            "session_id": ann.thread_id,
        },
        "expected_output": {"sql": sql, **({"cube": cube["cube"]} if cube else {})},
        "metadata": {
            "trace_id": trace_id,
            "source": source,
            "rating": ann.rating,
            "feedback_type": ann.feedback_type,
            "db_name": db_name,
            "collected_at": today,
            "good_sql": sql,
            # 点赞时写的评论（如「查询正确」「口径不对」）——监控时最有信息量
            # 的一栏，原先完全没入集
            "note": ann.note,
            # 撤回的定位键（Dataset API 无可读的消息标识，只有 trace_id；一条 trace
            # 可能承载同会话多条反馈）。存量条目没有这个键，撤回端点有兜底规则。
            "message_id": ann.message_id,
            **({"cube_spec": cube["cube_spec"],
                "cube_spec_readable": cube["cube_spec_readable"]} if cube else {}),
            # 模型原本那份口径（上面那份可能已被「按新口径试算」改过）
            **_original_cube_fields(ann),
            # 模型原本下发的（good_sql 可能被标注页改过/手工填过）
            **_physical_sql_field(_model_sql(ann) or sql),
        },
        "source_trace_id": trace_id or None,
    }


def _write_goodcase_item(kwargs: dict) -> str:
    """写一条 Dataset:goodcase 条目，返回 item_id（拿不到回 ""）。失败向上抛。

    调用方负责判 langfuse_enabled()——这里只管写。
    """
    from agent.trace.langfuse_client import get_client

    client = get_client()
    _ensure_dataset(client, "goodcase", "NL2SQL 人工确认的正确查询（正向样本），供回归/评测")
    item = client.create_dataset_item(**kwargs)
    return str(getattr(item, "id", "") or "")


async def _confirm_good_commit(
    ann, sql: str, db_name: str = "", annotator: str = ""
):
    """Good Set 共享落库（confirm-good 端点 + judge direct_good 直达共用）：
    ① Langfuse Dataset:goodcase（旁路：禁用/失败不阻塞本地闭环）
    ② 本地标注状态 → good（终态；gold_sql 落正确 SQL 供展示）
    """
    from api.message_feedback import _find_trace_with_retry

    trace_id = _find_trace_with_retry(ann.thread_id, ann.message_id, attempts=2)

    try:
        from agent.trace.langfuse_client import langfuse_enabled

        if langfuse_enabled():
            _write_goodcase_item(
                _goodcase_item_kwargs(ann, sql, db_name, trace_id, "user-annotation")
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


def maybe_auto_good(ann, sql: str, db_name: str = "") -> bool:
    """点赞自动入 Good Set（A）。同步函数，内部全是阻塞调用，**须在线程里跑**。

    门槛（2026-09-19 用户拍板"宽松"）：只看「点赞 + SQL 非空 + 非闲聊 + 人工没动过」，
    **完全不看五维分**。理由是核心缺口在转化率：那个手工点击不引入任何新信息，而
    正样本集（离线评测 `--dataset goodcase` 的基准）因此长期建不起来。误入集的兜底
    是撤回端点（`revoke_good_annotation`）——所以 `source="auto-good"` 必须写上，
    自动条要一眼可辨、可批量复核。

    与人工路 `_confirm_good_commit` 的**关键差异**：写 Langfuse 失败 / 未启用时直接
    返回 False 且**不翻本地状态**。人工路失败也照翻本地（人已经决策过，本地状态表达
    的是"已确认"）；自动路若失败还翻状态，条目会从待判断队列消失却在数据集里没有，
    等于静默丢样本——宁可留在队列里等人点一下。

    调用方拿到的是刚回填过快照的 ann（见 message_feedback._backfill）。
    """
    if ann is None or not ann:
        return False
    # 幂等 + 门槛
    if ann.auto_good or ann.gold_sql:
        return False  # 已入集过（人工或自动），不再写第二条
    if ann.rating != "positive":
        return False
    sql = (sql or "").strip()
    if not sql:
        return False  # 纯文本回答/闲聊/复算落空 —— 也是 Cube 通道那条 400 的根因
    if ann.feedback_type == "chat":
        return False  # 显式判成闲聊的不入正样本集（'' = 未判定，按 query 处理）
    if ann.status != "queued":
        return False  # 人工已经动过（annotating/validated = 正在标，不抢）

    try:
        from agent.trace.langfuse_client import langfuse_enabled

        if not langfuse_enabled():
            return False
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] 自动入集判 langfuse 开关失败（跳过）: %s", e)
        return False

    from api.message_feedback import _find_trace_with_retry

    trace_id = _find_trace_with_retry(ann.thread_id, ann.message_id, attempts=2)
    try:
        _write_goodcase_item(
            _goodcase_item_kwargs(ann, sql, db_name or ann.db_name, trace_id, "auto-good")
        )
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] 自动入集写 Dataset:goodcase 失败（留在队列）: %s", e)
        return False

    now = _now_iso()
    updated = store.update_annotation(
        ann.thread_id, ann.message_id,
        status="good", is_valid=1, gold_sql=sql, auto_good=1,
        annotator="", annotated_at=now, badcase_at=now,
    )
    if updated is None:
        # 状态在写入期间被别处改了：人工抢在前面，或者标注页刚把这一条**删掉**
        # （2026-09-19 起四个本地 Tab 支持硬删，这条路才变得可达）。数据集条目已经
        # 写出去了，本地却没翻——留在那儿就是一条无人认领的金标，而 revoke-good
        # 又会因 status != good 拒绝收回，只能上 Langfuse UI 手删。
        # 就地收拾掉自己刚写的那条：按 source=auto-good 过滤，绝不碰人工并行确认的。
        _logger.warning("[annotation] 自动入集本地状态更新失败（数据集已写，就地回滚）thread=%s",
                        ann.thread_id[:12])
        try:
            n = _delete_goodcase_items(ann, trace_id, source="auto-good")
            _logger.info("[annotation] 自动入集回滚数据集条目 count=%d", n)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[annotation] 自动入集回滚数据集条目失败（需人工处理）: %s", e)
        return False
    _logger.info("[annotation] 点赞自动入集 trace=%s sql_len=%d",
                 trace_id[:12] if trace_id else "?", len(sql))
    return True


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


# ── 撤回入集（Good Set 专有）────────────────────────────────────

class _DeleteUnresolved(Exception):
    """能定位到候选条目但不敢确定删哪条（存量条目无 message_id 且多条）。"""


def _delete_goodcase_items(ann, trace_id: str, source: str = "") -> int:
    """删除该消息在 Dataset:goodcase 的条目，返回删除条数。定位不了则抛 _DeleteUnresolved。

    定位靠 `source_trace_id` 过滤（SDK 支持）+ 客户端侧按 `metadata.message_id` 精确匹配。
    2026-09-19 之前写入的条目没有 message_id：**整个 trace 只匹配到一条才删**，多条则
    拒绝——宁可让人去 Langfuse UI 手动删，也不误删同会话别的条目。

    `source` 给定时只收拾 metadata.source 等于该值的条目（自动入集的自清路径用，
    见 maybe_auto_good 的 updated is None 分支）：只删自己刚写的那条，绝不碰人工
    并行确认出来的条目。过滤后的条数才是「唯一匹配」判据的分母。
    """
    if not trace_id:
        raise _DeleteUnresolved("拿不到 trace_id，无法定位数据集条目")
    from agent.trace.langfuse_client import get_client

    client = get_client()
    resp = client.api.dataset_items.list(
        dataset_name="goodcase", source_trace_id=trace_id, limit=100
    )
    items = list(resp.data or [])
    if source:
        items = [
            it for it in items
            if _as_dict_field(getattr(it, "metadata", None)).get("source") == source
        ]
    if not items:
        return 0  # 数据集里本来就没有（可能已被手工删过）
    exact = [
        it for it in items
        if _as_dict_field(getattr(it, "metadata", None)).get("message_id") == ann.message_id
    ]
    if exact:
        targets = exact
    elif len(items) == 1:
        targets = items  # 存量条目，唯一匹配才敢删
    else:
        raise _DeleteUnresolved(
            f"该 trace 下有 {len(items)} 条 Good Case 且都没有 message_id，"
            "无法确定该删哪条"
        )
    deleted = 0
    for it in targets:
        item_id = str(getattr(it, "id", "") or "")
        if not item_id:
            continue
        client.api.dataset_items.delete(item_id)
        deleted += 1
    return deleted


def withdraw_auto_good(thread_id: str, message_id: str) -> int:
    """用户撤销点赞时收回自动入集的 Good Set 条目（同步；返回收回条数）。

    只收回 **auto_good=1** 的：人工确认过的 Good Set 是人的判断，依据不止那个 👍，
    不因用户撤回点赞而消失。Langfuse 侧删不成就不动本地状态（保持「本地 good ⇔
    数据集有条目」这个不变量），免得重入时写出第二条重复条目。
    """
    ann = store.get_annotation(thread_id, message_id)
    if ann is None or ann.status != "good" or not ann.auto_good:
        return 0
    from api.message_feedback import _find_trace_with_retry

    trace_id = _find_trace_with_retry(thread_id, message_id, attempts=2)
    try:
        from agent.trace.langfuse_client import langfuse_enabled

        if langfuse_enabled():
            _delete_goodcase_items(ann, trace_id)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] 撤销点赞收回自动入集失败（保留本地 good）: %s", e)
        return 0
    if store.reopen_annotation(thread_id, message_id) is None:
        return 0
    # 收回后条目本不该停在队列里：用户的点赞已经没了，没有正样本依据，直接驳回。
    store.update_annotation(thread_id, message_id, status="rejected",
                            note=ann.note or "点赞已撤销（自动入集已收回）")
    return 1


async def revoke_good_annotation(request: Request):
    """POST …/{tid}/{mid}/revoke-good —— 撤回入集（人工确认与自动入集都能撤）。

    Langfuse 侧**真删条目**（dataset_items.delete），而不是打标记：数据集是评测基准，
    留一条错标就是毒化，没有"软删除"的空间。本地条目回 queued，可重新判断。

    不变量：**本地 status=good ⇔ 数据集里有这条**。所以删不掉时（定位不了/调用失败）
    不退本地状态，而是回错误让人工处理；Langfuse 整个禁用时没有数据集可言，才只退本地。
    """
    thread_id = request.path_params["thread_id"]
    message_id = request.path_params["message_id"]
    ann = store.get_annotation(thread_id, message_id)
    if ann is None:
        return json_response({"error": "标注不存在"}, status=404)
    if ann.status != "good":
        return json_response(
            {"error": f"状态 {ann.status} 不在 Good Set，无需撤回"}, status=409
        )
    from api.message_feedback import _find_trace_with_retry

    trace_id = _find_trace_with_retry(thread_id, message_id, attempts=2)
    warning = ""
    deleted = 0
    try:
        from agent.trace.langfuse_client import langfuse_enabled

        if langfuse_enabled():
            # 删除是同步阻塞调用（含 list + N 次 delete），别占事件循环
            deleted = await asyncio.to_thread(_delete_goodcase_items, ann, trace_id)
            if not deleted:
                warning = "数据集里没找到对应条目（可能已被手工删除），本地已回退"
        else:
            warning = "Langfuse 已禁用，数据集条目未删除（仅本地回退）"
    except _DeleteUnresolved as e:
        return json_response(
            {"error": f"{e}；请到 Langfuse 数据集 UI 手动删除后重试", "reason": "ambiguous"},
            status=409,
        )
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] 撤回时删除数据集条目失败: %s", e)
        return json_response(
            {"error": f"删除数据集条目失败（{e}）；本地未回退，可重试或去 Langfuse UI 手动删",
             "reason": "delete_failed"},
            status=502,
        )
    updated = store.reopen_annotation(thread_id, message_id)
    if updated is None:
        return json_response({"error": "本地状态回退失败（可能已被他处改动）"}, status=409)
    _logger.info("[annotation] 撤回入集 trace=%s auto=%s",
                 trace_id[:12] if trace_id else "?", ann.auto_good)
    payload = {"ok": True, "annotation": updated.to_mapping(), "deleted_items": deleted}
    if warning:
        payload["warning"] = warning
    return json_response(payload)


# ── 队列条目的删除（标注页四个本地 Tab 用）──────────────────────────
# 与数据集 Tab 的「撤回入集」是两码事：那条路要维持「本地 good ⇔ 数据集有条目」
# 这个不变量、还要去删 Langfuse 条目；这条路只碰四个本地状态，它们**没有任何
# Langfuse 产物**——good/badcase 一律拒绝（判据在 store.ANNOTATION_DELETABLE）。

def _deletable_error(status) -> str:
    """可删状态校验，返回错误文案（空串 = 通过）。正白名单，不是黑名单。

    必须是白名单：list_annotations(status=None) 意味着「全部状态」，前端还有一个
    `""`（全部）Tab，黑名单配 `status or None` 会让一次「清空本 Tab」把 good 与
    badcase 一并删掉——那是两条会把 Langfuse 数据集条目和 badcase_status.json 条目
    留成孤儿的路径。
    """
    if not isinstance(status, str) or not status:
        return f"status 必填，且必须是 {list(ANNOTATION_DELETABLE)} 之一"
    if status not in ANNOTATION_DELETABLE:
        return (
            f"状态 {status} 不可删除：它有 Langfuse 数据集条目或 BadCase 标记，"
            "请用对应的撤回 / 移出操作"
        )
    return ""


async def delete_annotation(request: Request):
    """DELETE …/{thread_id}/{message_id} —— 删掉一条本地队列条目**连同它的用户反馈**。

    硬删（无软删除、无墓碑）：删完就是删完。用户要恢复只能重新提交反馈，那会重新
    入队，但不是同一条历史。

    body（可选）：`{"expected_status": "queued"}` —— 客户端看到的状态，用于 CAS。
    缺省则用本次读到的状态（仍然拦得住「客户端看的是旧状态」这种秒级陈旧）。
    """
    thread_id = request.path_params["thread_id"]
    message_id = request.path_params["message_id"]
    data = await parse_body(request)

    ann = store.get_annotation(thread_id, message_id)
    if ann is None:
        return json_response({"error": "标注不存在"}, status=404)
    err = _deletable_error(ann.status)
    if err:
        return json_response({"error": err}, status=409)
    expected = str(data.get("expected_status", "") or "") or ann.status
    err = _deletable_error(expected)
    if err:
        return json_response({"error": err}, status=400)

    # 先删用户反馈（含自动入集收回与撤销哨兵），最后才删标注行。
    # 顺序理由：反过来一旦失败会留下「聊天里一个活的 👍/👎 + 一条不存在的队列行」，
    # 那个赞从此既看不见也修不了；而先删反馈、后删行若失败，留下的是一条可见的、
    # 状态未被降级的行，重试即可。
    # revoke=False：这一行马上要被真删，没有「回滚状态」可言；开着它反而有害——
    # delete_annotation 万一失败，一条 validated 行会被静默降级成 rejected。
    from api.message_feedback import purge_feedback

    result = await asyncio.to_thread(
        purge_feedback, thread_id, message_id, revoke=False
    )
    ok = await asyncio.to_thread(
        store.delete_annotation, thread_id, message_id, expected
    )
    if not ok:
        # CAS 未命中：状态在「读 → 删」之间变了（多半是刚被确认入集）。
        # 反馈行已经删掉了，照实说，别让前端以为整件事没发生。
        return json_response(
            {
                "ok": False,
                "deleted": {"annotation": False, "feedback": result["feedback"]},
                "error": "条目状态已变化（可能刚被确认入集），未删除；用户反馈已移除",
            },
            status=409,
        )
    _logger.info(
        "[annotation] 删除队列条目 thread=%s feedback=%s",
        thread_id[:12], result["feedback"],
    )
    return json_response(
        {
            "ok": True,
            "deleted": {"annotation": True, "feedback": result["feedback"]},
        }
    )


# 一次批量清空最多处理多少条。SQLite 侧很便宜（每行几条语句），封顶是为了
# Langfuse 侧的撤销哨兵：每条都要一次带重试的 trace 定位。超出的部分不清，
# 由响应里的 capped/remaining 如实体现，不假装清空。
_BATCH_CAP = 500


async def clear_annotations(request: Request):
    """POST /api/feedback/annotations/clear —— 清空某个本地状态的**全部**条目。

    逐行按 `expected_status` 条件删。不是多余的谨慎：「确认入 BadCase」在
    「读 ann → 执行金标 SQL → 写终态」之间隔着一次真实库往返（秒级），几百行的
    循环足够长到撞上一个刚变成 badcase 的行，无条件删会把它的 Dataset:badcase
    条目和 badcase_status.json 条目留成孤儿。
    """
    data = await parse_body(request)
    status = data.get("status")
    err = _deletable_error(status)
    if err:
        return json_response({"error": err}, status=400)

    from api.message_feedback import _schedule_langfuse_revoke_many, purge_feedback

    rows = await asyncio.to_thread(store.list_annotations, status, _BATCH_CAP)
    capped = store.count_annotations().get(status, 0) > len(rows)

    deleted = 0
    feedback_deleted = 0
    skipped = 0
    score_pairs: list[tuple[str, str]] = []
    for ann in rows:
        try:
            result = await asyncio.to_thread(
                purge_feedback, ann.thread_id, ann.message_id, revoke=False
            )
        except Exception as e:  # noqa: BLE001
            _logger.warning("[annotation] 批量清空：删反馈失败 %s: %s", ann.thread_id[:12], e)
            result = {"feedback": False}
        if result["feedback"]:
            feedback_deleted += 1
            # 只有真删掉反馈行的那几条才需要哨兵——「已驳回」Tab 里大多是反馈早就
            # 被删过、哨兵也早写过一遍的行，重发就是白跑一次带重试的 trace 定位。
            score_pairs.append((ann.thread_id, ann.message_id))
        try:
            ok = await asyncio.to_thread(
                store.delete_annotation, ann.thread_id, ann.message_id, status
            )
        except Exception as e:  # noqa: BLE001
            _logger.warning("[annotation] 批量清空：删条目失败 %s: %s", ann.thread_id[:12], e)
            continue
        if ok:
            deleted += 1
        else:
            skipped += 1  # CAS 未命中：循环期间它变成了终态
    scores_queued = _schedule_langfuse_revoke_many(score_pairs)
    remaining = store.count_annotations().get(status, 0)
    _logger.info(
        "[annotation] 批量清空 status=%s deleted=%d skipped=%d capped=%s",
        status, deleted, skipped, capped,
    )
    return json_response(
        {
            "ok": True,
            "status": status,
            "deleted": deleted,
            "feedback_deleted": feedback_deleted,
            "skipped": skipped,
            "remaining": remaining,
            "scores_queued": scores_queued,
            "capped": capped,
        }
    )


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

    input={question, session_id}、expected_output={sql[, cube]}、metadata 带
    source（auto-collect / user-annotation）、db_name、trace_id、bad_type、
    rating、reasons、collected_at、note（用户评论）、cube_spec（Cube 定义）、
    physical_sql_original（模型实际下发的 SQL）、cube_spec_readable（Cube 定义的人读
    文本）。字段可能为 dict 或 JSON 字符串，逐一兜底。

    **读取取值遵循字段契约的权威顺序**（见文件上方注释块 / docs 同名文档）：
    `sql` 只认 `expected_output.sql`；`cube` 认 `expected_output.cube`，缺席才退
    `metadata.cube_spec`（自动采集条与 2026-09-19 前的存量条目）；只存在于 metadata
    的模型侧留档（physical_sql_original）如实读。**不读 `metadata.gold_sql`/
    `good_sql`**——它们与 `expected_output.sql` 同值，是纯人读镜像。
    """
    inp = _as_dict_field(getattr(it, "input", None))
    exp = _as_dict_field(getattr(it, "expected_output", None))
    meta = _as_dict_field(getattr(it, "metadata", None))
    reasons = meta.get("reasons")
    if not isinstance(reasons, list):
        reasons = []
    # cube：优先取 expected_output.cube（结构化，评测用），退回 metadata.cube_spec
    # （JSON 串，UI 用）。老条目两者皆无 → 空串，标注页按「非 Cube 条」渲染。
    cube = exp.get("cube") or meta.get("cube_spec") or ""
    if isinstance(cube, dict):
        try:
            cube = json.dumps(cube, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            cube = ""
    return {
        "item_id": _as_field(getattr(it, "id", "")),
        "question": _as_field(inp.get("question")),
        "session_id": _as_field(inp.get("session_id")),
        "message_id": _as_field(meta.get("message_id")),
        "sql": _as_field(exp.get("sql")),
        # 该条有没有权威金标（expected_output.sql 非空）。判据只认 expected_output：
        # 自动采集条 expected_output 恒为 None，此前被 _as_field 压成 "" 与「金标为空串」
        # 混为一谈，于是「待补金标」这个欠债在页面上完全不可见（见 dataset_stats）。
        "has_gold": bool(str(exp.get("sql") or "").strip()),
        "cube_spec": _as_field(cube),
        # 模型实际下发的 SQL：自动采集条没有金标（expected_output 为空），这条是页面上
        # 唯一能看到「模型当时到底跑了什么」的字段；人工条则与上方金标对照。
        "physical_sql_original": _as_field(meta.get("physical_sql_original")),
        "note": _as_field(meta.get("note")),
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


# ── 数据集规模与金标缺口（B）────────────────────────────────────

_STATS_PAGE_CAP = 20           # 20 页 × 100 = 2000 条，与 run_experiment 的分页上限一致
_STATS_TTL_SEC = 60.0          # 数据集统计的进程内缓存（前端会反复刷新）
_stats_cache: dict = {"ts": 0.0, "data": None}
_stats_lock = threading.Lock()


def _iter_dataset_items(dataset: str, max_pages: int = _STATS_PAGE_CAP) -> list:
    """分页拉全量 dataset items（每页 100）。list_dataset_items 只取第 1 页，
    统计必须全量，否则 >100 条的数据集会算出错误的缺口。"""
    from agent.trace.langfuse_client import get_client

    client = get_client()
    out: list = []
    for page in range(1, max_pages + 1):
        resp = client.api.dataset_items.list(dataset_name=dataset, page=page, limit=100)
        items = list(resp.data or [])
        out.extend(items)
        if len(items) < 100:
            break  # 末页
    return out


def _summarize_dataset(items: list) -> dict:
    """按「有没有权威金标」与来源归组。金标判据只认 expected_output.sql。"""
    by_source: dict[str, int] = {}
    with_gold = 0
    for it in items:
        exp = _as_dict_field(getattr(it, "expected_output", None))
        if str(exp.get("sql") or "").strip():
            with_gold += 1
        src = str(_as_dict_field(getattr(it, "metadata", None)).get("source") or "") or "(未标来源)"
        by_source[src] = by_source.get(src, 0) + 1
    return {
        "total": len(items),
        "with_gold": with_gold,
        "without_gold": len(items) - with_gold,
        "by_source": by_source,
    }


def _dataset_stats_sync() -> dict:
    """两个数据集的规模统计（阻塞 IO，调用方须放进线程）。"""
    return {
        "badcase": _summarize_dataset(_iter_dataset_items("badcase")),
        "goodcase": _summarize_dataset(_iter_dataset_items("goodcase")),
    }


def _dataset_stats_cached() -> dict:
    """带 TTL 的缓存读取。缓存只在成功时写入，失败不留脏缓存。"""
    import time

    now = time.monotonic()
    with _stats_lock:
        if _stats_cache["data"] is not None and now - _stats_cache["ts"] < _STATS_TTL_SEC:
            return _stats_cache["data"]
    data = _dataset_stats_sync()
    with _stats_lock:
        _stats_cache["ts"] = time.monotonic()
        _stats_cache["data"] = data
    return data


async def dataset_stats(request: Request):
    """GET /api/feedback/dataset-stats —— 数据集规模 + **金标缺口** + 本地队列深度。

    为什么需要它：`without_gold`（badcase 里没有权威金标的条目数）此前**完全不可见**。
    这些条目只能回答"还跑不跑得通"，回答不了"这次答对了没"（字段契约：expected_output
    是机器读的唯一权威）——缺口不显示，就没人去补，坏例集永远停在"半成品"。

    数据源分两处，各有理由：
      · 数据集统计 → Langfuse 分页全量（权威，与页面看到的条目一致），60s 进程内缓存；
      · 队列深度 → 本地 SQLite（瞬时、零网络，且天然是最新的，不跟着缓存走）。
    """
    payload: dict = {"queue": store.count_annotations()}
    try:
        from agent.trace.langfuse_client import langfuse_enabled

        if not langfuse_enabled():
            payload.update({
                "badcase": _summarize_dataset([]),
                "goodcase": _summarize_dataset([]),
                "disabled": True,
            })
            return json_response(payload)
        payload.update(await asyncio.to_thread(_dataset_stats_cached))
        return json_response(payload)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[annotation] 读数据集统计失败: %s", e)
        payload.update({
            "badcase": _summarize_dataset([]),
            "goodcase": _summarize_dataset([]),
            "error": str(e),
        })
        return json_response(payload)


routes: list[BaseRoute] = [
    Route("/api/feedback/annotations", list_annotations, methods=["GET"]),
    Route("/api/feedback/annotations/bad-types", list_bad_types, methods=["GET"]),
    Route("/api/feedback/datasets", list_dataset_items, methods=["GET"]),
    Route("/api/feedback/dataset-stats", dataset_stats, methods=["GET"]),
    # 清空某个本地状态的全部条目（/{tid}/{mid} 是同路径异方法，路径段数不同不冲突）
    Route("/api/feedback/annotations/clear", clear_annotations, methods=["POST"]),
    Route("/api/feedback/annotations/{thread_id}/{message_id}",
          get_annotation_detail, methods=["GET"]),
    # 删除一条队列条目（连同用户反馈）。同路径异方法，本仓既有写法（message_feedback
    # 的 /feedback 就是 PUT+DELETE 两条 Route）
    Route("/api/feedback/annotations/{thread_id}/{message_id}",
          delete_annotation, methods=["DELETE"]),
    Route("/api/feedback/annotations/{thread_id}/{message_id}/judge",
          judge_annotation, methods=["POST"]),
    Route("/api/feedback/annotations/{thread_id}/{message_id}/execute",
          execute_annotation, methods=["POST"]),
    Route("/api/feedback/annotations/{thread_id}/{message_id}/preview-cube",
          preview_cube_annotation, methods=["POST"]),
    Route("/api/feedback/annotations/{thread_id}/{message_id}/confirm",
          confirm_annotation, methods=["POST"]),
    Route("/api/feedback/annotations/{thread_id}/{message_id}/confirm-good",
          confirm_good_annotation, methods=["POST"]),
    Route("/api/feedback/annotations/{thread_id}/{message_id}/revoke-good",
          revoke_good_annotation, methods=["POST"]),
]
