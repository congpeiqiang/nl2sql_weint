# -*- coding: utf-8 -*-
"""M5 灰度门禁：离线 A/B 实验（run_experiment.py）。

对齐 docs/langfuse平台/Langfuse接入实现方案.md §M5：
离线跑固定问题集，对两个 prompt label（prod-a / prod-b）各开一个 **worker 子进程**
（进程级 A/B，LANGFUSE_PROMPT_LABEL 在 import graph 前注入 → import 时掷出该 label 的
prompt），跑同一批查询，抽取 run_sql + 结果，按五维评分的确定性维 + 可选 LLM-judge
打分，聚合均值对比 → 回归门禁（核心维掉超过阈值 → exit 1）。

用法（PYTHONPATH=src）：
    # 跑一批（可单 label 查看分数，也可 A/B 对比 + 门禁）
    python -m agent.eval.run_experiment \
        --queries eval/queries/regression.json \
        --labels prod-a prod-b --threshold 0.05 [--judge]

    # 只跑一个 label（不做对比，canary 预检用）
    python -m agent.eval.run_experiment --queries ... --labels prod-a

    # 从 Dataset:badcase 回灌回归集（闭环：线上差评/低分 → 离线回归）
    python -m agent.eval.run_experiment --from-badcase --labels prod-a prod-b \
        [--from-badcase-limit 50]

worker 子进程由本模块 spawn（--worker 模式），流程：
    1. 置 LANGFUSE_PROMPT_LABEL=<label>（import 前，进程级掷骰的显式优先项）
    2. **顶层 import** agent.graphs.nl2sql_agent（MCP 工具在 asyncio 之外加载，
       同生产启动路径——这是进程内 invoke 能用的关键，见 d:/tmp/lf_m5_invoke_probe.py）
    3. asyncio.run 里逐条 ainvoke，抽 run_sql 参数 + 工具结果，算确定性分
       （可选 --judge 追加 sql_biz_correct LLM-judge，同步调用拿值）
    4. 结果 JSONL 落到 --out，供 orchestrator 聚合

注意：
- 实验 trace 带 tags=["nl2sql","experiment"] + session exp:{label}:{run}，
  在 Langfuse 上可用 tag=experiment 过滤，不污染生产会话视图。
- 确定性评分复用 M3 的 evaluators（compute_sql_valid_score / looks_like_exec_error），
  与在线评分口径一致，保证 A/B 数字可对生产 trace 校验。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_logger = logging.getLogger("run_experiment")

_PROJECT_ROOT = Path(__file__).resolve().parents[3]  # src/agent/eval/run_experiment.py → 根
_SRC = _PROJECT_ROOT / "src"

# 回归门禁只看这几维（数值越高越好，candidate 掉超过阈值即失败）
CORE_DIMS = ("sql_biz_correct_score", "sql_valid_score", "sql_exec_success")
AUX_DIMS = ("schema_match_score",)

# worker 默认模型路由（queries 内可逐条覆盖）
_DEFAULT_ROUTE = "deepseek"
_DEFAULT_MODEL = "deepseek-v4-flash"


def _load_env() -> None:
    """独立脚本运行先加载项目 .env（start_server 由入口加载；此处兜底）。"""
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env")


def _reconfigure_stdout() -> None:
    """Windows GBK 控制台会崩 print(⚠/✅)；统一 UTF-8。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


# ── 查询集来源：Dataset:badcase 回灌（闭环 ⑥）────────────────

def _dedupe_queries(queries: list[dict]) -> list[dict]:
    """按 (question, db_name) 去重，空 question 丢弃。"""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for q in queries:
        question = str(q.get("question", "")).strip()
        if not question:
            continue
        key = (question, str(q.get("db_name", "")))
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def _load_badcase_queries(
    limit: int | None = None,
    status_filter: str | None = None,
) -> list[dict]:
    """从 Langfuse Dataset:badcase 读已采集 BadCase 作为回归查询集。

    链路：线上差评/低分 → collect_badcase → Dataset:badcase → 此处回灌 A/B 门禁，
    让「线上暴露的问题」进入离线回归集合（feedback_gate 的查询来源）。
    返回 [{question, db_name?, source_trace_id}]，按 (question, db_name) 去重。

    status_filter：逗号分隔的包含状态（默认 "pending,reviewed"），仅加载这些状态的
    item。传 "all" 跳过状态过滤。fixed/invalid 的 item 视为已关闭，默认不回归。
    """
    from agent.trace.langfuse_client import get_client
    from agent.eval.badcase_status import load_status

    # 状态过滤：解析 include set，"all" → 不过滤
    include_all = False
    if status_filter and status_filter.strip().lower() == "all":
        include_all = True
    elif status_filter:
        include_set = {s.strip() for s in status_filter.split(",") if s.strip()}
    else:
        include_set = set(("pending", "reviewed"))  # 默认：仅开放状态

    # 预加载状态文件（一次 IO，避免逐条读文件）
    status_data = load_status() if not include_all else {}

    def _is_included(trace_id: str) -> bool:
        if include_all:
            return True
        entry = status_data.get(trace_id)
        if entry is None:
            return True  # 未注册 = 旧数据，默认包含
        return entry.get("status", "pending") in include_set

    client = get_client()
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    skipped_status = 0
    page = 1
    while page <= 20:
        resp = client.api.dataset_items.list(
            dataset_name="badcase", page=page, limit=100,
        )
        items = resp.data or []
        if not items:
            break
        for it in items:
            src = getattr(it, "source_trace_id", None) or ""
            if not _is_included(src):
                skipped_status += 1
                continue
            inp = it.input or {}
            question = str(inp.get("question", "")).strip()
            if not question or question == "(未取到问题)":
                continue
            md = it.metadata or {}
            db_name = str(md.get("db_name") or inp.get("db_name") or "").strip()
            key = (question, db_name)
            if key in seen:
                continue
            seen.add(key)
            rec: dict = {"question": question}
            if db_name:
                rec["db_name"] = db_name
            if src:
                rec["source_trace_id"] = src
            out.append(rec)
            if limit and len(out) >= limit:
                break
        if limit and len(out) >= limit:
            break
        if len(items) < 100:
            break
        page += 1
    if skipped_status:
        _logger.info(
            "[badcase] 按状态跳过 %d 条（fixed/invalid 已关闭；--badcase-status all 可包含）",
            skipped_status,
        )
    return out


# ── worker：单 label 跑查询集 ────────────────────────────────

def _extract_text_content(content) -> str:
    """工具结果 content 归一为纯文本（兼容 str / [{type:text}] 两种形态）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text", "")))
        return "\n".join(parts)
    return str(content)


def _extract_run_sql(messages) -> tuple[str, str]:
    """从结果消息抽取最后一次 run_sql 的 (sql, result_text)。

    遍历找最后一条 tool 消息（name 含 run_sql），回看其前一条 AI tool_calls
    里同名调用的 args.sql；result_text 取该 tool 消息正文（可能本身就是报错文本，
    由调用方用 looks_like_exec_error 判定执行失败）。
    """
    sql = ""
    result = ""
    last_tool_idx = -1
    for i, m in enumerate(messages):
        if getattr(m, "type", "") == "tool" and "run_sql" in getattr(m, "name", ""):
            last_tool_idx = i
    if last_tool_idx < 0:
        return "", ""
    result = _extract_text_content(getattr(messages[last_tool_idx], "content", ""))
    for j in range(last_tool_idx - 1, -1, -1):
        prev = messages[j]
        for c in getattr(prev, "tool_calls", None) or []:
            if c.get("name", "").endswith("run_sql"):
                sql = (c.get("args", {}) or {}).get("sql", "") or ""
                break
        if sql:
            break
    return sql, result


def _score_record(question: str, sql: str, result: str, use_judge: bool) -> dict:
    """确定性评分 + 可选 LLM-judge，返回 {scores, reasons}。"""
    from agent.eval.evaluators import (
        compute_sql_valid_score,
        looks_like_exec_error,
        judge_sql_biz_correct,
    )

    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    if not sql:
        # 没生成 SQL（LLM 跑偏/没走到查询工具）→ 五维全低，属 BadCase
        return {
            "scores": {
                "sql_valid_score": 0.0,
                "sql_exec_success": 0.0,
                "schema_match_score": 0.0,
            },
            "reasons": {"sql_valid_score": "未生成 run_sql"},
        }
    v, r = compute_sql_valid_score(sql)
    scores["sql_valid_score"] = v
    reasons["sql_valid_score"] = r
    exec_ok = bool(result.strip()) and not looks_like_exec_error(result)
    scores["sql_exec_success"] = 1.0 if exec_ok else 0.0
    reasons["sql_exec_success"] = "" if exec_ok else ("执行失败/报错文本" if result.strip() else "结果为空")
    # schema_match 近似：能跑出非错误结果 → 表/字段已解析；报错 → 0.3（对齐在线规则）
    if exec_ok:
        scores["schema_match_score"] = 1.0
        reasons["schema_match_score"] = "Schema 发现成功（有结果）"
    else:
        scores["schema_match_score"] = 0.3
        reasons["schema_match_score"] = "Schema 发现失败或执行报错"
    if use_judge and sql:
        try:
            score = judge_sql_biz_correct(question, sql, result[:1500])
            if score is not None:
                scores["sql_biz_correct_score"] = score
                reasons["sql_biz_correct_score"] = "LLM-judge"
        except Exception as e:  # noqa: BLE001
            _logger.warning("[worker] sql_biz_correct judge 异常: %s", e)
    return {"scores": scores, "reasons": reasons}


def _run_worker(label: str, queries: list[dict], out_path: Path, use_judge: bool) -> int:
    """单 label 跑完全部查询，写 JSONL。返回 0=成功 1=worker 内部失败。"""
    # ── import 前注入 label（进程级 A/B 的显式优先项）──
    os.environ["LANGFUSE_PROMPT_LABEL"] = label
    # 评审模型也走同供应商；强制 judge 恒真（本 worker 内直接调用，不受采样影响）
    os.environ["NL2SQL_EVAL_JUDGE_SAMPLE"] = "1.0"

    from agent.trace.langfuse_client import resolve_prompt_label, prompt_label_info

    resolved = resolve_prompt_label()
    info = prompt_label_info()
    _logger.info("[worker] label=%s resolved=%s info=%s", label, resolved, json.dumps(info, ensure_ascii=False))

    # ── 顶层 import（MCP 工具在 asyncio 之外加载）──
    from agent.graphs.nl2sql_agent import agent as g

    from langchain_core.messages import HumanMessage

    # 每次 worker 一个 run id，实验 trace 按此分组
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    records: list[dict] = []

    async def _run_all() -> None:
        for idx, q in enumerate(queries):
            question = str(q.get("question", "")).strip()
            if not question:
                continue
            db_name = str(q.get("db_name", "") or os.getenv("NL2SQL_EVAL_DB", "chinook_aliyun"))
            route = str(q.get("llm_route", "") or _DEFAULT_ROUTE)
            model = str(q.get("llm_model", "") or _DEFAULT_MODEL)
            rec: dict = {
                "label": label,
                "index": idx,
                "question": question,
                "db_name": db_name,
                "run_id": run_id,
                "sql": "",
                "result_head": "",
                "scores": {},
                "reasons": {},
            }
            try:
                result = await g.ainvoke(
                    {"messages": [HumanMessage(content=question)]},
                    {
                        "metadata": {
                            "langfuse_session_id": f"exp:{label}:{run_id}",
                            "langfuse_trace_name": f"exp:{label}:{idx}",
                            "langfuse_tags": ["nl2sql", "experiment"],
                        },
                        "configurable": {
                            "db_name": db_name,
                            "llm_route": route,
                            "llm_model": model,
                            "enable_thinking": False,
                        },
                    },
                )
                msgs = result.get("messages", []) or []
                sql, result_text = _extract_run_sql(msgs)
                scored = _score_record(question, sql, result_text, use_judge)
                rec["sql"] = sql
                rec["result_head"] = result_text[:200]
                rec["scores"] = scored["scores"]
                rec["reasons"] = scored["reasons"]
                _logger.info(
                    "[worker] #%d %s sql=%s exec=%s valid=%.1f",
                    idx, question[:30], (sql or "∅")[:60],
                    scored["scores"].get("sql_exec_success"),
                    scored["scores"].get("sql_valid_score", -1),
                )
            except Exception as e:  # noqa: BLE001
                _logger.warning("[worker] #%d 查询失败: %s", idx, e)
                rec["reasons"]["fatal"] = f"{type(e).__name__}: {e}"
                rec["scores"]["sql_exec_success"] = 0.0
                rec["scores"]["sql_valid_score"] = 0.0
                rec["scores"]["schema_match_score"] = 0.0
            records.append(rec)

    asyncio.run(_run_all())
    out_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
        encoding="utf-8",
    )
    _logger.info("[worker] %s 完成：%d 条 → %s", label, len(records), out_path)
    return 0


# ── orchestrator：spawn worker、聚合、对比、门禁 ──────────────

def _mean(vals: list[float]) -> float | None:
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _aggregate(records: list[dict]) -> dict:
    """聚合单 label 结果：每维均值 + 无SQL/执行失败比例。"""
    out: dict = {"count": len(records)}
    dims = (*CORE_DIMS, *AUX_DIMS)
    for dim in dims:
        vals = [r["scores"][dim] for r in records if dim in r.get("scores", {})]
        out[dim] = _mean(vals)
    total = max(1, len(records))
    out["no_sql_ratio"] = round(sum(1 for r in records if not r.get("sql")) / total, 4)
    out["exec_fail_ratio"] = round(
        sum(1 for r in records if r.get("scores", {}).get("sql_exec_success", 1) == 0) / total, 4,
    )
    return out


def _compare_gate(ref: dict, cand: dict, threshold: float) -> tuple[bool, list[str]]:
    """回归门禁：核心维 candidate 均值 < reference 均值 - threshold → 失败。
    返回 (pass, 失败原因列表)。"""
    failures: list[str] = []
    for dim in CORE_DIMS:
        a, b = ref.get(dim), cand.get(dim)
        if a is None or b is None:
            continue
        if b < a - threshold:
            failures.append(f"{dim}: {a} → {b}（掉 {(a - b):.3f} > 阈值 {threshold}）")
    return (not failures), failures


def _run_orchestrator(args) -> int:
    queries = json.loads(args.queries_path.read_text(encoding="utf-8"))
    if not isinstance(queries, list) or not queries:
        _logger.error("--queries 文件需是查询对象数组")
        return 2

    # ── 逐 label spawn worker 子进程（隔离进程级 A/B）──
    per_label: dict[str, list[dict]] = {}
    for label in args.labels:
        out_path = Path(args.out_dir) / f"exp_{label}.jsonl"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_SRC), os.environ.get("PYTHONPATH", "")]))
        cmd = [
            sys.executable, "-m", "agent.eval.run_experiment",
            "--worker", "--label", label,
            "--queries", str(args.queries_path),
            "--out", str(out_path),
        ]
        if args.judge:
            cmd.append("--judge")
        _logger.info("[orchestrator] spawn: %s", " ".join(cmd[-8:]))
        proc = subprocess.run(cmd, cwd=_PROJECT_ROOT, env=env, timeout=args.timeout)
        if proc.returncode != 0:
            _logger.error("[orchestrator] label=%s worker 失败 (exit=%d)，中止", label, proc.returncode)
            return 1
        per_label[label] = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # ── 聚合展示 ──
    _logger.info("")
    for label, records in per_label.items():
        agg = _aggregate(records)
        _logger.info("[orchestrator] label=%s %s", label, json.dumps(agg, ensure_ascii=False))
    # 落盘 manifest（工作区 eval/experiment_runs/）
    try:
        from agent.workspace_manager import get_workspace_manager

        run_dir = get_workspace_manager().active_workspace / "eval" / "experiment_runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        manifest = {
            "stamp": stamp,
            "queries": args.queries_path.name,
            "labels": {l: _aggregate(recs) for l, recs in per_label.items()},
        }
        (run_dir / f"run_{stamp}.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        _logger.info("[orchestrator] manifest → %s", run_dir / f"run_{stamp}.json")
    except Exception as e:  # noqa: BLE001
        _logger.warning("[orchestrator] manifest 落盘失败: %s", e)

    # ── 门禁 ──
    if len(args.labels) < 2:
        _logger.info("单 label 模式：不对比、不门禁（canary 预检用）。PASS")
        return 0
    ref_label, cand_label = args.labels[0], args.labels[1]
    ref, cand = _aggregate(per_label[ref_label]), _aggregate(per_label[cand_label])
    passed, failures = _compare_gate(ref, cand, args.threshold)
    if passed:
        _logger.info("[orchestrator] ✅ %s vs %s：无回归，门禁通过", ref_label, cand_label)
        return 0
    for f in failures:
        _logger.error("  ❌ %s", f)
    _logger.error("[orchestrator] 回归门禁失败（%s 相对 %s），exit 1", cand_label, ref_label)
    return 1


# ── 入口 ────────────────────────────────────────────────────

def main() -> None:
    _reconfigure_stdout()
    _load_env()
    parser = argparse.ArgumentParser(description="M5 A/B 实验：离线跑查询集 + 回归门禁")
    parser.add_argument("--worker", action="store_true", help="worker 子进程模式（内部使用）")
    parser.add_argument("--label", default="", help="worker 模式：本进程使用的 prompt label")
    parser.add_argument("--queries", default="", help="查询集 JSON（[{question, db_name?, llm_route?, llm_model?}]）；缺省时若给了 --from-badcase 则用 badcase 集")
    parser.add_argument("--out", default="", help="worker 模式：结果 JSONL 输出路径")
    parser.add_argument("--from-badcase", action="store_true", help="查询集取 Dataset:badcase（闭环回灌），可与 --queries 合并")
    parser.add_argument("--from-badcase-limit", type=int, default=0, help="badcase 回灌条数上限（默认不限）")
    parser.add_argument(
        "--badcase-status", default="",
        help="badcase 状态过滤（逗号分隔，默认 pending,reviewed；传 all 包含全部）",
    )
    parser.add_argument("--judge", action="store_true", help="追加 sql_biz_correct LLM-judge 打分（慢，控成本）")
    parser.add_argument("--labels", nargs="+", default=["prod-a"], help="参与对比的 label（首个为 reference）")
    parser.add_argument("--threshold", type=float, default=0.05, help="回归门禁阈值（默认 0.05）")
    parser.add_argument("--out-dir", default=str(Path(_PROJECT_ROOT) / ".tmp" / "experiment"), help="结果输出目录")
    parser.add_argument("--timeout", type=int, default=1800, help="单 worker 超时秒（默认 1800）")
    args = parser.parse_args()

    if args.worker:
        if not args.label:
            parser.error("--worker 需要 --label")
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
        sys.exit(_run_worker(args.label, queries, out_path, args.judge))

    # ── 解析查询集：--from-badcase 回灌（可与 --queries 合并、去重）──
    merged: list[dict] = []
    if args.queries:
        merged += json.loads(Path(args.queries).read_text(encoding="utf-8"))
    if args.from_badcase:
        bad = _load_badcase_queries(
            args.from_badcase_limit or None,
            status_filter=args.badcase_status or None,
        )
        _logger.info("Dataset:badcase 装载 %d 条查询", len(bad))
        merged += bad
    merged = _dedupe_queries(merged)
    if not merged:
        _logger.error("查询集为空：需 --queries 文件或 --from-badcase 提供查询")
        sys.exit(2)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    qpath = out_dir / f"queries_{stamp}.json"
    qpath.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    _logger.info("查询集 %d 条（去重后）→ %s", len(merged), qpath)
    args.queries_path = qpath
    sys.exit(_run_orchestrator(args))


if __name__ == "__main__":
    main()
