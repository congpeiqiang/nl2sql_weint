# -*- coding: utf-8 -*-
"""用户反馈类型判别：NL2SQL 查询问题（query）vs 闲聊/无关对话（chat）。

背景：user-feedback 评分（好评 1 / 差评 0）既有针对真实数据查询的质量反馈，
也有针对闲聊/无关对话的抱怨（如「回复太啰嗦」）。后者不是 NL2SQL 质量信号，
会让 collect_badcase 误采 badcase、让 feedback_gate 好评率失真。
本模块把每条反馈归为 query / chat / ''（未判定），消费端（采集/门禁/看板）
按需过滤。

判定信号（按可靠性排序）：
  1. 确定性分：trace 上存在 SCORE_DIMS 任一维度 —— LangfuseSpanMiddleware
     只在 sql-execution 类工具写分，闲聊 trace 恒无、查询 trace 恒有
     （SQL 失败也写 sql_exec_success=0）；
  2. run_sql 族 observation：trace 存在 skill:sql-execution / schema-linking
     span，或工具名以 _run_sql/_dry_run/_dry_plan/_query_cube 结尾；
  3. SQL 快照：本地 FeedbackStore.sql 非空（快路径，零成本）。

快路径（写入时）：FeedbackStore.sql 非空 → 'query'。
慢路径（打分/聚合/入队时）：find_message_trace_id 定位 trace → 信号检查。
无 trace / Langfuse 不可达 → ''（未知：不参与 query 统计，也不误判为 chat，
避免把闲聊差评误当查询信号的反向污染）。

消费端约定：
  - collect_badcase：user-feedback 差评且类型 != 'chat' 才采集；
  - feedback_gate：默认只统计 query（metadata.feedback_type == 'chat' 剔除，
    '' 按 query 处理以兼容存量/未判定，不缩样）；
  - 看板：query vs chat 分组统计信噪比。
"""
from __future__ import annotations

import functools
import logging

_logger = logging.getLogger(__name__)

# 确定性分维度：任一出现即该 trace 是一次数据查询（LangfuseSpanMiddleware._maybe_score 写）
QUERY_SCORE_DIMS = (
    "schema_match_score",
    "sql_valid_score",
    "sql_biz_correct_score",
    "report_table_score",
    "analysis_report_score",
    "sql_exec_success",
)

# run_sql 族 observation 名特征
_QUERY_OBS_PREFIXES = ("skill:sql-execution", "skill:schema-linking")
_QUERY_TOOL_SUFFIXES = ("_run_sql", "_dry_run", "_dry_plan", "_query_cube")


def trace_has_query_signal(trace_id: str) -> bool:
    """该 trace 是否有数据查询痕迹（确定性分或 run_sql 族 observation）。

    任何失败返回 False（保守：宁判 chat 不误判查询信号——但注意调用方
    classify_feedback_type 只在能定位 trace 时才走到这里）。
    """
    try:
        from agent.trace.langfuse_v4_reads import (
            get_trace_scores,
            list_trace_observation_names,
        )

        for s in get_trace_scores(trace_id):
            if s.get("name") in QUERY_SCORE_DIMS:
                return True
        for name in list_trace_observation_names(trace_id):
            if name.startswith(_QUERY_OBS_PREFIXES):
                return True
            if name.endswith(_QUERY_TOOL_SUFFIXES):
                return True
    except Exception as e:  # noqa: BLE001
        _logger.debug("[feedback_type] trace %s 信号检查失败: %s", trace_id[:12], e)
    return False


def _find_trace(thread_id: str, message_id: str) -> str:
    """定位反馈所属 trace（慢路径入口；复用 message_feedback 同款语义）。"""
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
        _logger.debug("[feedback_type] 定位 trace 失败: %s", e)
    return ""


@functools.lru_cache(maxsize=4096)
def _classify_cached(thread_id: str, message_id: str, sql_snapshot: str) -> str:
    """慢路径（带 LRU 缓存）。同一条消息的归类稳定（trace 不变化）。

    sql_snapshot 非空 → 'query'（快路径兜底，先于缓存命中判定，见
    classify_feedback_type 主函数）；此处只处理 trace 信号判定。
    """
    trace_id = _find_trace(thread_id, message_id)
    if not trace_id:
        return ""
    return "query" if trace_has_query_signal(trace_id) else "chat"


def classify_feedback_type(
    thread_id: str,
    message_id: str,
    sql_snapshot: str = "",
    trace_id: str = "",
) -> str:
    """反馈类型：'query' | 'chat' | ''（未判定）。

    快路径：sql 快照非空 → 'query'（本地零成本，不依赖 Langfuse）。
    慢路径：定位 trace（可复用调用方已定位的 trace_id）→ 确定性分/run_sql
            observation 信号 → query；无信号 → chat。
    无 trace / Langfuse 不可达 → ''。

    trace_id 由调用方传入可省一次 find_message_trace_id（message_feedback
    打分线程已定位，直接复用）。
    """
    if sql_snapshot and sql_snapshot.strip():
        return "query"
    if trace_id:
        try:
            return "query" if trace_has_query_signal(trace_id) else "chat"
        except Exception as e:  # noqa: BLE001
            _logger.debug("[feedback_type] trace %s 判定失败: %s", trace_id[:12], e)
            return ""
    return _classify_cached(thread_id, message_id, sql_snapshot or "")
