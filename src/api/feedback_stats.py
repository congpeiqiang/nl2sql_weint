"""反馈看板指标 API（P0 优化②）。

    GET /api/feedback/stats?days=7

返回近 N 天反馈聚合：总数/正负比例/好评率 + 按反馈类型（查询/闲聊）分组
（信噪比 = 查询反馈 ÷ 闲聊反馈）+ 每日趋势。

数据源为本地 FeedbackStore（SQLite，唯一真相，Langfuse 不可达不影响看板）。
feedback_type 为空（存量/未判定）的记录在首次聚合时惰性判定（LRU 缓存，
见 agent.feedback.feedback_type），判定失败保持 unknown 单独归组，不污染
query/chat 口径。

指标口径（见 docs/langfuse平台/NL2SQL反馈闭环优化设计方案.md §3.1）：
  total          近 N 天有效反馈数（撤销即本地删除，天然不含）
  positive_rate  positive / total
  signal_noise   查询类反馈数 / 闲聊类反馈数（chat=0 → null，前端显示 ∞/N/A）
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response

_logger = logging.getLogger(__name__)

_DEFAULT_DAYS = 7
_MAX_DAYS = 365


def _local_date(iso: str) -> str:
    """ISO UTC 时间 → 服务器本地日期（YYYY-MM-DD），按本地日切。"""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return (iso or "")[:10]


def _classify_empty(rec) -> str:
    """对 feedback_type 为空（存量/未判定）的记录做惰性判定。

    失败/Langfuse 不可达 → 保持 ''（unknown）。快路径（sql 快照）零成本；
    慢路径带 LRU 缓存，多次聚合不重复打 Langfuse。
    """
    try:
        from agent.feedback.feedback_type import classify_feedback_type

        return classify_feedback_type(rec.thread_id, rec.message_id, rec.sql)
    except Exception as e:  # noqa: BLE001
        _logger.debug("[feedback_stats] 惰性判定失败: %s", e)
        return ""


def _bucket(recs: list) -> dict:
    n = len(recs)
    pos = sum(1 for r in recs if r.rating == "positive")
    return {
        "count": n,
        "positive": pos,
        "negative": n - pos,
        "positive_rate": round(pos / n, 4) if n else None,
    }


async def feedback_stats(request: Request):
    try:
        days = int(request.query_params.get("days", _DEFAULT_DAYS))
    except (TypeError, ValueError):
        days = _DEFAULT_DAYS
    days = max(1, min(days, _MAX_DAYS))

    from agent.feedback.store import get_store

    since = datetime.now(timezone.utc) - timedelta(days=days)
    records = get_store().records_in_window(since.isoformat())

    # 分组：query / chat / unknown（'' 惰性判定后仍未知）
    groups: dict[str, list] = {"query": [], "chat": [], "unknown": []}
    trend: dict[str, dict] = defaultdict(lambda: {"total": 0, "positive": 0, "negative": 0,
                                                   "query": 0, "chat": 0})
    for rec in records:
        ftype = rec.feedback_type or _classify_empty(rec)
        if ftype not in ("query", "chat"):
            ftype = "unknown"
        groups[ftype].append(rec)
        d = _local_date(rec.updated_at)
        trend[d]["total"] += 1
        trend[d]["positive"] += 1 if rec.rating == "positive" else 0
        trend[d]["negative"] += 0 if rec.rating == "positive" else 1
        trend[d]["query"] += 1 if ftype == "query" else 0
        trend[d]["chat"] += 1 if ftype == "chat" else 0

    all_recs = groups["query"] + groups["chat"] + groups["unknown"]
    n_query = groups["query"].__len__()
    n_chat = groups["chat"].__len__()
    ratio = round(n_query / n_chat, 2) if n_chat else None

    return json_response(
        {
            "days": days,
            "total": len(all_recs),
            **_bucket(all_recs),
            "query": _bucket(groups["query"]),
            "chat": _bucket(groups["chat"]),
            "unknown": _bucket(groups["unknown"]),
            "signal_noise_ratio": ratio,
            "trend": [
                {"date": d, **trend[d]}
                for d in sorted(trend)
            ],
        }
    )


routes: list[BaseRoute] = [
    Route("/api/feedback/stats", feedback_stats, methods=["GET"]),
]
