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

    # 从 Langfuse Dataset 选查询集（Langfuse Experiment 离线跑；闭环：线上问题 → 回归）
    python -m agent.eval.run_experiment --dataset badcase --labels prod-a prod-b \
        [--dataset-limit 50] [--badcase-status pending,reviewed]

    # 语义库 A/B：同一 prompt，只换 wrenai 语义库版本（git ref 物化）
    python -m agent.eval.run_experiment --dataset badcase \
        --labels prod-a prod-a --semantic chinook_aliyun=v5.0.0 chinook_aliyun=v6.0.0

    # 结构化 arm（每臂独立指定 prompt × skill × 语义库三件套；缺省维度走默认）：
    python -m agent.eval.run_experiment --arms arms.json --dataset badcase --judge
    #   arms.json: [{"name":"ref","prompt_label":"prod-a","skill_ref":"","semantic_ref":""},
    #               {"name":"cand","prompt_label":"prod-a","skill_ref":"skills-v1","semantic_ref":"chinook_aliyun=v6.0.0"}]

    # 结果落 Langfuse Dataset Run（--run-name 自定义；UI Dataset → Runs 对比多轮）

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
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_logger = logging.getLogger("run_experiment")

_PROJECT_ROOT = Path(__file__).resolve().parents[3]  # src/agent/eval/run_experiment.py → 根
_SRC = _PROJECT_ROOT / "src"

# 回归门禁只看这几维（数值越高越好，candidate 掉超过阈值即失败）
CORE_DIMS = ("sql_biz_correct_score", "sql_valid_score", "sql_exec_success")
AUX_DIMS = ("schema_match_score",)

# worker 默认模型路由（queries 内可逐条覆盖：llm_route/llm_model）
# 空串 = 跟随主模型：active provider 默认模型（与线上聊天一致）。
# 不再写死 qwen —— 写死会把整场实验绑死在单一接入点上，其 key 失效即全军覆没
#（2026-09-03 生产：阿里云百炼 qwen 接入点 key 被封 → 所有离线 run 首题 401）。
_DEFAULT_ROUTE = ""
_DEFAULT_MODEL = ""

# ── 运行中「停止」协作取消 ──────────────────────────────
# 三层进程（API / orchestrator / worker 子进程）共用同一**停止标记文件**做协作取消：
# API cancel 端点写标记 → orchestrator 每臂顶部 / worker 每题顶部检查 → 自然断点干净退出。
# 不做 kill subprocess：单题 agent 在跑 LLM 时强杀会留半截 trace。
# 标记文件统一放 <per-stamp run_dir>/cancel（= Path(out_dir).parent / "cancel"）。
_CANCEL_FILE_NAME = "cancel"
# orchestrator 检测到停止时返回的专用退出码 → API 映射为 status=cancelled（区别于 0/1/2）
RC_CANCELLED = 130


def _cancel_path(out_dir: str | Path) -> Path:
    """从 orchestrator/worker 的 out_dir 推出停止标记文件路径。

    out_dir = <run_dir>/out（per-stamp 结果目录）→ 标记 = <run_dir>/cancel，
    与 API 侧 `experiment.py` 写标记用的 `_run_dir()/stamp/"cancel"` 同一路径。
    """
    return Path(out_dir).parent / _CANCEL_FILE_NAME


def _load_env() -> None:
    """独立脚本运行先加载项目 env（start_server 由入口加载；此处兜底）。

    生产部署在容器（env_file: .env.prod 注入）；宿主机/本机手动跑时统一由
    agent.settings.env_loader 叠加 .env.prod 的 LANGFUSE_*（生产项目凭据）。
    """
    from agent.settings.env_loader import load_env

    load_env()


def _reconfigure_stdout() -> None:
    """Windows GBK 控制台会崩 print(⚠/✅)；统一 UTF-8。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


# ── 查询集来源：Dataset:badcase 回灌（闭环 ⑥）────────────────

def _dedupe_queries(queries: list[dict]) -> list[dict]:
    """按 (question, db_name) 去重（保留首个记录含 dataset_item_id 的字段），空 question 丢弃。

    --dataset all（badcase+goodcase 合并）时同一问题同库只跑一次，避免冗余执行；
    item_id 取首个出现（badcase 在前）。裸查询（--queries 文件）行为同旧版。
    """
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


def _load_dataset_queries(
    dataset_name: str,
    limit: int | None = None,
    status_filter: str | None = None,
) -> list[dict]:
    """从 Langfuse Dataset（badcase / goodcase）读已采集问题作为回归查询集。

    链路：线上差评/低分 → collect_badcase → Dataset:badcase；点赞/正确查询 →
    Dataset:goodcase。此处回灌离线实验，让「线上暴露的问题」进入回归集合。
    返回 [{question, db_name?, source_trace_id?, dataset_item_id, dataset_name}]，
    按 (question, db_name) 去重。dataset_item_id 用于把实验结果关联回 Dataset Run
    （Langfuse Experiment 离线落库）。

    status_filter：仅 badcase 生效，逗号分隔的包含状态（默认 "pending,reviewed"），
    传 "all" 跳过状态过滤。fixed/invalid 的 item 视为已关闭，默认不回归。
    """
    from agent.trace.langfuse_client import get_client
    from agent.eval.badcase_status import load_status

    # 状态过滤：badcase 默认仅开放状态；goodcase 无状态概念
    if status_filter and status_filter.strip().lower() == "all":
        include_all = True
        include_set: set[str] = set()
    elif status_filter:
        include_all = False
        include_set = {s.strip() for s in status_filter.split(",") if s.strip()}
    else:
        include_all = dataset_name != "badcase"
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
            dataset_name=dataset_name, page=page, limit=100,
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
            rec: dict = {
                "question": question,
                "dataset_name": dataset_name,
                "dataset_item_id": str(getattr(it, "id", "") or ""),
            }
            if db_name:
                rec["db_name"] = db_name
            if src:
                rec["source_trace_id"] = src
            # 金标 expected_output 随查询带到 worker，供 Experiment 页 Expected Output
            # 列（goodcase 才有；badcase 无金标不注入）
            _exp_out = getattr(it, "expected_output", None)
            if _exp_out is not None:
                rec["expected_output"] = _exp_out
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
            "[dataset:%s] 按状态跳过 %d 条（fixed/invalid 已关闭；--badcase-status all 可包含）",
            dataset_name, skipped_status,
        )
    return out


def _load_badcase_queries(
    limit: int | None = None,
    status_filter: str | None = None,
) -> list[dict]:
    """Dataset:badcase 回灌（--from-badcase 兼容入口，行为不变）。"""
    return _load_dataset_queries("badcase", limit=limit, status_filter=status_filter)


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


def _extract_strategy(messages) -> str:
    """从工具调用/结果推断查询实际走的通道（策略分层）。

    C=Cube 通道（wrenai_*_query_cube / list_cubes）；A=标准语义管道
    （wrenai_*_get_mdl / describe_schema / run_sql）；B=直连快通道（dbmcp_run_sql）；
    无查询工具 → none。优先级 C > A > B > none。
    """
    seen: set[str] = set()
    for m in messages:
        name = str(getattr(m, "name", "") or "")
        if name:
            seen.add(name)
        for c in getattr(m, "tool_calls", None) or []:
            cname = str(c.get("name", "") or "")
            if cname:
                seen.add(cname)
    if any(("query_cube" in n or "list_cubes" in n) for n in seen):
        return "C"
    if any(
        n.startswith("wrenai_")
        and any(k in n for k in ("run_sql", "get_mdl", "describe_schema"))
        for n in seen
    ):
        return "A"
    if any("dbmcp_run_sql" in n for n in seen):
        return "B"
    return "none"


def _current_trace_refs() -> tuple[str, str]:
    """取当前 handler 最近一次 trace 的 (trace_id, root_observation_id)。

    worker 逐条串行 ainvoke，langfuse CallbackHandler.last_trace_id 在每次 run
    开始时同步更新，故紧跟 invoke 后读取即为该条查询的 trace；root observation 由
    M-T3b 的 _ROOT_OBS_MAP 提供。总开关关闭/无 handler → ("", "")。
    """
    try:
        from agent.trace.langfuse_client import get_langfuse_handler, get_root_observation_id

        handler = get_langfuse_handler()
        trace_id = str(getattr(handler, "last_trace_id", "") or "") if handler else ""
        if not trace_id:
            return "", ""
        return trace_id, get_root_observation_id(trace_id) or ""
    except Exception as e:  # noqa: BLE001
        _logger.warning("[worker] 取 trace ref 失败: %s", e)
        return "", ""


# 参与实验的 Langfuse prompt 名（run 级快照记录各名当前版本号；与 api/experiment.py 的
# _PROMPT_NAMES 同集——label A/B 入口，两套 prompt 版本计数器各自独立）
_PROMPT_VERSION_NAMES = ("main_system_prompt", "nl2sql_system_prompt")


def _run_snapshot(prompt_label: str, semantic: str, skill_ref: str, run_id: str) -> dict:
    """构造 run 级版本快照（worker 执行时锁定「实际生效版本」，全 arm 各条目恒定）。

    字段均为 Langfuse metadata 可存值（str，≤200）：
    - prompt_label / prompt_version_*：label 是该 arm 真实生效的进程级 label；
      prompt_version_<name> 用 get_prompt_version 取该 label 下当前实际版本号
      （Langfuse 各 prompt 独立计数；取不到 → ""）。
    - skill_ref / semantic_ref：该 arm 选择的 git ref（留空 = 磁盘 skill / 语义库 HEAD）。
    """
    from agent.trace.langfuse_client import get_prompt_version

    snap: dict = {
        "run_id": run_id,
        "prompt_label": prompt_label,
        "skill_ref": skill_ref or "(disk)",
        "semantic_ref": semantic or "(head)",
    }
    for pn in _PROMPT_VERSION_NAMES:
        try:
            ver = get_prompt_version(pn, prompt_label)
            snap[f"prompt_version_{pn}"] = str(ver) if isinstance(ver, int) else ""
        except Exception as e:  # noqa: BLE001
            _logger.warning("[worker] 取 prompt %s 版本失败: %s", pn, e)
            snap[f"prompt_version_{pn}"] = ""
    return snap


def _report_run_item(
    client,
    run_name: str,
    item: dict,
    trace_id: str,
    obs_id: str,
    meta: dict,
    run_description: str = "",
    run_meta: dict | None = None,
) -> None:
    """把单条实验结果关联进 Langfuse Dataset Run（run 由首个 run_item 隐式创建）。

    仅当 item 带 dataset_item_id 时生效；--queries 文件的裸查询（无 item_id）跳过，
    并提示该结果不会在 Langfuse Dataset Runs 中出现。

    Langfuse run 级展示（Dataset → Runs 页）：
    - run_description：整轮实验说明（用户提交时填写，所有臂/条目同值）。
    - run_meta：run 级版本快照（实际生效的 prompt label/版本、skill_ref、语义库
      db=ref）。Dataset Run 的 metadata 是「run 级」——每条 create 都会更新 run
      元数据，故必须传**恒定**的 run 级 dict（含 index/question 的逐条 dict 会让
      run 元数据被最后一条覆盖成噪声）。裸查询（run_meta=None）时退化为旧行为
      （逐条 meta），不改原 CLI 语义。
    """
    item_id = str(item.get("dataset_item_id", "") or "")
    if not item_id:
        _logger.debug("[worker] 无 dataset_item_id，跳过 Dataset Run 关联（裸查询集）")
        return
    try:
        client.api.dataset_run_items.create(
            run_name=run_name,
            run_description=run_description or None,
            dataset_item_id=item_id,
            trace_id=trace_id or None,
            observation_id=obs_id or None,
            metadata=run_meta if run_meta is not None else meta,
        )
        _logger.info(
            "[worker] run_item 落库 run=%s item=%s trace=%s%s",
            run_name, item_id[:12], (trace_id or "")[:12],
            f" desc={run_description[:40]!r}" if run_description else "",
        )
    except Exception as e:  # noqa: BLE001
        _logger.warning("[worker] dataset_run_items.create 失败 item=%s: %s", item_id[:12], e)


# ── Langfuse Experiment 页写路径（路径 B：属性注入现有 M-T trace）──
# v4 Experiment 页实体只认带 langfuse.experiment.* OTel 属性的 trace；官方私有
# _propagate_attributes(experiment=...) 把属性注入 OTel context → 本次 trace 的 root span
# 及所有子 span 继承 → 后端聚合出 Experiment 实体。与 Datasets→Runs（dataset_run_items）
# 并行：一条 trace 可同时出现在两个视图。机制已 PoC 验证（见 §9.2 文档）。
_EXPERIMENT_DATASET_CACHE: dict[str, str] = {}


def _experiment_dataset_id(client, dataset_name: str) -> str:
    """Langfuse-managed Dataset id（实验属性要 UUID，名字不行）。按 dataset_name 缓存，失败→空串。"""
    cached = _EXPERIMENT_DATASET_CACHE.get(dataset_name)
    if cached is not None:
        return cached
    ds_id = ""
    try:
        ds = client.api.datasets.get(dataset_name=dataset_name)
        ds_id = str(getattr(ds, "id", "") or "")
    except Exception as e:  # noqa: BLE001
        _logger.warning("[worker] datasets.get(%s) 失败（跳过 Experiment 页）: %s", dataset_name, e)
    _EXPERIMENT_DATASET_CACHE[dataset_name] = ds_id
    return ds_id


def _serialize_experiment_expected(v):
    """序列化 expected_output 为 OTel 属性值（对齐官方 SDK _serialize：str/None 原样，其余 JSON）。"""
    if v is None or isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(v)


@contextmanager
def _experiment_attrs(handler, client, run_name, item, meta, description="", run_meta=None):
    """把 langfuse.experiment.* 属性注入单条查询的 trace（路径 B，与 Dataset Run 并行）。

    - 仅当 handler 可用 + item 带 dataset_item_id + dataset_id 可解析时启用；
      否则 yield None（查询照常跑，只落 Dataset Run）。
    - experiment_id 稳定 = exp-<run_name> → 同一 arm 的所有 item 归并成一个
      Experiment 实体（UI Experiment 页按 run 对比多轮）。
    - run 级（整轮实验、恒定）：
      · description（非空）→ root span 直写 langfuse.experiment.description
        （官方 run_experiment 同款：属性是整 run 共享的人类可读说明，UI 页顶栏展示）；
      · run_meta（版本快照 dict）→ langfuse.experiment.metadata.*（_propagate_attributes
        的 experiment_metadata，所有 item 继承，run 详情可溯源实际版本）。
    - 逐条 item 级：experiment_item_metadata=meta（label/db/index/question…，实验页条目详情）。
    - 根链 on_chain_start 时补 langfuse.experiment.item.root_observation_id=<root span
      自身 id>（官方要求该值必须等于 root spanId；root obs id 只在 span 创建时才知，
      故用 M-T6b 同款 on_chain_start 补丁，逐条装/卸避免跨查询串扰）。
    - item 带 expected_output（goodcase 金标）时，同时在 root span 上写
      langfuse.experiment.item.expected_output（官方 SDK 同款属性；UI Experiment 页
      Expected Output 列只读该属性，不回落 dataset item）。
    - 实验属性注入失败只告警、不阻断查询（退化为纯 Dataset Run）。
    """
    attrs = None
    exp_expected = item.get("expected_output")  # goodcase 金标；None = 不注入
    if handler is not None and run_name:
        item_id = str(item.get("dataset_item_id", "") or "")
        if item_id:
            ds_id = _experiment_dataset_id(client, str(item.get("dataset_name", "") or "badcase"))
            if ds_id:
                attrs = {
                    "experiment_id": f"exp-{run_name}",
                    "experiment_name": run_name,
                    "experiment_dataset_id": ds_id,
                    "experiment_item_id": item_id,
                    "experiment_item_metadata": {
                        "db_name": meta.get("db_name", ""),
                        "label": meta.get("label", ""),
                        "semantic_ref": meta.get("semantic_ref", ""),
                        "skill_ref": meta.get("skill_ref", ""),
                        "model": meta.get("model", ""),
                        "index": meta.get("index", 0),
                        "question": meta.get("question", ""),
                        "run_id": meta.get("run_id", ""),
                    },
                }
                if run_meta:
                    # run 级版本快照（恒定值）；值均 str ≤200，直接进 experiment_metadata
                    attrs["experiment_metadata"] = {str(k): str(v) for k, v in run_meta.items() if v != ""}
    if attrs is None:
        yield None
        return

    from langfuse._client.propagation import _propagate_attributes

    orig_start = handler.on_chain_start
    patched = {"n": 0}

    def _root_backfill(serialized, inputs, *, run_id, parent_run_id=None, tags=None,
                       metadata=None, **kw):
        result = orig_start(serialized, inputs, run_id=run_id, parent_run_id=parent_run_id,
                            tags=tags, metadata=metadata, **kw)
        if parent_run_id is None and not patched["n"]:
            try:
                obs = handler._runs.get(run_id)
                if obs is not None:
                    oid = getattr(obs, "id", "") or ""
                    otel = getattr(obs, "_otel_span", None)
                    if oid and otel is not None:
                        otel.set_attribute("langfuse.experiment.item.root_observation_id", oid)
                        if description:
                            # run 级描述：root span 直写（官方 run_experiment 同款属性）
                            otel.set_attribute("langfuse.experiment.description", description)
                        if exp_expected is not None:
                            otel.set_attribute(
                                "langfuse.experiment.item.expected_output",
                                _serialize_experiment_expected(exp_expected),
                            )
                        patched["n"] += 1
                        _logger.info(
                            "[worker] experiment root_observation_id=%s desc=%s expected_output=%s",
                            oid[:12], "set" if description else "none",
                            "set" if exp_expected is not None else "none",
                        )
            except Exception as e:  # noqa: BLE001
                _logger.debug("[worker] experiment root_observation_id 补齐失败: %s", e)
        return result

    handler.on_chain_start = _root_backfill
    cm = None
    try:
        cm = _propagate_attributes(experiment=attrs)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[worker] 实验属性上下文构造失败，退化为纯 Dataset Run: %s", e)
    if cm is not None:
        try:
            cm.__enter__()
        except Exception as e:  # noqa: BLE001
            _logger.warning("[worker] 实验属性注入失败，退化为纯 Dataset Run: %s", e)
            cm = None
    try:
        yield attrs
    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        handler.on_chain_start = orig_start


def _resolve_effective_model(route: str, model: str) -> str:
    """溯源 label：本次查询实际生效的模型名。

    route/model 显式给定 → 原样返回；空 = 跟随主模型（active provider 默认模型，
    与 ThinkingToggleMiddleware/create_model 的回退链一致）。读不到配置给可读占位。
    仅用于 metadata/日志标注，不参与真实模型选择。
    """
    if model:
        return model
    try:
        from agent.settings.model_config_store import get_store

        store = get_store()
        provs = store.get_all_decrypted()
        active = store.get_active()
        cfg = next((p for p in provs if p.name == route), None) if route else None
        if cfg is None:
            cfg = next((p for p in provs if p.name == active), None) or (provs[0] if provs else None)
        if cfg is not None:
            mid = cfg.default_model
            if not mid:
                for m in cfg.models or []:
                    if isinstance(m, dict) and m.get("id"):
                        mid = str(m["id"])
                        break
            if mid:
                return mid
    except Exception:  # noqa: BLE001 —— 标注失败不影响运行
        pass
    return model or (route if route else "(active 默认)")


def _run_worker(
    label: str,
    queries: list[dict],
    out_path: Path,
    use_judge: bool,
    semantic: str = "",
    run_name: str = "",
    skill_ref: str = "",
    prompt_label: str | None = None,
    cancel_file: str = "",
    description: str = "",
) -> int:
    """单 label（arm）跑完全部查询，写 JSONL。返回 0=成功（含中途停止的部分结果） 1=worker 内部失败。

    prompt_label 显式指定时覆盖 LANGFUSE_PROMPT_LABEL（空串 → 走 production 默认）；
    None（旧调用）→ 沿用 label。skill_ref 非空时注入 SKILLS_REF（skill 版本 A/B）。
    cancel_file 非空时每题前检查：标记存在 → 中止（已做部分仍落盘）；停止判定归 orchestrator
    （worker 仍返回 0），避免退出码语义被取消路径污染。
    description：整轮实验的人类可读说明（实验级单个，所有 arm/条目同值），写入
    Langfuse run 描述（Dataset Run run_description + root span langfuse.experiment.description）。
    """
    # ── import 前注入 label（进程级 A/B 的显式优先项）+ 语义库版本（A/B 语义库）
    #    + skill 版本（A/B skill，git ref 物化）──
    if semantic:
        os.environ["WREN_SEMANTIC_OVERRIDE"] = semantic
        _logger.info("[worker] 语义库 A/B override: %s", semantic)
    if prompt_label is not None:
        if prompt_label:
            os.environ["LANGFUSE_PROMPT_LABEL"] = prompt_label
            _logger.info("[worker] prompt label 覆盖: %s", prompt_label)
        else:
            os.environ.pop("LANGFUSE_PROMPT_LABEL", None)
            _logger.info("[worker] prompt label 置空 → 走 production")
    else:
        os.environ["LANGFUSE_PROMPT_LABEL"] = label
    if skill_ref:
        os.environ["SKILLS_REF"] = skill_ref
        _logger.info("[worker] skill 版本 A/B override: %s", skill_ref)
    # 评审模型也走同供应商；强制 judge 恒真（本 worker 内直接调用，不受采样影响）
    os.environ["NL2SQL_EVAL_JUDGE_SAMPLE"] = "1.0"
    # 队列隔离：本 worker 不把 LLM-judge 任务入 {AGENT_DATA_ROOT}/eval_queue.sqlite、
    # 不起 drainer（该 sqlite 与在线生产共用，多进程 drainer 会 reset 在线在途任务）。
    # 实验的 sql_biz_correct 由 _score_record 同步直评；确定性分同步写实验 trace。
    os.environ["NL2SQL_EVAL_JUDGE_QUEUE"] = "0"
    # 实验 trace 与生产隔离：Environment 属性（UI 一等公民筛选，Environment 列一眼区分）。
    # 官方 run_experiment 用 "sdk-experiment"；这里用更可读的 "experiment"。
    # 只在 worker 进程设置 → 生产后端（不设此变量）trace 保持默认环境。
    os.environ["LANGFUSE_TRACING_ENVIRONMENT"] = "experiment"

    from agent.trace.langfuse_client import resolve_prompt_label, prompt_label_info

    resolved = resolve_prompt_label()
    info = prompt_label_info()
    _logger.info("[worker] label=%s resolved=%s info=%s", label, resolved, json.dumps(info, ensure_ascii=False))

    # ── 顶层 import（MCP 工具在 asyncio 之外加载）──
    from agent.graphs.nl2sql_agent import agent as g

    from langchain_core.messages import HumanMessage

    # 每次 worker 一个 run id，实验 trace 按此分组
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    # run 级版本快照（执行时锁定实际生效版本：prompt label/各 prompt 版本、skill、语义库）
    run_snapshot = _run_snapshot(resolved, semantic, skill_ref, run_id)
    _logger.info(
        "[worker] run 级版本快照: %s",
        json.dumps({k: v for k, v in run_snapshot.items() if k != "run_id"}, ensure_ascii=False),
    )
    records: list[dict] = []

    async def _run_all() -> None:
        for idx, q in enumerate(queries):
            # 运行中「停止」：标记文件已写（API cancel 端点落盘）→ 当前题自然结束后不再开新题。
            # 单题 agent（LLM）无法中断，等它跑完当前题即退出，最坏粒度 = 一题时长。
            if cancel_file and Path(cancel_file).exists():
                _logger.warning(
                    "[worker] 收到停止请求（cancel 标记），中止于第 %d 题（已跑 %d/%d）",
                    idx, idx, len(queries),
                )
                break
            question = str(q.get("question", "")).strip()
            if not question:
                continue
            db_name = str(q.get("db_name", "") or os.getenv("NL2SQL_EVAL_DB", "chinook_aliyun"))
            route = str(q.get("llm_route", "") or _DEFAULT_ROUTE)
            model = str(q.get("llm_model", "") or _DEFAULT_MODEL)
            # enable_thinking：不强制，缺省 None = 跟随主模型默认（无覆盖时节点用
            # 模块级 deepseek_model，deepseek 思考开）；queries 内可逐题 "true"/"false" 覆盖。
            _th_raw = q.get("enable_thinking")
            thinking = None if _th_raw is None else str(_th_raw).lower() in ("true", "1", "yes", "on")
            model_label = _resolve_effective_model(route, model)
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
                # 路径 B：实验属性注入（langfuse.experiment.* → Experiment 页），与 Dataset Run 并行
                from agent.trace.langfuse_client import get_client, get_langfuse_handler

                _meta_pre = {
                    "label": label,
                    "semantic_ref": semantic or "",
                    "skill_ref": skill_ref or "",
                    "db_name": db_name,
                    "model": model_label,
                    "index": idx,
                    "question": question,
                    "run_id": run_id,
                }
                with _experiment_attrs(
                    get_langfuse_handler(), get_client(), run_name, q, _meta_pre,
                    description=description, run_meta=run_snapshot,
                ):
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
                                "enable_thinking": thinking,
                            },
                        },
                    )
                msgs = result.get("messages", []) or []
                sql, result_text = _extract_run_sql(msgs)
                scored = _score_record(question, sql, result_text, use_judge)
                strategy = _extract_strategy(msgs)
                trace_id, obs_id = _current_trace_refs()
                rec["sql"] = sql
                rec["result_head"] = result_text[:200]
                rec["scores"] = scored["scores"]
                rec["reasons"] = scored["reasons"]
                rec["strategy"] = strategy
                rec["trace_id"] = trace_id
                rec["dataset_item_id"] = str(q.get("dataset_item_id", "") or "")
                rec["dataset_name"] = str(q.get("dataset_name", "") or "")
                rec["semantic"] = semantic
                rec["skill_ref"] = skill_ref
                rec["run_name"] = run_name
                _logger.info(
                    "[worker] #%d %s sql=%s exec=%s valid=%.1f strategy=%s",
                    idx, question[:30], (sql or "∅")[:60],
                    scored["scores"].get("sql_exec_success"),
                    scored["scores"].get("sql_valid_score", -1),
                    strategy,
                )
                # 落 Langfuse Dataset Run（Langfuse Experiment 离线落库）
                if run_name and rec["dataset_item_id"]:
                    from agent.trace.langfuse_client import get_client

                    _report_run_item(
                        get_client(), run_name, q, trace_id, obs_id,
                        {
                            "label": label,
                            "semantic_ref": semantic or "",
                            "skill_ref": skill_ref or "",
                            "db_name": db_name,
                            "model": model_label,
                            "strategy": strategy,
                            "index": idx,
                            "question": question,
                            "run_id": run_id,
                        },
                        run_description=description,
                        run_meta=run_snapshot,
                    )
                # 五维分写回 trace（UI 按 trace/run item 查看每维分数）
                if trace_id:
                    from agent.trace.langfuse_client import create_score

                    for dim, val in scored["scores"].items():
                        create_score(
                            dim, float(val),
                            trace_id=trace_id,
                            observation_id=obs_id or None,
                            comment=str(scored["reasons"].get(dim, ""))[:200],
                            metadata={
                                "label": label,
                                "experiment": run_name or "",
                                "semantic_ref": semantic or "",
                                "skill_ref": skill_ref or "",
                            },
                        )
            except Exception as e:  # noqa: BLE001
                _logger.warning("[worker] #%d 查询失败: %s", idx, e)
                rec["reasons"]["fatal"] = f"{type(e).__name__}: {e}"
                rec["scores"]["sql_exec_success"] = 0.0
                rec["scores"]["sql_valid_score"] = 0.0
                rec["scores"]["schema_match_score"] = 0.0
            records.append(rec)

    asyncio.run(_run_all())
    # 确保 run_item / trace 已在进程退出前导出
    try:
        from agent.trace.langfuse_client import get_client

        get_client().flush()
    except Exception:  # noqa: BLE001
        pass
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
    """聚合单 label 结果：每维均值 + 无SQL/执行失败比例 + 策略分层。

    by_strategy：{A, B, C, none} 各子集同口径统计；子集为空 → None。
    策略是查询实际走的通道（_extract_strategy），与实验 arm（prompt/语义库）正交，
    分层用于观察 signal 是否被策略混合摊薄。
    """
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

    strat_map: dict[str, list[dict]] = {}
    for r in records:
        strat_map.setdefault(str(r.get("strategy") or "none"), []).append(r)
    out["by_strategy"] = {}
    for strat in ("A", "B", "C", "none"):
        sub = strat_map.get(strat, [])
        if not sub:
            out["by_strategy"][strat] = None
            continue
        a: dict = {"count": len(sub)}
        for dim in dims:
            vals = [r["scores"][dim] for r in sub if dim in r.get("scores", {})]
            a[dim] = _mean(vals)
        a["no_sql_ratio"] = round(sum(1 for r in sub if not r.get("sql")) / len(sub), 4)
        a["exec_fail_ratio"] = round(
            sum(1 for r in sub if r.get("scores", {}).get("sql_exec_success", 1) == 0) / len(sub), 4,
        )
        out["by_strategy"][strat] = a
    return out


def _compare_gate(ref: dict, cand: dict, threshold: float) -> tuple[bool, list[str]]:
    """回归门禁（双门槛）：核心维 candidate 均值 < reference 均值 - threshold → 失败。

    第一门槛：overall 全部记录；第二门槛：**A 类**（标准语义管道）子集——语义库/prompt
    差异主要作用于 A 类查询，防简单查询（B/C/none）摊薄信号。返回 (pass, 失败原因列表)。
    """
    failures: list[str] = []
    for dim in CORE_DIMS:
        a, b = ref.get(dim), cand.get(dim)
        if a is None or b is None:
            continue
        if b < a - threshold:
            failures.append(f"{dim}: {a} → {b}（掉 {(a - b):.3f} > 阈值 {threshold}）")
    ref_a, cand_a = (ref.get("by_strategy") or {}).get("A"), (cand.get("by_strategy") or {}).get("A")
    if ref_a and cand_a:
        for dim in CORE_DIMS:
            a, b = ref_a.get(dim), cand_a.get(dim)
            if a is None or b is None:
                continue
            if b < a - threshold:
                failures.append(f"[A类] {dim}: {a} → {b}（掉 {(a - b):.3f} > 阈值 {threshold}）")
    return (not failures), failures


def _build_arms(args) -> list[dict]:
    """构造实验 arm 列表（每臂 {name, prompt_label, skill_ref, semantic_ref}）。

    --arms <json 文件> 显式指定（未指定维度走默认：prompt_label=""=production、
    skill_ref=""=磁盘、semantic_ref=""=当前 HEAD）；否则由旧 CLI 的
    --labels + --semantic（按位置）构造（行为逐字节不变）。
    """
    arms_file = getattr(args, "arms", "") or ""
    if arms_file:
        raw = json.loads(Path(arms_file).read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not raw:
            _logger.error("--arms 文件需是 arm 对象数组")
            raise SystemExit(2)
        arms = []
        for i, a in enumerate(raw):
            if not isinstance(a, dict):
                continue
            arms.append(
                {
                    "name": str(a.get("name") or f"arm{i}"),
                    "prompt_label": str(a.get("prompt_label") or ""),
                    "skill_ref": str(a.get("skill_ref") or ""),
                    "semantic_ref": str(a.get("semantic_ref") or ""),
                }
            )
        if not arms:
            _logger.error("--arms 文件为空")
            raise SystemExit(2)
        return arms
    semantics: list[str] = list(args.semantic or [])
    arms = []
    for i, label in enumerate(args.labels):
        sem = semantics[i] if i < len(semantics) else (semantics[-1] if semantics else "")
        arms.append(
            {
                "name": label,
                "prompt_label": label,  # 旧语义：label 即 prompt label
                "skill_ref": "",
                "semantic_ref": sem,
            }
        )
    return arms


def _default_run_name(arm: dict, stamp: str) -> str:
    """Dataset Run 名默认 <arm>：<semantic-ref>[:<skill-ref>]@<stamp>。"""
    sem = arm.get("semantic_ref", "") or ""
    skill = arm.get("skill_ref", "") or ""
    ref = sem.split("=", 1)[1] if "=" in sem else "head"
    base = f"{arm['name']}:{ref}"
    if skill:
        sref = skill.split("=", 1)[1] if "=" in skill else skill
        base += f":{sref}"
    return f"{base}@{stamp}"


def _run_orchestrator(args, on_progress=None, stamp: str | None = None) -> int:
    queries = json.loads(args.queries_path.read_text(encoding="utf-8"))
    if not isinstance(queries, list) or not queries:
        _logger.error("--queries 文件需是查询对象数组")
        return 2

    # ── 逐 arm spawn worker 子进程（隔离进程级 A/B：prompt label × 语义库 ref × skill ref）──
    arms = _build_arms(args)
    stamp = stamp or datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%dT%H%M%S")
    per_arm: dict[str, list[dict]] = {}
    arm_run_name: dict[str, str] = {}

    # 运行中「停止」：标记文件 = <run_dir>/cancel（API cancel 端点落盘），
    # orchestrator 每臂顶部检查——多臂时不启动后续臂，单臂等 worker（每题检查）自然退出。
    cancel_file = _cancel_path(args.out_dir)
    cancelled = False

    if on_progress:
        on_progress({"stage": "running_arms", "total": len(arms), "done": 0, "current": ""})
    for i, arm in enumerate(arms):
        if cancel_file.exists():
            cancelled = True
            _logger.warning("[orchestrator] 检测到停止请求，中止后续臂（已完成 %d/%d）", i, len(arms))
            break
        name = arm["name"]
        run_name = args.run_name or _default_run_name(arm, stamp)
        arm_run_name[name] = run_name
        out_path = Path(args.out_dir) / f"exp_{name}.jsonl"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_SRC), os.environ.get("PYTHONPATH", "")]))
        cmd = [
            sys.executable, "-m", "agent.eval.run_experiment",
            "--worker", "--label", name,
            "--queries", str(args.queries_path),
            "--out", str(out_path),
            "--run-name", run_name,
            "--prompt-label", arm["prompt_label"],  # 空串显式传 → worker 走 production
            "--cancel-file", str(cancel_file),  # 每题前检查停止标记（cooperative cancel）
        ]
        if arm["semantic_ref"]:
            cmd += ["--semantic", arm["semantic_ref"]]
        if arm["skill_ref"]:
            cmd += ["--skill-ref", arm["skill_ref"]]
        # 实验级 Description：整轮一条，透传给每个 arm worker → Langfuse run 描述
        if getattr(args, "description", "") or "":
            cmd += ["--description", str(args.description)]
        if args.judge:
            cmd.append("--judge")
        _logger.info("[orchestrator] spawn: %s", " ".join(cmd[-12:]))
        proc = subprocess.run(cmd, cwd=_PROJECT_ROOT, env=env, timeout=args.timeout)
        if proc.returncode != 0:
            _logger.error("[orchestrator] arm=%s worker 失败 (exit=%d)，中止", name, proc.returncode)
            return 1
        per_arm[name] = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if on_progress:
            on_progress({"stage": "running_arms", "total": len(arms), "done": i + 1, "current": name})

    # ── 聚合展示（含策略分层）──
    _logger.info("")
    for name, records in per_arm.items():
        agg = _aggregate(records)
        _logger.info("[orchestrator] arm=%s %s", name, json.dumps(agg, ensure_ascii=False))
        _logger.info("[orchestrator]   run=%s", arm_run_name[name])

    # ── Dataset Run 级分数（每 arm 一个 run；UI Dataset → Runs 按 run 对比）──
    # 停止后不落 run 级分（半截结果建 Run 实体误导对比），只保留 manifest 部分明细。
    if not cancelled:
        _write_run_scores(args, per_arm, arm_run_name)

    # ── 落盘 manifest（工作区 eval/experiment_runs/）──
    try:
        from agent.workspace_manager import get_workspace_manager

        run_dir = get_workspace_manager().active_workspace / "eval" / "experiment_runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "stamp": stamp,
            "queries": args.queries_path.name,
            "dataset": args.dataset or "",
            "arms": arms,
            "labels": {},
        }
        for l, recs in per_arm.items():
            manifest["labels"][l] = {
                "aggregate": _aggregate(recs),
                "run_name": arm_run_name[l],
            }
        # 门禁结果随 manifest 落盘（arms≥2 且未被停止时；供 API 读回展示）。
        # 停止的 run 缺臂（per_arm 不足）→ 跳过，防 labels[...] KeyError。
        if len(arms) >= 2 and not cancelled and len(per_arm) == len(arms):
            ref_agg = manifest["labels"][arms[0]["name"]]["aggregate"]
            cand_agg = manifest["labels"][arms[1]["name"]]["aggregate"]
            _passed, _failures = _compare_gate(ref_agg, cand_agg, args.threshold)
            manifest["gate"] = {
                "passed": _passed,
                "failures": _failures,
                "ref": arms[0]["name"],
                "cand": arms[1]["name"],
                "threshold": args.threshold,
            }
        (run_dir / f"run_{stamp}.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        _logger.info("[orchestrator] manifest → %s", run_dir / f"run_{stamp}.json")
    except Exception as e:  # noqa: BLE001
        _logger.warning("[orchestrator] manifest 落盘失败: %s", e)

    # ── 停止收尾（部分 manifest 已落盘 → get_run 能读回已完成样本；跳过门禁/分数）──
    if cancelled:
        _logger.info("[orchestrator] 已按停止请求中止，保留部分结果")
        return RC_CANCELLED

    # ── 门禁（ref=arms[0]，cand=arms[1]）──
    if len(arms) < 2:
        _logger.info("单 arm 模式：不对比、不门禁（canary 预检用）。PASS")
        return 0
    ref_name, cand_name = arms[0]["name"], arms[1]["name"]
    ref, cand = _aggregate(per_arm[ref_name]), _aggregate(per_arm[cand_name])
    passed, failures = _compare_gate(ref, cand, args.threshold)
    if passed:
        _logger.info("[orchestrator] ✅ %s vs %s：无回归，门禁通过", ref_name, cand_name)
        return 0
    for f in failures:
        _logger.error("  ❌ %s", f)
    _logger.error("[orchestrator] 回归门禁失败（%s 相对 %s），exit 1", cand_name, ref_name)
    return 1


def _write_run_scores(args, per_label: dict[str, list[dict]], label_run_name: dict[str, str]) -> None:
    """把聚合分数写为 Dataset Run 级 scores（best-effort）。

    run 由 worker 的 dataset_run_items 隐式创建；此处按 run 名读回 id 后写维度分。
    --dataset=all 时试 badcase 再试 goodcase，命中哪个写哪个。
    """
    try:
        from agent.trace.langfuse_client import get_client

        client = get_client()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[orchestrator] run 级分数跳过（client 不可用）: %s", e)
        return
    datasets = ["badcase", "goodcase"] if args.dataset == "all" else [args.dataset or "badcase"]
    for label, records in per_label.items():
        run_name = label_run_name[label]
        run_id = ""
        for ds in datasets:
            try:
                run = client.api.datasets.get_run(dataset_name=ds, run_name=run_name)
                run_id = getattr(run, "id", "") or ""
                if run_id:
                    break
            except Exception:  # noqa: BLE001  run 不存在于该数据集 → 试下一个
                continue
        if not run_id:
            _logger.warning("[orchestrator] run 级分数跳过：找不到 run=%s（无 item 落库？）", run_name)
            continue
        agg = _aggregate(records)
        for dim in (*CORE_DIMS, *AUX_DIMS):
            val = agg.get(dim)
            if val is None:
                continue
            try:
                client.api.scores.create(
                    name=f"experiment:{dim}",
                    value=float(val),
                    dataset_run_id=run_id,
                    metadata={"label": label, "dim": dim},
                )
            except Exception as e:  # noqa: BLE001
                _logger.warning("[orchestrator] run 级分数 %s 落库失败: %s", dim, e)
        _logger.info("[orchestrator] run=%s 写 %d 维 run 级分数", run_name, len((*CORE_DIMS, *AUX_DIMS)))


def _prepare_queries(args) -> Path:
    """装载并落盘查询集（--queries 文件 + --dataset 合并、去重、db_name 归一化）。

    返回查询集 JSON 路径；供 main() 与 experiment API 共用（API 用 SimpleNamespace
    传参，字段：queries/dataset/datasets/dataset_limit/from_badcase/from_badcase_limit/
    badcase_status/out_dir）。datasets 为非空列表时按列表装载任意数据集集合
    （API 多选），否则回退 dataset 单值。空集 → SystemExit(2)。
    """
    merged: list[dict] = []
    if getattr(args, "queries", "") or "":
        merged += json.loads(Path(args.queries).read_text(encoding="utf-8"))
    if getattr(args, "from_badcase", False) and not args.dataset:
        args.dataset = "badcase"
    datasets = getattr(args, "datasets", None)
    if datasets:
        # API 多选：任意 Langfuse 数据集集合（不限于 badcase/goodcase）。
        dlimit = args.dataset_limit or getattr(args, "from_badcase_limit", 0) or None
        for ds_name in datasets:
            if not ds_name:
                continue
            ds_queries = _load_dataset_queries(
                ds_name, dlimit,
                args.badcase_status or None if ds_name == "badcase" else None,
            )
            _logger.info("Dataset:%s 装载 %d 条查询", ds_name, len(ds_queries))
            merged += ds_queries
    elif args.dataset:
        # 兼容旧 --from-badcase-limit；新 --dataset-limit 优先
        dlimit = args.dataset_limit or getattr(args, "from_badcase_limit", 0) or None
        if args.dataset == "all":
            bad = _load_dataset_queries("badcase", dlimit, args.badcase_status or None)
            good = _load_dataset_queries("goodcase", dlimit, None)
            _logger.info(
                "Dataset:all 装载 %d 条（badcase=%d goodcase=%d）",
                len(bad) + len(good), len(bad), len(good),
            )
            merged += bad + good
        else:
            ds_queries = _load_dataset_queries(
                args.dataset, dlimit,
                args.badcase_status or None if args.dataset == "badcase" else None,
            )
            _logger.info("Dataset:%s 装载 %d 条查询", args.dataset, len(ds_queries))
            merged += ds_queries
    merged = _dedupe_queries(merged)
    # db_name 归一化：查询集里的小写变体（chinook_aliyun）或物理库名（chinook）
    # 会因 is_modeled 严格按配置名匹配而被判未建模 → 强制走直连 B，语义库 A/B
    # 不生效。统一归一到 db_config name（大小写不敏感 / 唯一物理库名兜底）。
    try:
        from agent.utils.semantic_db import normalize_db_name

        for _q in merged:
            _orig = _q.get("db_name", "") or ""
            _norm = normalize_db_name(_orig)
            if _norm and _norm != _orig:
                _q["db_name"] = _norm
                _logger.info("[experiment] db_name 归一化 '%s' → '%s'", _orig, _norm)
    except Exception as e:  # noqa: BLE001  归一化失败不影响实验（仅不生效语义 A/B）
        _logger.warning("[experiment] db_name 归一化跳过: %s", e)
    if not merged:
        _logger.error("查询集为空：需 --queries 文件或 --dataset 提供查询")
        raise SystemExit(2)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%dT%H%M%S")
    qpath = out_dir / f"queries_{stamp}.json"
    qpath.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    _logger.info("查询集 %d 条（去重后）→ %s", len(merged), qpath)
    return qpath


# ── 入口 ────────────────────────────────────────────────────

def main() -> None:
    _reconfigure_stdout()
    _load_env()
    parser = argparse.ArgumentParser(description="M5 A/B 实验：离线跑查询集 + 回归门禁")
    parser.add_argument("--worker", action="store_true", help="worker 子进程模式（内部使用）")
    parser.add_argument("--label", default="", help="worker 模式：本进程使用的 prompt label")
    parser.add_argument("--queries", default="", help="查询集 JSON（[{question, db_name?, llm_route?, llm_model?}]）；缺省时若给了 --from-badcase 则用 badcase 集")
    parser.add_argument("--out", default="", help="worker 模式：结果 JSONL 输出路径")
    parser.add_argument("--skill-ref", default="", help="worker 模式：skill 版本 git ref（SKILLS_REF，空=磁盘默认）")
    parser.add_argument("--prompt-label", default=None, help="worker 模式：显式 prompt label（空串=production 默认；缺省沿用 --label）")
    parser.add_argument("--from-badcase", action="store_true", help="（兼容别名）查询集取 Dataset:badcase，等价 --dataset badcase")
    parser.add_argument("--from-badcase-limit", type=int, default=0, help="badcase 回灌条数上限（默认不限）")
    parser.add_argument(
        "--badcase-status", default="",
        help="badcase 状态过滤（逗号分隔，默认 pending,reviewed；传 all 包含全部）",
    )
    parser.add_argument(
        "--dataset", choices=["badcase", "goodcase", "all"], default="",
        help="从 Langfuse Dataset 选查询集（badcase/goodcase/all），可与 --queries 合并",
    )
    parser.add_argument("--dataset-limit", type=int, default=0, help="单数据集装载条数上限（默认不限）")
    parser.add_argument(
        "--semantic", action="append", default=[],
        help="语义库版本覆盖 <db>=<ref>，可重复，按位置对应 --labels（缺省沿用上一个；无则当前 HEAD）",
    )
    parser.add_argument(
        "--run-name", default="",
        help="Dataset Run 名（默认 <arm>:<semantic>[:<skill>]@<stamp>）；同一 arm 所有 item 同 run_name",
    )
    parser.add_argument("--judge", action="store_true", help="追加 sql_biz_correct LLM-judge 打分（慢，控成本）")
    parser.add_argument("--labels", nargs="+", default=["prod-a"], help="参与对比的 label（首个为 reference；--arms 存在时忽略）")
    parser.add_argument(
        "--arms", default="",
        help="结构化 arm 定义 JSON 文件：[{name?, prompt_label?, skill_ref?, semantic_ref?}]，每臂独立指定（缺省维度走默认）",
    )
    parser.add_argument("--threshold", type=float, default=0.05, help="回归门禁阈值（默认 0.05）")
    parser.add_argument("--out-dir", default=str(Path(_PROJECT_ROOT) / ".tmp" / "experiment"), help="结果输出目录")
    parser.add_argument("--timeout", type=int, default=1800, help="单 worker 超时秒（默认 1800）")
    parser.add_argument("--cancel-file", default="", help="worker 模式：停止标记文件路径（每题前检查，存在即中止）")
    parser.add_argument("--description", default="", help="实验级描述（整轮一条，透传所有臂写入 Langfuse run description）")
    args = parser.parse_args()

    if args.worker:
        if not args.label:
            parser.error("--worker 需要 --label")
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
        sys.exit(_run_worker(
            args.label, queries, out_path, args.judge,
            semantic=args.semantic[0] if args.semantic else "",
            run_name=args.run_name,
            skill_ref=args.skill_ref,
            prompt_label=args.prompt_label,
            cancel_file=args.cancel_file,
            description=args.description,
        ))

    # ── 解析查询集：--dataset 选 Langfuse 数据集（可与 --queries 合并、去重、归一化）──
    args.queries_path = _prepare_queries(args)
    sys.exit(_run_orchestrator(args))


if __name__ == "__main__":
    main()
