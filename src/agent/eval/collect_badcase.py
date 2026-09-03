# -*- coding: utf-8 -*-
"""BadCase 每日采集 → Langfuse Dataset「badcase」。

对齐 docs/langfuse平台/Langfuse接入实现方案.md §3.5：
按条件筛选 BadCase：
  1) 系统异常（trace status = ERROR）
  2) 五维分低于阈值（默认 < 0.6）：schema_match / sql_valid / sql_biz_correct /
     report_table / analysis_report
  3) user-feedback = 0（用户差评）
  4) sql_exec_success = 0（SQL 执行失败）
→ 按 session 去重（本地 stamp 文件）→ 写入 Dataset「badcase」
  （create_dataset_item 带 source_trace_id 链回 trace，待人工复审）→ 根因写入 metadata。

用法（进程外分离启动，避免随会话被杀，见记忆 server-run-durability-background-task）：
    .venv/Scripts/python.exe -m agent.eval.collect_badcase --days 1
    .venv/Scripts/python.exe -m agent.eval.collect_badcase --days 30 --force   # 重扫历史并重放
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_logger = logging.getLogger("collect_badcase")

_PROJECT_ROOT = Path(__file__).resolve().parents[3]  # src/agent/eval/collect_badcase.py → 项目根


def _load_env() -> None:
    """独立脚本运行先加载项目 env（start_server 由入口加载；此处兜底）。

    生产部署在容器（env_file: .env.prod 注入）；宿主机/本机手动跑时统一由
    agent.settings.env_loader 叠加 .env.prod 的 LANGFUSE_*（生产项目凭据）。
    """
    from agent.settings.env_loader import load_env

    load_env()

# 五维 + 执行成功：值低于阈值 → BadCase（§3.5 第 2/4 类）
SCORE_DIMS = (
    "schema_match_score",
    "sql_valid_score",
    "sql_biz_correct_score",
    "report_table_score",
    "analysis_report_score",
    "sql_exec_success",
)
DEFAULT_THRESHOLD = 0.6


def _get_client():
    _load_env()
    from agent.trace.langfuse_client import get_client

    return get_client()


def _stamp_path() -> Path:
    """已采集 stamp：{active_workspace}/eval/badcase_collected.json（按 trace_id 去重）。"""
    from agent.workspace_manager import get_workspace_manager

    return get_workspace_manager().active_workspace / "eval" / "badcase_collected.json"


def _load_stamp() -> set[str]:
    try:
        data = json.loads(_stamp_path().read_text(encoding="utf-8"))
        return set(data.get("collected", []))
    except Exception:  # noqa: BLE001
        return set()


def _save_stamp(collected: set[str]) -> None:
    p = _stamp_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {"collected": sorted(collected), "updated_at": datetime.now(timezone.utc).isoformat()},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


def collect(days: int = 1, threshold: float = DEFAULT_THRESHOLD,
            limit: int = 200, force: bool = False) -> int:
    """扫描近 days 天的用户会话，低分/异常/差评 → Dataset「badcase」。返回新采集数。

    v4 平台 events_only 下 legacy trace.list/trace.get 读不到事件新表，改走
    v2/observations 列用户 AGENT root + scores_v3 按 sessionId 汇总分
    （见 agent.trace.langfuse_v4_reads）。会话主 trace = chat_agent root（多次
    run 取最新）；评分跨主/子 trace 统一按 sessionId 汇总。
    """
    from agent.trace.langfuse_v4_reads import (
        get_trace_metadata,
        list_user_traces,
        session_question,
        session_scores_map,
    )

    client = _get_client()
    since = datetime.now(timezone.utc) - timedelta(days=days)

    traces = list_user_traces(since, limit=limit)
    _logger.info("扫描近 %d 天用户 AGENT roots: %d 条", days, len(traces))

    # 按 session 归组，每组取主 trace（chat_agent 优先，按 startTime 取最新）
    by_session: dict[str, list[dict]] = {}
    for t in traces:
        sid = t.get("session_id") or t.get("trace_id")
        if sid:
            by_session.setdefault(sid, []).append(t)

    # 确保 Dataset「badcase」存在（幂等：已存在则复用）
    try:
        client.create_dataset(
            name="badcase",
            description="NL2SQL 自动采集 BadCase（低分/异常/差评），待人工复审",
        )
    except Exception as e:  # noqa: BLE001
        _logger.warning("create_dataset(badcase) 异常（已存在则忽略）: %s", e)

    def _main_of(sid: str, ts: list[dict]) -> dict | None:
        mains = [t for t in ts if t.get("name") == "chat_agent"]
        pool = mains or ts
        pool = [t for t in pool if t.get("trace_id")]
        if not pool:
            return None
        pool.sort(key=lambda t: t.get("start_time") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return pool[0]

    def _session_feedback_type(sid: str) -> str:
        """会话内最新一条本地用户反馈的 feedback_type（差评采集用）。

        本地 FeedbackStore 为唯一真相（thread_id == session_id）；类型为空
        （存量/未判定）时走慢路径判定并回填。任何失败按 ''（非 chat）处理，
        不误丢差评采集。
        """
        try:
            from agent.feedback.feedback_type import classify_feedback_type
            from agent.feedback.store import get_store

            recs = get_store().session_feedback(sid)
            if not recs:
                return ""
            latest = max(recs, key=lambda r: r.updated_at or "")
            if latest.feedback_type:
                return latest.feedback_type
            ftype = classify_feedback_type(
                latest.thread_id, latest.message_id, latest.sql
            )
            if ftype:
                get_store().set_feedback_type(latest.thread_id, latest.message_id, ftype)
            return ftype
        except Exception as e:  # noqa: BLE001
            _logger.debug("[badcase] 判定反馈类型失败（按非 chat 处理）: %s", e)
            return ""

    collected = _load_stamp()
    new_count = 0
    status_items: list[dict] = []  # 积累新条目，循环结束后批量注册状态
    for sid, ts in by_session.items():
        # 隔离离线 A/B 实验（run_experiment）：worker trace 的 session 恒为
        # exp:{label}:{run}（run_experiment.py 注入 langfuse_session_id）。list_user_traces
        # 按 type=AGENT+root 全量枚举、无 environment 过滤，候选臂是坏版本时执行失败
        # SQL（sql_exec_success=0）会污染 Dataset:badcase → 标注队列/门禁基线失真。
        # 前缀过滤是廉价精确信号（生产 session=线程 uuid，不以 exp: 开头）。
        if str(sid).startswith("exp:"):
            _logger.debug("[badcase] 跳过实验 session %s（离线 A/B trace 不进 badcase）", sid[:12])
            continue
        rep = _main_of(sid, ts)
        if not rep:
            continue
        rep_id = rep["trace_id"]
        # 兜底：实验 root trace 名也是 exp:{label}:{idx}（worker 注入 langfuse_trace_name），
        # session 缺失（sid 回退 trace_id）时按 trace 名同样排除。
        if str(rep.get("name") or "").startswith("exp:"):
            _logger.debug("[badcase] 跳过实验 root trace %s（离线 A/B trace 不进 badcase）", rep_id[:12])
            continue
        if rep_id in collected and not force:
            continue
        metadata = get_trace_metadata(rep_id)
        scores = session_scores_map(sid)
        reasons: list[str] = []
        for dim in SCORE_DIMS:
            v = scores.get(dim)
            if v is not None and v < threshold:
                reasons.append(f"{dim}={v:.2f}")
        # 优化①：差评采集只收「查询类」反馈——闲聊/无关对话差评（如「回复太啰嗦」）
        # 不是 NL2SQL 质量信号，跳过采集（类型 ''= 未判定/存量，按 query 处理不丢信号）。
        uf_value = scores.get("user-feedback")
        if uf_value is not None and uf_value < 0.5:
            fb_type = _session_feedback_type(sid)
            if fb_type != "chat":
                reasons.append("user_feedback=0")
            else:
                _logger.debug(
                    "[badcase] session=%s 差评属闲聊反馈（type=chat），跳过采集",
                    sid[:12],
                )
        if not reasons:
            continue
        question = session_question(sid)
        # 记下 db_name，供 run_experiment --from-badcase 回灌时定位同一数据库
        db_name = metadata.get("db_name") or ""
        item_meta: dict = {
            "trace_id": rep_id,
            "reasons": reasons,
            "scores": {k: v for k, v in scores.items() if k in SCORE_DIMS or k == "user-feedback"},
            "collected_at": datetime.now(timezone.utc).date().isoformat(),
            "source": "auto-collect",
        }
        if db_name:
            item_meta["db_name"] = str(db_name)
        client.create_dataset_item(
            dataset_name="badcase",
            input={"question": question or "(未取到问题)", "session_id": sid},
            expected_output=None,
            metadata=item_meta,
            source_trace_id=rep_id,
        )
        collected.add(rep_id)
        new_count += 1
        status_items.append({
            "trace_id": rep_id,
            "question": question or "",
            "db_name": str(db_name),
            "reasons": reasons,
            "collected_at": item_meta["collected_at"],
        })
        _logger.info("[badcase] session=%s reasons=%s", sid[:12], ",".join(reasons))

    # 批量注册状态为 pending（已有状态不覆盖，见 badcase_status.register_batch）
    if status_items:
        try:
            from agent.eval.badcase_status import register_batch

            registered = register_batch(status_items)
            _logger.info("状态跟踪：新注册 %d 条 pending", registered)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[badcase] 状态批量注册失败: %s", e)

    _save_stamp(collected)
    _logger.info("完成：本次新采集 %d 条 BadCase → Dataset:badcase（累计 %d）", new_count, len(collected))
    return new_count


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="BadCase 采集 → Langfuse Dataset:badcase")
    parser.add_argument("--days", type=int, default=1, help="扫描最近 N 天（默认 1）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="低分阈值（默认 0.6）")
    parser.add_argument("--limit", type=int, default=200, help="trace 扫描上限（默认 200）")
    parser.add_argument("--force", action="store_true", help="忽略 stamp，重放已采集项")
    args = parser.parse_args()
    count = collect(days=args.days, threshold=args.threshold,
                    limit=args.limit, force=args.force)
    sys.exit(0 if count >= 0 else 1)


if __name__ == "__main__":
    main()
