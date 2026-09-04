# -*- coding: utf-8 -*-
"""评估单元（Evaluation Subject）组装与落盘（评估精准化设计方案 P1 片1 §4）。

一次用户查询（= 一条 chat-turn trace）收尾后组装为一条结构化记录：

    {subject_id, ts, question, db_name, final_sql, exec_status,
     result_summary:{row_count,head_rows(≤10),note}, final_answer,
     report:null|{path,text_head}, task_type, skill_tags}

数据来源（架构：主 agent after_agent 组装 + 工具边界「证据 sidecar」落盘）：
- 主 agent 不直连 run_sql（main_agent.py tools 无 run_sql），产出 SQL 全在 nl2sql
  子 agent 线程执行。唯一把**产出 SQL** 带进主线程的现成通道是 check_async_task
  成功 ToolMessage 内嵌 `sql`（check_progress._build_check_result 的 result["sql"]，
  值即 check_progress._extract_last_sql 结果 → 天然排除探值/核查/dry_run）。
- 但 check 摘要不含数字 row_count/head_rows → 工具调用边界（此时完整 payload 存在）
  把 run_sql/report 证据写盘（`.raw.json` sidecar），收尾时读盘补齐 result_summary。
- sidecar 落盘跨进程安全（主/子可能在 langgraph 不同 worker 进程）。

设计约束：
- 纯逻辑模块：不 import langfuse / langfuse_span（防 import 环）；磁盘根路径由调用方传入。
- 所有副作用旁路：本模块 IO 函数任何异常不外抛（调用方再兜 debug 日志即可）。
- final_sql 判定复用 check_progress._extract_last_sql 语义（惰性 import，勿重复造）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# 组装/证据开关（NL2SQL_EVAL_SUBJECT，默认开；仿 NL2SQL_PROCESS_DATA_DUMP 解析）。
_SUBJECT_ENABLED = (os.getenv("NL2SQL_EVAL_SUBJECT", "1") or "1").strip().lower() not in (
    "0", "false", "no", "off",
)

# sidecar 单条 run_sql 证据保留上限（新者胜，防单次长查询写爆）
_RUNSQL_EVIDENCE_CAP = 50
# report text_head 上限（与 process_data dump 的头部截断口径一致，够 judge 预览）
_REPORT_HEAD_MAX = 2000
# result_summary head_rows 单元格文本上限
_CELL_MAX = 120
# head_rows 保留行数
_HEAD_ROWS_MAX = 10


# ── 消息访问小工具（兼容 langchain 对象与 dict 两形态）──────────

def _msg_role(m: Any) -> str:
    try:
        if isinstance(m, dict):
            return str(m.get("role") or m.get("type") or "")
        return str(getattr(m, "type", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _msg_name(m: Any) -> str:
    try:
        if isinstance(m, dict):
            return str(m.get("name") or "")
        return str(getattr(m, "name", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _msg_content_text(m: Any) -> str:
    """消息 content → 纯文本（content 可能是 str / content-block 列表 / dict）。"""
    try:
        if isinstance(m, dict):
            raw = m.get("content", "")
        else:
            raw = getattr(m, "content", "")
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(raw, list):
        parts = []
        for b in raw:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(str(b.get("text", "")))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    if isinstance(raw, dict):
        # 个别 tool content 已是被序列化的 dict
        return json.dumps(raw, ensure_ascii=False)
    return str(raw or "")


def _parse_json_obj(text: str) -> dict | None:
    """从文本提取首个 JSON 对象（容忍前后缀/围栏）。"""
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _is_system_human(text: str) -> bool:
    t = (text or "").strip()
    return (not t) or t.startswith("[系统") or t.startswith("[系统自动通知]")


# ── 消息辅助（供 compose_subject / 复用）────────────────────

def messages_since_last_human(messages: list) -> list:
    """当前轮消息切片：最后一条「非系统」human/user 之后。

    防「总结一下刚才」类轮次把上一问题的 run_sql/check 混进来；auto-continue
    不新增 human → 切片起点=当前问题（原样保留整轮）。找不到合格 human → 返回
    全量（合成/边界场景兜底）。
    """
    start = 0
    for i, m in enumerate(messages):
        if _msg_role(m) not in ("human", "user"):
            continue
        if _is_system_human(_msg_content_text(m)):
            continue
        start = i + 1
    return list(messages[start:])


def embedded_producing_sql(turn: list) -> str:
    """异步委派路径：逆向扫当前轮 check_async_task 成功消息，取内嵌产出 SQL。

    值由子侧 check_progress._extract_last_sql 产生 → 探值/核查排除语义继承。
    """
    for m in reversed(turn):
        name = _msg_name(m)
        if "check_async_task" not in name:
            continue
        obj = _parse_json_obj(_msg_content_text(m))
        if not obj:
            continue
        if str(obj.get("status", "")) != "success":
            continue
        sql = obj.get("sql") or ""
        if isinstance(sql, str) and sql.strip():
            return sql.strip()
    return ""


def direct_producing_sql(turn: list) -> str:
    """同步兜底：复用 check_progress._extract_last_sql 语义（惰性 import 防环）。"""
    try:
        from agent.subagents.check_progress import _extract_last_sql

        return (_extract_last_sql(list(turn)) or "").strip()
    except Exception as e:  # noqa: BLE001
        _logger.debug("[eval_subject] direct producing sql 失败: %s", e)
        return ""


def extract_final_answer(turn: list) -> str:
    """当前轮最后一条非空 AI 文本（主 agent 回答）。"""
    for m in reversed(turn):
        if _msg_role(m) not in ("ai", "assistant"):
            continue
        text = _msg_content_text(m).strip()
        if text:
            return text
    return ""


def norm_sql(s: str) -> str:
    """归一化 SQL 用于证据匹配：折叠连续空白。"""
    return " ".join((s or "").split())


# ── 证据构造 ──────────────────────────────────────────────

def _cell_text(v: Any) -> str:
    if v is None:
        return ""
    t = str(v)
    return t[:_CELL_MAX] + ("..." if len(t) > _CELL_MAX else "")


def run_sql_evidence_entry(sql: str, ok: bool, error: str, skill: str,
                           payload: Any) -> dict:
    """工具边界 run_sql 证据：保留完整数字载荷（check 摘要里没有的）。"""
    rc, cols, rows = None, [], []
    if isinstance(payload, dict):
        cols = payload.get("columns")
        rows = payload.get("rows")
        rc = payload.get("row_count")
        cols = [str(c) for c in cols] if isinstance(cols, list) else []
        rows = rows if isinstance(rows, list) else []
        if rc is None and rows:
            rc = len(rows)
        if not cols and rows and isinstance(rows[0], dict):
            cols = [str(k) for k in rows[0].keys()]
    if rc is None:
        rc = len(rows)
    head_rows = []
    for r in rows[:_HEAD_ROWS_MAX]:
        if isinstance(r, dict):
            head_rows.append([_cell_text(r.get(c)) for c in cols])
        else:
            head_rows.append([_cell_text(r)])
    return {
        "sql": sql,
        "ok": bool(ok),
        "error": str(error or "")[:500],
        "skill": skill or "",
        "row_count": int(rc) if rc is not None else None,
        "columns": cols,
        "head_rows": head_rows,
        "ts": _now(),
    }


def report_evidence_entry(args: dict) -> dict | None:
    """工具边界报告产物证据（write_file 写 report 时；path + 正文头部）。"""
    if not isinstance(args, dict):
        return None
    path = args.get("path") or args.get("file_path") or ""
    if not path:
        return None
    content = args.get("content") or ""
    if not isinstance(content, str):
        content = str(content)
    return {
        "path": str(path),
        "text_head": content[:_REPORT_HEAD_MAX],
        "ts": _now(),
    }


# ── 组装 ──────────────────────────────────────────────────

def compose_subject(*, context: dict, messages: list, evidence: dict) -> dict | None:
    """把一次查询（消息 + 证据）组为评估单元；无产出 SQL → None（非终结 run / 非查询轮）。

    context keys: subject_id, ts, question, db_name, session_thread_id（透传）。
    """
    turn = messages_since_last_human(messages or [])
    final_sql = embedded_producing_sql(turn) or direct_producing_sql(turn)
    if not final_sql:
        return None
    task_type = "async-subagent" if embedded_producing_sql(turn) else "sync"

    # 证据匹配：按 final_sql 归一化精确匹配，取最后命中（同 SQL 重跑以新证据为准）
    entry = None
    for ev in (evidence or {}).get("run_sqls") or []:
        if ev and norm_sql(ev.get("sql", "")) == norm_sql(final_sql):
            entry = ev

    if entry:
        result_summary = {
            "row_count": entry.get("row_count"),
            "head_rows": entry.get("head_rows") or [],
            "note": "",
        }
        exec_status = {"ok": bool(entry.get("ok")), "error": entry.get("error") or ""}
        skill_tags = [entry.get("skill")] if entry.get("skill") else []
    else:
        # 主线程内嵌 sql 存在但该次执行的数字载荷未落盘（如进程重启丢 sidecar /
        # 重委派错配）→ 结构仍可用，note 标缺失。
        result_summary = {"row_count": None, "head_rows": [], "note": "evidence-missing"}
        exec_status = {"ok": None, "error": ""}
        skill_tags = []

    report = None
    for rep in reversed((evidence or {}).get("reports") or []):
        if rep and rep.get("path"):
            report = {"path": rep["path"], "text_head": rep.get("text_head") or ""}
            break

    subject = {
        "subject_id": context.get("subject_id", ""),
        "ts": context.get("ts") or _now(),
        "question": context.get("question", ""),
        "db_name": context.get("db_name", ""),
        "final_sql": final_sql,
        "exec_status": exec_status,
        "result_summary": result_summary,
        "final_answer": extract_final_answer(turn),
        "report": report,
        "task_type": task_type,
        "skill_tags": skill_tags,
    }
    return subject


# ── 落盘（sidecar 证据 + subject；旁路：异常仅日志）────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def eval_root_dir(root: str, session_thread_id: str) -> Path:
    # S2-2：评估单元落盘移出 agent 可达区。root 现为 data_root（VFS `/`），
    # 落盘到 `<data_root>/eval_runs/{session_thread}/eval-subject/`，对应 VFS
    # `/eval_runs/...`——不在 /shared、/workspace 任何 allow 规则内，agent 的
    # read_file/ls/glob/grep 均被静态层 deny（在线 agent 无论如何摸不到参考答案）。
    # 原先落在 `/workspace/nl2sql_process_data/{session_thread}/eval-subject/`，
    # 与其它 run 的中间产物同区，被 T1 在线 agent 直接读到 final_answer（泄题）。
    return Path(root) / "eval_runs" / session_thread_id / "eval-subject"


def subject_path(root: str, session_thread_id: str, subject_id: str) -> Path:
    return eval_root_dir(root, session_thread_id) / f"{subject_id[:8]}.json"


def evidence_path(root: str, session_thread_id: str, subject_id: str) -> Path:
    return eval_root_dir(root, session_thread_id) / f"{subject_id[:8]}.raw.json"


def append_evidence(root: str, session_thread_id: str, subject_id: str, *,
                    run_sql: dict | None = None, report: dict | None = None) -> None:
    """向 sidecar 追加一条证据（读-改-写；run_sqls cap 50 新者胜）。"""
    if not _SUBJECT_ENABLED or not root or not session_thread_id or not subject_id:
        return
    try:
        with _LOCK:
            p = evidence_path(root, session_thread_id, subject_id)
            ev: dict = {}
            if p.exists():
                try:
                    ev = json.loads(p.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    ev = {}
                if not isinstance(ev, dict):
                    ev = {}
            ev.setdefault("run_sqls", [])
            ev.setdefault("reports", [])
            if run_sql:
                ev["run_sqls"].append(run_sql)
                if len(ev["run_sqls"]) > _RUNSQL_EVIDENCE_CAP:
                    del ev["run_sqls"][: len(ev["run_sqls"]) - _RUNSQL_EVIDENCE_CAP]
            if report:
                ev["reports"].append(report)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(ev, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _logger.debug("[eval_subject] append_evidence 失败: %s", e)


def read_evidence(root: str, session_thread_id: str, subject_id: str) -> dict:
    """读 sidecar；缺失/损坏 → {}。"""
    try:
        p = evidence_path(root, session_thread_id, subject_id)
        if not p.exists():
            return {}
        ev = json.loads(p.read_text(encoding="utf-8"))
        return ev if isinstance(ev, dict) else {}
    except Exception as e:  # noqa: BLE001
        _logger.debug("[eval_subject] read_evidence 失败: %s", e)
        return {}


def write_subject(root: str, session_thread_id: str, subject_id: str,
                  subject: dict) -> None:
    """写评估单元 JSON（同 subject_id 覆盖 = auto-continue 多 run 幂等）。"""
    if not _SUBJECT_ENABLED or not root or not session_thread_id or not subject_id:
        return
    try:
        with _LOCK:
            p = subject_path(root, session_thread_id, subject_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(subject, ensure_ascii=False, default=str), encoding="utf-8",
            )
    except Exception as e:  # noqa: BLE001
        _logger.debug("[eval_subject] write_subject 失败: %s", e)
