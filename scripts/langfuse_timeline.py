#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Langfuse 实际执行时间线拉取（只读，不产生任何写请求）。

仓库内做 trace/session 事后分析的日常工具：把某条 trace（一次查询/一次子任务）或
某个 session（一次会话，含全部根 trace）的真实执行步骤按时间顺序拉出来——
非 GENERATION 全量时间线 + 工具计数 + read SKILL 证据 + write_todos 步骤演进 +
skill: 标签分布（抓陈旧/启发式 skill 名泄漏，如 knowledge-retrieval/schema-linking）。

用法（在仓库根目录执行）：
    uv run python scripts/langfuse_timeline.py --trace bbb0eda05f3eaac8c0927b34ac1f141c
    uv run python scripts/langfuse_timeline.py --session 01a06f0b-7083-7d13-b219-c94d7d1c224c
    uv run python scripts/langfuse_timeline.py --session <id> --head 60 --roots-only
    uv run python scripts/langfuse_timeline.py --trace <id1> --trace <id2> --env dev --ascii

参数：
    --trace <id>     按 trace id 拉（可重复）。子任务与主线程是不同 trace，分开拉。
    --session <id>   先列会话全部根 trace（含自动续跑产生的多 root），再逐个拉时间线。
    --roots-only     会话模式只列根 trace + 各问题，不展开全量时间线。
    --env {prod,dev} 默认 prod（连生产自托管 192.168.25.64:3010）；dev 走 .env 开发库。
                     若进程环境已显式 export DEPLOY_ENV，优先尊重它（传 --env 则覆盖）。
    --since <ISO>    只拉该时间点之后的 observation（UTC，可选）。
    --head <N>       每条 trace 只打印前 N 行时间线（会话/大 trace 快速预览）。
    --ascii         中文按 \\uXXXX 转义输出（Windows GBK 控制台防乱码）；默认原样 UTF-8。

注意：v4 observations.get_many 分页必须读 meta.cursor（不是 next_cursor）——
仓库旧读路径读 next_cursor，>100 obs 的 trace 被静默截断（曾致「无理解阶段」误判）。
本脚本主拉取带 fields=core,basic,metadata（不带 io，避免整 trace 超大 input/output
拖慢；write_todos 输出与用户问题走独立的窄请求）。服务端 name= 等值过滤可用，
name=contains 会卡住——需要按名字细分时一律用等值 + 客户端过滤。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 自包含：无论从哪执行，都先把仓库 src 加进 sys.path（agent.* 依赖）
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

# ── 分类口径（与分析脚本一致）──────────────────────────────
FETCH_TOOLS = [
    "get_context", "get_instructions", "get_all_knowledge", "recall_queries",
    "describe_schema", "get_mdl", "describe_model", "list_models", "get_db_info",
    "list_stored_queries", "get_data_source", "list_knowledge",
    "list_cubes", "describe_cube", "query_cube",
]
EXEC_TOOLS = ["run_sql", "dry_run", "dry_plan"]
# 启发式兜底 skill 族名（TOOL_SKILL_MAP 的 value）——展示名里出现即「未归到真实 skill」
HEURISTIC_FAMILIES = [
    "knowledge-retrieval", "schema-linking", "recall-queries", "sql-execution",
    "cube-query", "artifact-write", "artifact-read",
]
OLD_SKILL_TOKENS = ["clarification", "knowledge-loader", "schema-linking"]
SKILL_MD = "SKILL.md"
VFS = "vfs_path"
PROCESS_DATA = "nl2sql_process_data"


def _esc(s: str, ascii_out: bool) -> str:
    return json.dumps(s or "", ensure_ascii=ascii_out) if ascii_out else str(s or "")


def _srt(v) -> str:
    try:
        return str(v or "")
    except Exception:
        return ""


def _meta(o) -> dict:
    return getattr(o, "metadata", None) or {}


def _tool_name(o) -> str:
    md = _meta(o)
    return _srt(md.get("tool_name") or md.get("tool") or getattr(o, "name", ""))


def _start(o) -> datetime | None:
    v = getattr(o, "start_time", None)
    return v if isinstance(v, datetime) else None


def _end(o) -> datetime | None:
    v = getattr(o, "end_time", None)
    return v if isinstance(v, datetime) else None


def _dur(o) -> str:
    s, e = _start(o), _end(o)
    if s and e:
        try:
            return f"{(e - s).total_seconds():.1f}s"
        except Exception:
            return ""
    return ""


def _skill_label(raw_name: str) -> str:
    """obs.name 形如 'skill:<skill>:<tool>' → 取中段 skill 标签。"""
    if raw_name.startswith("skill:"):
        parts = raw_name.split(":", 2)
        if len(parts) >= 2:
            return parts[1]
    return ""


def _vfs_tail(vfs: str, ascii_out: bool) -> str:
    """VFS 路径缩写：保留 SKILL / 产物 / 结果文件段，去 workspace 前缀。"""
    v = _srt(vfs)
    if not v:
        return ""
    key = ""
    for marker in ("skills/", "nl2sql_process_data/", "large_tool_results/",
                   "query_result/"):
        if marker in v:
            key = v.split(marker, 1)[1]
            break
    if not key:
        key = v
    if len(key) > 100:
        key = "…" + key[-99:]
    return " vfs=" + _esc(key, ascii_out)


# ── 环境 ──────────────────────────────────────────────────
def _load_env(env_choice: str | None) -> None:
    # 显式传 --env 覆盖；否则尊重已 export 的 DEPLOY_ENV；否则默认 prod
    if env_choice:
        os.environ["DEPLOY_ENV"] = env_choice
    else:
        os.environ.setdefault("DEPLOY_ENV", "prod")
    from agent.settings.env_loader import load_env  # noqa: E402

    load_env()
    if os.environ.get("LANGFUSE_BASE_URL") and not os.environ.get("LANGFUSE_HOST"):
        os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_BASE_URL"]


# ── 拉取（meta.cursor 翻全，修复 next_cursor 截断 bug）────────
def _get_all(api, conds: list[dict], fields: str, since: datetime | None = None) -> list:
    """通用翻全页：conds = filter 条件列表。v4 meta 只有 cursor 字段。"""
    flt = json.dumps(conds)
    rows, cursor = [], None
    while True:
        kw = dict(fields=fields, limit=100, cursor=cursor, filter=flt)
        if since is not None:
            kw["from_start_time"] = since
        resp = api.get_many(**kw)
        rows.extend(list(resp.data or []))
        cursor = getattr(getattr(resp, "meta", None), "cursor", None)
        if not cursor:
            break
    return rows


def _cond_eq(col: str, val) -> dict:
    return {"type": "string", "column": col, "operator": "=", "value": val}


def fetch_trace_obs(api, trace_id: str, since: datetime | None = None) -> list:
    """主时间线拉取：不带 io（整 trace io 含超大 input/output，会拖死）."""
    return _get_all(api, [_cond_eq("traceId", trace_id)],
                    "core,basic,metadata", since)


def fetch_write_todos(api, trace_id: str) -> list:
    """仅 write_todos 的 io（窄过滤 name= 等值，输出是小型 todos JSON）。"""
    return _get_all(api, [
        _cond_eq("traceId", trace_id),
        _cond_eq("name", "write_todos"),
        _cond_eq("type", "TOOL"),
    ], "core,io")


def fetch_session_roots(api, session_id: str) -> list:
    return _get_all(api, [
        _cond_eq("sessionId", session_id),
        {"type": "boolean", "column": "isRootObservation", "operator": "=",
         "value": True},
    ], "core,basic")


# ── 单条 trace 渲染 ───────────────────────────────────────
def _trace_question(api, trace_id: str, ascii_out: bool) -> str:
    """root input 按时间升序，取第一个非[系统 human 文本（窄请求，只拉 root io）。"""
    try:
        from agent.trace.langfuse_v4_reads import (  # noqa: E402
            extract_question_from_input, trace_root_inputs,
        )
        for inp in trace_root_inputs(trace_id):
            q = extract_question_from_input(inp)
            if q:
                return _esc(q[:500], ascii_out)
    except Exception as e:  # noqa: BLE001
        return f"(提取问题失败: {e})"
    return "(root input 未提取到用户问题)"


def _dump_trace(api, trace_id: str, args, header: bool = True) -> None:
    A = args.ascii
    print(f"\n{'='*72}\nTRACE {trace_id}")
    try:
        obs = fetch_trace_obs(api, trace_id, since=args.since)
    except Exception as e:  # noqa: BLE001
        print(f"  !! 拉取失败: {e}")
        return
    print(f"total observations: {len(obs)}")
    if not obs:
        return
    print("question:", _trace_question(api, trace_id, A))

    # root 概要
    roots = [o for o in obs if getattr(o, "is_root_observation", False)]
    for o in roots:
        print(f"  root {_esc(getattr(o,'name',''),A)} @{_srt(_start(o))[5:19]} "
              f"dur={_dur(o)} level={_esc(getattr(o,'level',''),A)} "
              f"session={_esc(getattr(o,'session_id',''),A)}")

    # 1) 时间线
    order = sorted(
        [o for o in obs if _srt(getattr(o, "type", "")) != "GENERATION"],
        key=lambda o: _srt(_start(o) or ""),
    )
    shown = 0
    print("\n-- timeline (non-GENERATION) --")
    for o in order:
        raw = _srt(getattr(o, "name", ""))
        nm = _tool_name(o)
        lbl = _skill_label(raw)
        line = f"  {_srt(_start(o))[5:19]} [{getattr(o,'type','')}] {_esc(nm, A)}"
        if lbl:
            line += f"  ↠skill:{_esc(lbl, A)}"
        line += f"  dur={_dur(o)}"
        lv = _srt(getattr(o, "level", ""))
        if lv in ("ERROR", "WARNING"):
            line += f"  level={lv}"
        vf = _vfs_tail(_srt(_meta(o).get(VFS)), A)
        if vf:
            line += vf
        print(line)
        shown += 1
        if args.head and shown >= args.head:
            print(f"  … (head {args.head} 截断，共 {len(order)} 行)")
            break

    # 2) write_todos 步骤演进（窄请求：只拉 write_todos 的 io）
    try:
        wt_obs = fetch_write_todos(api, trace_id)
    except Exception:  # noqa: BLE001
        wt_obs = []
    if wt_obs:
        print("\n-- write_todos 步骤演进 --")
        for o in sorted(wt_obs, key=lambda x: _srt(_start(x) or "")):
            out = getattr(o, "output", None)
            todos = []
            try:
                data = json.loads(out) if isinstance(out, str) else out
                todos = (data or {}).get("update", {}).get("todos", []) or []
            except Exception:
                todos = []
            head = _srt(_start(o))[5:19]
            if todos:
                print(f"  @{head} → {len(todos)} 项:")
                for t in todos:
                    st = t.get("status") if isinstance(t, dict) else "?"
                    c = t.get("content") if isinstance(t, dict) else t
                    print(f"      [{st}] {_esc(c, A)}")
            else:
                print(f"  @{head}  (output 无 todos)")

    # 3) read SKILL.md 证据
    sk_reads = [
        o for o in obs if "read_file" in _tool_name(o)
        and SKILL_MD in _srt(_meta(o).get(VFS))
    ]
    if sk_reads:
        print(f"\n-- read SKILL.md 痕迹 ({len(sk_reads)}) --")
        for o in sk_reads:
            print(f"  {_srt(_start(o))[5:19]}  {_vfs_tail(_srt(_meta(o).get(VFS)), A)}")

    # 4) 工具计数
    cnt: dict = {}
    for o in obs:
        if _srt(getattr(o, "type", "")) != "TOOL":
            continue
        nm = _tool_name(o)
        for t in FETCH_TOOLS + EXEC_TOOLS:
            if t in nm:
                cnt[t] = cnt.get(t, 0) + 1
    print("\n-- 检索/理解/执行工具计数 --")
    if not cnt:
        print("  (无检索/执行类工具)")
    for t in FETCH_TOOLS + EXEC_TOOLS:
        if cnt.get(t):
            print(f"  {t}: {cnt[t]}")

    # 5) skill: 标签分布 + 非新 skill 泄漏
    label_cnt: dict = {}
    for o in obs:
        lbl = _skill_label(_srt(getattr(o, "name", "")))
        if lbl:
            label_cnt[lbl] = label_cnt.get(lbl, 0) + 1
    print("\n-- skill: 展示标签分布 --")
    if not label_cnt:
        print("  (无 skill: 前缀 span)")
    for lbl, c in sorted(label_cnt.items(), key=lambda x: -x[1]):
        leak = ""
        if not lbl.startswith("nl2sql-"):
            leak = "  << 非新 skill 标签（启发式兜底/遗留），需排查"
        print(f"  {c:>3}  {lbl}{leak}")
    for token in OLD_SKILL_TOKENS:
        hit = [l for l in label_cnt if token in l]
        if hit:
            print(f"  !! 旧 skill 词「{token}」命中: {hit}")


# ── 会话模式 ──────────────────────────────────────────────
def _dump_session(api, session_id: str, args) -> None:
    A = args.ascii
    from agent.trace.langfuse_v4_reads import session_question  # noqa: E402

    print(f"\n{'#'*72}\nSESSION {session_id}")
    q = session_question(session_id)
    print("question:", _esc(q or "(未提取到)", A))

    roots = fetch_session_roots(api, session_id)
    roots.sort(key=lambda o: _srt(_start(o) or ""))
    # 按 trace 归并（auto-continue 同 trace 多 root）
    by_trace: dict = {}
    for o in roots:
        tid = _srt(getattr(o, "trace_id", ""))
        by_trace.setdefault(tid, []).append(o)
    print(f"root observations: {len(roots)}  (traces: {len(by_trace)})")
    for tid, rl in by_trace.items():
        first = rl[0]
        print(f"  - {_srt(_start(first))[5:19]}  {_esc(getattr(first,'name',''),A)} "
              f"[{getattr(first,'type','')}]  trace={tid[:16]}  roots={len(rl)}")
    if args.roots_only:
        return
    for tid in by_trace:
        _dump_trace(api, tid, args, header=False)


def main() -> None:
    ap = argparse.ArgumentParser(description="Langfuse 执行时间线拉取（只读）")
    ap.add_argument("--trace", action="append", default=[], help="trace id（可重复）")
    ap.add_argument("--session", action="append", default=[], help="session id（可重复）")
    ap.add_argument("--roots-only", action="store_true", help="会话模式只列根 trace")
    ap.add_argument("--env", choices=["prod", "dev"], default=None,
                    help="默认 prod；dev 走开发库")
    ap.add_argument("--since", default=None, help="只拉该 ISO 时间之后（UTC）")
    ap.add_argument("--head", type=int, default=0, help="每条 trace 时间线只打前 N 行")
    ap.add_argument("--ascii", action="store_true", help="中文转义输出（GBK 控制台）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    if not args.trace and not args.session:
        ap.error("至少给一个 --trace 或 --session")

    since = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError:
            ap.error(f"--since 无法解析: {args.since}（示例 2026-09-05T00:40:00Z）")

    _load_env(args.env)
    from agent.trace.langfuse_client import get_client  # noqa: E402

    api = get_client().api.observations
    print(f"host: {os.environ.get('LANGFUSE_HOST', os.environ.get('LANGFUSE_BASE_URL', '?'))}")
    print(f"env : DEPLOY_ENV={os.environ.get('DEPLOY_ENV')}")

    for sid in args.session:
        _dump_session(api, sid, args)
    for tid in args.trace:
        _dump_trace(api, tid, args)


if __name__ == "__main__":
    main()
