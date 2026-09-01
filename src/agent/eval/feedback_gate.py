# -*- coding: utf-8 -*-
"""M6 反馈闭环：真实用户反馈 A/B 门禁（feedback_gate.py）。

数据来源：前端 👍/👎/评论 → PUT /api/threads/{tid}/messages/{mid}/feedback
→ Langfuse `user-feedback` score（好评 1 / 差评 0，comment 存评语）。
本脚本把「真实反馈」接入灰度放量/发版判断：按 trace 上记录的 prompt_label 分组，
比较两组好评率；candidate 好评率低于 reference − 阈值 → exit 1（不放量/触发回滚）。

与 run_experiment（离线 LLM/确定性评分门禁）互补：
  - run_experiment：批量跑固定问题集，评「模型现在做得好不好」（离线、可重复）
  - feedback_gate：聚合真实用户点赞/差评，评「线上用户觉得好不好」（在线、权威）

闭环全链：
    前端反馈 → Langfuse user-feedback score ─→ feedback_gate（本文件）门禁
              ↘ 差评/低分 → collect_badcase → Dataset:badcase → run_experiment --from-badcase

用法（PYTHONPATH=src，脚本自载项目 .env）：
    # 默认按 prompt_label 分组：ref=production vs cand=prod-a
    .venv/Scripts/python.exe -m agent.eval.feedback_gate --days 7

    # 显式指定分组对比 + 阈值 + 最少样本
    .venv/Scripts/python.exe -m agent.eval.feedback_gate --days 7 \
        --ref production --cand prod-a --threshold 0.05 --min-rated 5

    # 只看报表，不门禁（exit 恒 0）
    .venv/Scripts/python.exe -m agent.eval.feedback_gate --days 7 --report-only

manifest：{active_workspace}/eval/feedback_gates/feedback_gate_{stamp}.json
退出码：0=通过/跳过  1=回归（candidate 好评率低于 reference−阈值）  2=数据不足且 --fail-insufficient
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_logger = logging.getLogger("feedback_gate")

_PROJECT_ROOT = Path(__file__).resolve().parents[3]  # src/agent/eval/feedback_gate.py → 根


def _load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env")


def _reconfigure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def _with_retry(fn, retries: int = 3, delay: float = 2.0):
    """Cloud 偶发 httpx.ReadTimeout，重试到成功或耗尽（最后一次异常上抛）。"""
    last: Exception | None = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                _logger.warning("[feedback_gate] 重试 %d/%d: %s", i + 1, retries, e)
                time.sleep(delay)
    raise last  # type: ignore[misc]


def get_client():
    _load_env()
    from agent.trace.langfuse_client import get_client as _get

    return _get()


# ── 数据拉取 ────────────────────────────────────────────────

def _ts_key(r: dict):
    ts = r.get("timestamp")
    if isinstance(ts, datetime):
        return ts.timestamp()
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts).timestamp()
        except Exception:  # noqa: BLE001
            return 0.0
    return 0.0


def _fetch_scores(since: datetime) -> list[dict]:
    """拉近 days 天所有 user-feedback score（v3 游标分页，单页上限 100）。

    v4 平台 events_only 下 legacy scores.get_many 读不到事件新表，改走
    scores_v3.get_many_v3（events 表数据）。字段与 legacy 对齐。
    """
    from agent.trace.langfuse_v4_reads import list_scores

    rows = _with_retry(lambda: list_scores(name="user-feedback", since=since, limit=2000))
    _logger.info("[feedback_gate] user-feedback 共 %d 条", len(rows))
    return rows


def _dedupe_scores(rows: list[dict]) -> list[dict]:
    """同一 trace 可能多次打分（点赞后改评/存评论→各写一条 score），取最新一条。

    v4 撤销用哨兵分表达（value<0=已撤销，见 langfuse_v4_reads.USER_FEEDBACK_REVOKED）：
    最新一条 value<0 → 该 trace 视为无有效反馈（已撤销），从统计中剔除——
    否则撤销后残留的旧点赞分会被误计为好评。
    """
    from agent.trace.langfuse_v4_reads import is_revoked

    best: dict[str, dict] = {}
    for r in rows:
        tid = r.get("trace_id", "")
        if not tid:
            continue
        prev = best.get(tid)
        if prev is None or _ts_key(r) > _ts_key(prev):
            best[tid] = r
    return [r for r in best.values() if not is_revoked(r.get("value"))]


def _trace_group_dict(md: dict) -> tuple[str, str]:
    """从 v4 root observation metadata 返回 (group_key, group_type)。

    M5 起的 trace 在 metadata.prompt 记录分流结果（Langfuse 注入时被字符串化
    成 "{'prompt_label': ...}"，需还原）。prompt.prompt_label 优先，兜底
    metadata.langfuse_release（客户端级 LANGFUSE_RELEASE，全组相同，仅作明细参考）。
    """
    from agent.trace.langfuse_v4_reads import _parse_str_dict

    prompt = md.get("prompt")
    pd = _parse_str_dict(prompt)
    lbl = pd.get("prompt_label")
    if lbl:
        return str(lbl), "prompt_label"
    rel = md.get("langfuse_release") or ""
    if rel:
        return f"release:{rel}", "release"
    return "", "unknown"


def _resolve_groups(deduped: list[dict]) -> tuple[dict[str, list[dict]], int]:
    """逐 trace 取 metadata.prompt_label 分组；无法归类的记 unknown 数。

    v4 平台 events_only 下 legacy trace.get 读不到事件新表，改走 v2/observations
    取 root observation 的 metadata（trace 级业务元数据）。
    """
    from agent.trace.langfuse_v4_reads import get_trace_metadata

    groups: dict[str, list[dict]] = {}
    unknown = 0
    for r in deduped:
        tid = r["trace_id"]
        try:
            md = _with_retry(lambda: get_trace_metadata(tid))
        except Exception as e:  # noqa: BLE001
            _logger.warning("[feedback_gate] 取 trace %s metadata 失败: %s", tid, e)
            continue
        group, gtype = _trace_group_dict(md)
        if not group:
            unknown += 1
            continue
        groups.setdefault(group, []).append({**r, "group_type": gtype})
    return groups, unknown


# ── 聚合与门禁 ──────────────────────────────────────────────

def _aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    pos = sum(1 for r in rows if r.get("value", 0) >= 0.5)
    return {
        "n_rated": n,
        "n_pos": pos,
        "positive_rate": round(pos / n, 4) if n else None,
    }


def _recent_negatives(rows: list[dict], top: int = 10) -> list[dict]:
    neg = [r for r in rows if r.get("value", 0) < 0.5]
    neg.sort(key=_ts_key, reverse=True)
    out: list[dict] = []
    for r in neg[:top]:
        ts = r.get("timestamp")
        out.append(
            {
                "trace_id": r["trace_id"],
                "comment": r.get("comment", ""),
                "time": (ts.isoformat() if isinstance(ts, datetime) else str(ts or "")),
            }
        )
    return out


def _gate(ref: dict, cand: dict, threshold: float, min_rated: int,
          fail_insufficient: bool) -> tuple[int, str]:
    """好评率对比门禁。返回 (exit_code, 结论文本)。"""
    if ref["n_rated"] < min_rated or cand["n_rated"] < min_rated:
        msg = (
            f"数据不足：ref rated={ref['n_rated']} / cand rated={cand['n_rated']}"
            f"（< min_rated={min_rated}）"
        )
        if fail_insufficient:
            return 2, f"{msg}；--fail-insufficient，门禁按失败处理"
        return 0, f"{msg}；跳过门禁（--fail-insufficient 可改失败）"
    ref_rate, cand_rate = ref["positive_rate"], cand["positive_rate"]
    if cand_rate < ref_rate - threshold:
        return 1, f"回归：candidate 好评率 {cand_rate} < reference {ref_rate} − 阈值 {threshold}"
    return 0, f"通过：candidate {cand_rate} ≥ reference {ref_rate} − 阈值 {threshold}"


# ── 入口 ────────────────────────────────────────────────────

def main() -> None:
    _reconfigure_stdout()
    _load_env()
    ap = argparse.ArgumentParser(description="M6 真实反馈 A/B 门禁（user-feedback 好评率）")
    ap.add_argument("--days", type=int, default=7, help="统计近 N 天（默认 7）")
    ap.add_argument("--ref", default="production", help="reference 组（默认 production）")
    ap.add_argument("--cand", default="prod-a", help="candidate 组（默认 prod-a）")
    ap.add_argument("--threshold", type=float, default=0.05, help="好评率回归阈值（默认 0.05）")
    ap.add_argument("--min-rated", type=int, default=5, help="每组最少有效反馈数才门禁（默认 5）")
    ap.add_argument("--report-only", action="store_true", help="只看报表不门禁（exit 恒 0）")
    ap.add_argument("--fail-insufficient", action="store_true", help="数据不足时按失败处理（exit 2）")
    args = ap.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    _logger.info("[feedback_gate] 拉取近 %d 天 user-feedback（since=%s）", args.days, since.isoformat())

    rows = _fetch_scores(since)
    _logger.info("[feedback_gate] 原始 score %d 条", len(rows))
    deduped = _dedupe_scores(rows)
    _logger.info("[feedback_gate] 去重后（按 trace 取最新）%d 条", len(deduped))
    groups, unknown = _resolve_groups(deduped)
    _logger.info("[feedback_gate] 分组: %s", {k: len(v) for k, v in groups.items()})
    if unknown:
        _logger.info("[feedback_gate] 无法归类的 trace（无 prompt_label 元数据）: %d 条", unknown)

    # ── 报表 ──
    table = {g: _aggregate(rows_g) for g, rows_g in groups.items()}
    _logger.info("")
    _logger.info("[feedback_gate] 各组好评率（共 %d 组）:", len(table))
    for g, agg in sorted(table.items(), key=lambda kv: (-kv[1]["n_rated"], kv[0])):
        rate = "N/A" if agg["positive_rate"] is None else f"{agg['positive_rate']:.2%}"
        _logger.info("  %-14s rated=%-4d pos=%-4d rate=%s", g, agg["n_rated"], agg["n_pos"], rate)
    _logger.info("")

    # 每组最近差评明细（决策参考）
    for g in sorted(groups):
        neg = _recent_negatives(groups[g], top=5)
        if not neg:
            continue
        _logger.info("  [%s] 最近差评 %d 条:", g, len(neg))
        for n in neg:
            _logger.info("    · %s  comment=%s  trace=%s", n["time"], n["comment"] or "(无)", n["trace_id"][:12])

    # ── manifest ──
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    manifest = {
        "stamp": stamp,
        "window_days": args.days,
        "since": since.isoformat(),
        "scores_raw": len(rows),
        "scores_deduped": len(deduped),
        "unknown_group": unknown,
        "groups": table,
        "ref": args.ref,
        "cand": args.cand,
        "threshold": args.threshold,
        "min_rated": args.min_rated,
        "negatives": {g: _recent_negatives(groups[g], top=20) for g in groups},
    }
    try:
        from agent.workspace_manager import get_workspace_manager

        out_dir = get_workspace_manager().active_workspace / "eval" / "feedback_gates"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"feedback_gate_{stamp}.json"
        out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        _logger.info("[feedback_gate] manifest → %s", out_path)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[feedback_gate] manifest 落盘失败: %s", e)

    if args.report_only:
        _logger.info("[feedback_gate] --report-only：不门禁。PASS")
        sys.exit(0)

    ref_agg = table.get(args.ref, {"n_rated": 0, "n_pos": 0, "positive_rate": None})
    cand_agg = table.get(args.cand, {"n_rated": 0, "n_pos": 0, "positive_rate": None})
    code, msg = _gate(ref_agg, cand_agg, args.threshold, args.min_rated, args.fail_insufficient)
    if code == 0:
        _logger.info("[feedback_gate] ✅ %s", msg)
    elif code == 1:
        _logger.error("[feedback_gate] ❌ %s（exit 1）", msg)
    else:
        _logger.error("[feedback_gate] ⚠ %s（exit 2）", msg)
    sys.exit(code)


if __name__ == "__main__":
    main()
