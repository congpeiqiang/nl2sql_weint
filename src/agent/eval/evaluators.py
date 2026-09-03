# -*- coding: utf-8 -*-
"""M3 五维在线评估（Langfuse Score 写入）。

五维（docs/langfuse平台/Langfuse接入实现方案.md §3.1 + NL2SQL-评估精准化设计方案.md §8）：
- schema_match_score       Schema 选择正确性：code evaluator 规则（同步）
- sql_valid_score          SQL 合法性/安全性：确定性（同步，复用 sql_approval.classify_sql）
- sql_biz_correct_score    业务语义正确性：LLM-as-a-Judge（采样，落盘队列 worker）
- report_table_score       报表/表格正确性：LLM-as-a-Judge（采样，落盘队列 worker）
- analysis_report_score    分析报告质量/幻觉检测：LLM-as-a-Judge（采样，落盘队列 worker）
- sql_exec_success         SQL 执行是否成功（code evaluator，同步）——SQL 异常类 BadCase 依据

设计（对齐 §3.2「全量规则校验 + 部分 LLM-Judge 采样」+ 设计文档 P0 §8.2/§8.3）：
- 确定性维度在 span 结束时同步写，零成本、无 LLM、每查询必现；
- LLM-judge 维度按采样率（环境变量 NL2SQL_EVAL_JUDGE_SAMPLE，0~1，默认 0.3）
  先入 {AGENT_DATA_ROOT}/eval_queue.sqlite 落盘待评队列，由单例守护 worker
  异步执行（幂等、进程重启自动续跑、失败重试留痕），取代旧 fire-and-forget
  daemon 线程（P0 可靠交付，见 docs/langfuse平台/NL2SQL-评估精准化设计方案.md §8）；
  judge 需要用户问题 → 从执行线程的 state 读最后一条非系统 human 消息
  （HTTP 自调用，与 message_feedback 同模式）。
- 所有 Langfuse 调用兜 try/except：评估是旁路，异常不影响主流程。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re

from agent.middlewares.sql_approval import classify_sql
from agent.trace.langfuse_client import create_score

_logger = logging.getLogger(__name__)

# ── 确定性规则（code evaluator，同步）──────────────────────────

# schema 发现成功/失败的基础分（语义是否正确由 LLM-judge 采样补充）
_SCHEMA_OK_SCORE = 1.0
_SCHEMA_ERR_SCORE = 0.3


def compute_sql_valid_score(sql: str) -> tuple[float, str]:
    """SQL 合法性/安全性评分（确定性，复用 sql_approval 闸门分类）。

    分类同审批闸门（classify_sql）：
    - read（只读查询）      → 1.0
    - full_dump（全表拉取） → 0.4（无 WHERE/LIMIT/聚合，高成本有风险）
    - write/DDL/不可识别    → 0.0（应被拦截，属 BadCase）
    """
    verdict, detail = classify_sql(sql)
    if verdict == "read":
        return 1.0, "只读查询"
    if verdict == "full_dump":
        return 0.4, "疑似全表拉取（无 WHERE/LIMIT/聚合）"
    return 0.0, f"写/DDL 或不可识别语句（{detail or 'UNKNOWN'}）"


def compute_schema_match_score(ok: bool, error: str = "") -> tuple[float, str]:
    """Schema 选择正确性 code 规则（零成本）。

    仅凭发现成功/失败给基础分：成功=1.0（表/字段已解析）；异常=0.3。
    语义层（是否选对表）由 LLM-judge（judge_sql_biz_correct）采样补充。
    """
    if ok:
        return _SCHEMA_OK_SCORE, "Schema 发现成功"
    return _SCHEMA_ERR_SCORE, f"Schema 发现失败: {(error or '')[:120]}"


def compute_sql_exec_success(ok: bool, error: str = "") -> tuple[float, str]:
    """SQL 执行是否成功（code evaluator；失败属 BadCase 第 1/4 类）。

    注意：dbmcp_run_sql 等工具在 SQL 报错时把错误文本作为正常返回值（非异常），
    调用方须先用 looks_like_exec_error 判定后传入 ok=False。
    """
    if ok:
        return 1.0, ""
    return 0.0, f"执行失败: {(error or '')[:120]}"


# DB 报错特征（错误文本作为工具返回值时用于识别执行失败）
_EXEC_ERROR_RE = re.compile(
    r"Error calling tool|Traceback \(most recent call last\)|"
    r"does not exist|doesn't exist|syntax error|SyntaxError|OperationalError|"
    r"ProgrammingError|ExecutionError|connection (timed out|refused|failed)|"
    r"deadlock|query timeout|sqlite3\.|psycopg2\.|mysql\.connector|"
    r"column .* not found|relation .* does not exist|table .* does not exist|"
    r"no such table|no such column|not a valid database|unable to open",
    re.IGNORECASE,
)


def looks_like_exec_error(text: str) -> bool:
    """结果文本是否像 DB 执行错误（而非真实数据）。"""
    if not text:
        return False
    return _EXEC_ERROR_RE.search(str(text)) is not None


# ── LLM-as-a-Judge（采样，后台线程）────────────────────────────

def judge_sample_rate() -> float:
    """LLM-judge 采样率（0~1）。NL2SQL_EVAL_JUDGE_SAMPLE 覆盖，默认 0.3。"""
    try:
        rate = float(os.getenv("NL2SQL_EVAL_JUDGE_SAMPLE", "0.3"))
    except ValueError:
        return 0.3
    return max(0.0, min(1.0, rate))


def should_sample() -> bool:
    """是否命中采样（rate=1.0 恒真，rate=0 恒假）。"""
    rate = judge_sample_rate()
    return rate >= 1.0 or (rate > 0 and random.random() < rate)


def _judge_model():
    """评审用模型：当前 active provider 默认模型（关闭思考控成本）。

    与主 agent 同供应商保证口径一致；失败返回 None → judge 跳过不写分。
    """
    try:
        from agent.llms.model import create_model

        return create_model(enable_thinking=False)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[eval] 创建评审模型失败: %s", e)
        return None


def _parse_json(text: str) -> dict | None:
    """从模型输出提取 JSON 对象（容忍前后缀/围栏）。"""
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None


def _clamp(x) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.5


def judge_sql_biz_correct(question: str, sql: str, result: str) -> float | None:
    """LLM-as-a-Judge：SQL 结果是否满足用户问题意图。返回 0~1；失败返回 None（不写分）。

    已知坑（2026-08-23 实测）：问题说「artists 表」而物理表是 artist（单复数归一化），
    评审模型此前据此判 0 分 → 假阴性。rubric 显式声明：NL 表名可被归一化为物理名，
    不能仅因拼写不一致判错，以「结果是否合理回答意图」为准。
    """
    model = _judge_model()
    if model is None:
        return None
    prompt = (
        "你是 NL2SQL 结果评审员。用户用自然语言提问，SQL 会把表述归一化为物理表/字段名"
        "（如问题说「artists 表」，物理表可能是 artist，属正常归一化，不能据此判错）。\n"
        "评审要点：\n"
        "1. 结果数据是否合理回答了用户问题的意图（数量/明细/汇总是否正确）。\n"
        "2. SQL 是否合理（只读、无语法问题）。\n"
        "3. 结果为空、报错、或数据明显答非所问 → 低分。\n"
        "不要仅因表名/字段名与问题措辞不完全一致就判错。\n"
        f"用户问题：{question[:500]}\n"
        f"执行的 SQL：{sql[:800]}\n"
        f"查询结果（截断）：{result[:1500]}\n\n"
        '只输出 JSON，不要其它文字：{"score": 0.0~1.0, "reason": "一句话理由"}'
    )
    try:
        resp = model.invoke(prompt)
        content = getattr(resp, "content", "") or str(resp)
        data = _parse_json(str(content))
        if not data:
            _logger.debug("[eval] sql_biz_correct judge 输出无法解析: %s", str(content)[:200])
            return None
        return _clamp(data.get("score"))
    except Exception as e:  # noqa: BLE001
        _logger.warning("[eval] sql_biz_correct judge 失败: %s", e)
        return None


def judge_report(question: str, report: str) -> tuple[float | None, float | None]:
    """LLM-as-a-Judge：报告/表格正确性 + 幻觉检测。返回 (table_score, analysis_score)。"""
    model = _judge_model()
    if model is None:
        return None, None
    prompt = (
        "你是数据分析报告评审员。判断报告/表格是否准确回答了用户问题，"
        "以及有无幻觉（编造数据里不存在的结论）。\n"
        f"用户问题：{question[:500]}\n"
        f"报告/表格内容：{report[:2000]}\n\n"
        '只输出 JSON，不要其它文字：{"table_score": 0.0~1.0, "analysis_score": 0.0~1.0, "reason": "..."}'
    )
    try:
        resp = model.invoke(prompt)
        content = getattr(resp, "content", "") or str(resp)
        data = _parse_json(str(content))
        if not data:
            _logger.debug("[eval] report judge 输出无法解析: %s", str(content)[:200])
            return None, None
        return _clamp(data.get("table_score")), _clamp(data.get("analysis_score"))
    except Exception as e:  # noqa: BLE001
        _logger.warning("[eval] report judge 失败: %s", e)
        return None, None


# ── 用户问题提取（judge 需要）────────────────────────────────

def fetch_question(thread_id: str) -> str:
    """读某 thread 的 state，取最后一条非系统 human 消息作为用户问题。

    sub-agent 场景传子线程 id（其 state 首条 human 即问题），主 agent 场景传主线程 id。
    失败/缺失返回空串（judge 跳过）。与 message_feedback._extract_question_sql 同模式。
    """
    if not thread_id:
        return ""
    import httpx

    base = (os.environ.get("LANGGRAPH_API_URL") or "http://localhost:2026").rstrip("/")
    try:
        # trust_env=False：本调用恒为服务内部自调用，直连目标；否则会读
        # HTTP(S)_PROXY 被代理劫持（judge worker 每任务挂满 10s 超时，队列排空极慢）。
        r = httpx.get(f"{base}/threads/{thread_id}/state", timeout=10.0, trust_env=False)
        if r.status_code != 200:
            return ""
        state = r.json()
        messages = (state.get("values") or {}).get("messages") or []
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or msg.get("type")
            if role not in ("human", "user"):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            text = str(content).strip()
            if not text or text.startswith("[系统") or text.startswith("[系统自动通知]"):
                continue
            return text
    except Exception as e:  # noqa: BLE001
        _logger.debug("[eval] 读 thread %s state 失败: %s", thread_id, e)
    return ""


# ── 采样调度入口（LangfuseSpanMiddleware 调用）────────────────

def _execute_judge_task(
    kind: str,
    trace_id: str,
    question_thread: str,
    sql: str = "",
    result: str = "",
    report: str = "",
) -> None:
    """执行一次 LLM-judge 并写分（eval_queue 守护 worker 调用的执行体）。

    与旧 daemon 线程体内逻辑一致。判定类「软失败」（未取到问题 / judge 输出
    不可解析）按原语义静默跳过，不抛异常；意外异常向上抛给 eval_queue 记失败
    并重试（超过上限置 failed 留痕），供运维可查。
    """
    q = fetch_question(question_thread)
    if not q:
        _logger.debug("[eval] 未取到用户问题，跳过 judge（thread=%s）", question_thread)
        return
    if kind == "sql_biz_correct":
        score = judge_sql_biz_correct(q, sql, result)
        if score is not None:
            create_score(
                name="sql_biz_correct_score", value=score,
                trace_id=trace_id, comment="LLM-judge(采样)",
            )
            _logger.info("[eval] sql_biz_correct_score=%.2f trace=%s", score, trace_id[:12])
    elif kind == "report":
        table_score, analysis_score = judge_report(q, report)
        if table_score is not None:
            create_score(
                name="report_table_score", value=table_score,
                trace_id=trace_id, comment="LLM-judge(采样)",
            )
            _logger.info("[eval] report_table_score=%.2f trace=%s", table_score, trace_id[:12])
        if analysis_score is not None:
            create_score(
                name="analysis_report_score", value=analysis_score,
                trace_id=trace_id, comment="LLM-judge(采样)",
            )
            _logger.info("[eval] analysis_report_score=%.2f trace=%s", analysis_score, trace_id[:12])


def schedule_judge(
    kind: str,
    trace_id: str,
    question_thread: str,
    sql: str = "",
    result: str = "",
    report: str = "",
) -> None:
    """把一次 LLM-judge 采样任务入**落盘待评队列**（幂等），由守护 worker 执行写分。

    P0 可靠交付（docs/langfuse平台/NL2SQL-评估精准化设计方案.md §8.3）：取代旧
    fire-and-forget daemon 线程——任务持久化到 {AGENT_DATA_ROOT}/eval_queue.sqlite，
    进程崩溃/重启后 pending 自动续跑（补评）；失败可重试可查。签名与调用点不变。

    Args:
        kind: "sql_biz_correct"（子 agent 执行 span 后）| "report"（主 agent 报表 span 后）
        trace_id: 写分目标 trace（sql_biz_correct → 子 trace；report → 主 trace）
        question_thread: 从该 thread 的 state 读用户问题（子 agent 是子线程，主 agent 是主线程）
    """
    # 离线隔离：run_experiment 实验 worker 设 NL2SQL_EVAL_JUDGE_QUEUE=0 → 不把 LLM-judge
    # 任务入队、不起 drainer。原因：worker 与在线生产共用同一
    # {AGENT_DATA_ROOT}/eval_queue.sqlite，worker 起 drainer 时 _drain_loop 启动即
    # reset_running_to_pending() 会把在线 drainer 正在跑的 running 行重置 → 重复打分。
    # 实验的 sql_biz_correct 由 run_experiment._score_record 同步直评（不依赖本队列），
    # 确定性分（sql_valid/exec/schema）仍同步写实验 trace → 关掉入队不丢任何分。
    try:
        gate = (os.getenv("NL2SQL_EVAL_JUDGE_QUEUE", "1") or "1").strip().lower()
        if gate in ("0", "false", "no", "off"):
            _logger.debug("[eval] LLM-judge 入队已禁用（NL2SQL_EVAL_JUDGE_QUEUE=%s）", gate)
            return
        from agent.eval.eval_queue import enqueue, ensure_worker

        enqueue(kind, trace_id, question_thread, sql=sql, result=result, report=report)
        ensure_worker(_execute_judge_task)
    except Exception as e:  # noqa: BLE001
        _logger.debug("[eval] judge 入队失败: %s", e)
