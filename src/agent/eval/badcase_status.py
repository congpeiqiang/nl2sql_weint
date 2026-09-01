# -*- coding: utf-8 -*-
"""BadCase 状态标记 → 反馈闭环最后一公里（P0，2026-08-24）。

Langfuse Dataset API 是 append-only（无 update/delete），无法在 item 上标记
「已修复」「已确认无效」等状态。本模块用本地 JSON 文件追踪每条 badcase 的生命周期：

    {workspace}/eval/badcase_status.json

以 source_trace_id 为主键，与 Dataset:badcase 的 source_trace_id 对应。

状态流转：
    pending（新采集）→ reviewed（已复审，仍有效）→ fixed（根因修复）
                                                   → invalid（非真问题）

闭环用法：
    1. collect_badcase 采集新 item → 自动注册 pending
    2. 人工复审 → CLI mark fixed/invalid（或 reviewed 标记仍待修）
    3. run_experiment --from-badcase 默认跳过 fixed/invalid → 回归集只含开放问题
    4. 修复后重跑 → 确认 badcase 不再出现在新采集 → 闭环完成

CLI：
    python -m agent.eval.badcase_status list [--status pending,reviewed]
    python -m agent.eval.badcase_status summary
    python -m agent.eval.badcase_status mark <trace_id> <status> [--note "根因说明"]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_logger = logging.getLogger("badcase_status")

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
try:
    from agent.settings.env_loader import load_env
    load_env()
except Exception:  # noqa: BLE001 独立脚本容错：env 加载失败不阻塞 list/mark
    pass

VALID_STATUSES = ("pending", "reviewed", "fixed", "invalid")
# 默认回归集包含的状态（fixed/invalid 视为已关闭，不回归）
DEFAULT_INCLUDE = ("pending", "reviewed")


def _status_path() -> Path:
    """{active_workspace}/eval/badcase_status.json"""
    from agent.workspace_manager import get_workspace_manager

    return get_workspace_manager().active_workspace / "eval" / "badcase_status.json"


def load_status() -> dict[str, dict]:
    """读取状态文件，返回 {trace_id: entry_dict}。文件不存在/损坏 → 空 dict。"""
    p = _status_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_status(data: dict[str, dict]) -> None:
    """原子写入状态文件。"""
    p = _status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 公共 API（collect_badcase / run_experiment 调用）─────────

def register(
    trace_id: str,
    question: str = "",
    db_name: str = "",
    reasons: list[str] | None = None,
    collected_at: str = "",
) -> bool:
    """注册一条 badcase 为 pending（新采集）。已有状态的条目不覆盖（保留人工判断）。

    返回 True = 新注册，False = 已有状态（未修改）。
    """
    if not trace_id:
        return False
    data = load_status()
    if trace_id in data:
        return False  # 已有状态，保留人工标记
    data[trace_id] = {
        "status": "pending",
        "question": question[:200] if question else "",
        "db_name": db_name,
        "reasons": list(reasons or []),
        "collected_at": collected_at or datetime.now(timezone.utc).date().isoformat(),
        "updated_at": _now_iso(),
        "note": "",
        "bad_type": "",
        "gold_sql": "",
    }
    save_status(data)
    return True


def register_batch(items: list[dict]) -> int:
    """批量注册 badcase 为 pending（collect_badcase 循环结束后一次性调用）。

    每条 item 需含 trace_id；可选 question, db_name, reasons, collected_at。
    已有状态的条目不覆盖。返回新注册条数。单次 IO（load + save），比逐条 register 高效。
    """
    if not items:
        return 0
    data = load_status()
    now = _now_iso()
    today = datetime.now(timezone.utc).date().isoformat()
    new_count = 0
    for it in items:
        tid = it.get("trace_id", "")
        if not tid or tid in data:
            continue
        data[tid] = {
            "status": "pending",
            "question": (it.get("question") or "")[:200],
            "db_name": it.get("db_name", ""),
            "reasons": list(it.get("reasons") or []),
            "collected_at": it.get("collected_at") or today,
            "updated_at": now,
            "note": "",
            "bad_type": it.get("bad_type", "") or "",
            "gold_sql": it.get("gold_sql", "") or "",
        }
        new_count += 1
    if new_count:
        save_status(data)
    return new_count


def is_open(trace_id: str) -> bool:
    """该 trace 是否属于开放状态（pending/reviewed = 应纳入回归集）。
    未注册视为开放（兼容旧数据：stamp 有但 status 文件无的条目，默认回归）。
    """
    data = load_status()
    entry = data.get(trace_id)
    if entry is None:
        return True  # 未注册 = 旧数据，默认包含
    return entry.get("status", "pending") in DEFAULT_INCLUDE


def _resolve_trace_id(data: dict, partial: str) -> str:
    """trace_id 前缀匹配：完整 ID 精确命中 → 直接返回；否则找唯一前缀匹配。
    无匹配返回 partial 本身（调用方按新条目处理）。多匹配 → ValueError。
    """
    if partial in data:
        return partial
    matches = [tid for tid in data if tid.startswith(partial)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"trace_id 前缀 {partial[:16]} 匹配到 {len(matches)} 条，请用更长前缀"
        )
    return partial


def mark(trace_id: str, status: str, note: str = "") -> bool:
    """标记一条 badcase 的状态。支持 trace_id 前缀匹配（≥8 字符即可唯一）。
    完全不存在于状态文件 → 自动创建（status+note）。
    返回 True = 成功，False = status 非法或前缀歧义。
    """
    if status not in VALID_STATUSES:
        _logger.error("无效状态 %s（可选: %s）", status, ", ".join(VALID_STATUSES))
        return False
    data = load_status()
    try:
        resolved = _resolve_trace_id(data, trace_id)
    except ValueError as e:
        _logger.error("%s", e)
        return False
    if resolved not in data:
        data[resolved] = {
            "status": status,
            "question": "",
            "db_name": "",
            "reasons": [],
            "collected_at": "",
            "updated_at": _now_iso(),
            "note": note,
            "bad_type": "",
            "gold_sql": "",
        }
    else:
        prev = data[resolved].get("status", "pending")
        data[resolved]["status"] = status
        data[resolved]["updated_at"] = _now_iso()
        if note:
            data[resolved]["note"] = note
        _logger.info("  %s: %s → %s", resolved[:16], prev, status)
    save_status(data)
    return True


def annotate(
    trace_id: str,
    bad_type: str,
    gold_sql: str = "",
    note: str = "",
    question: str = "",
    db_name: str = "",
) -> bool:
    """标注页确认一条 badcase：状态置 reviewed（人工确认有效，进回归集）+ bad_type。

    与 mark 的区别：mark 只改状态；annotate 额外写入错误类型与人工金标 SQL，
    供 run_experiment 按 bad_type 分组、与 gold_sql exact-match 回归。
    """
    from agent.eval.bad_types import BAD_TYPE_KEYS, is_valid_bad_type

    if bad_type and not is_valid_bad_type(bad_type):
        _logger.error("无效 bad_type=%s（可选: %s）", bad_type, ", ".join(BAD_TYPE_KEYS))
        return False
    data = load_status()
    try:
        resolved = _resolve_trace_id(data, trace_id)
    except ValueError as e:
        _logger.error("%s", e)
        return False
    if resolved not in data:
        data[resolved] = {
            "status": "reviewed",
            "question": question[:200] if question else "",
            "db_name": db_name,
            "reasons": ["user_feedback=0", "manual_annotation"],
            "collected_at": datetime.now(timezone.utc).date().isoformat(),
            "updated_at": _now_iso(),
            "note": note,
            "bad_type": bad_type,
            "gold_sql": gold_sql,
        }
    else:
        entry = data[resolved]
        prev = entry.get("status", "pending")
        entry["status"] = "reviewed"  # 人工确认有效，进回归集
        entry["updated_at"] = _now_iso()
        entry["bad_type"] = bad_type
        if gold_sql:
            entry["gold_sql"] = gold_sql
        if note:
            entry["note"] = note
        if question and not entry.get("question"):
            entry["question"] = question[:200]
        _logger.info("  %s: %s → reviewed（bad_type=%s）", resolved[:16], prev, bad_type or "?")
    save_status(data)
    return True


# ── CLI ──────────────────────────────────────────────────────

def _cmd_list(args) -> int:
    data = load_status()
    if not data:
        print("状态文件为空（尚未采集 badcase）")
        return 0
    statuses = set(args.status.split(",")) if args.status else set(VALID_STATUSES)
    items = [
        (tid, entry)
        for tid, entry in data.items()
        if entry.get("status", "pending") in statuses
    ]
    items.sort(key=lambda x: x[1].get("updated_at", ""), reverse=True)
    print(f"共 {len(items)} 条（过滤: {','.join(sorted(statuses))}）\n")
    for tid, e in items:
        q = e.get("question", "")[:40] or "(无问题)"
        reasons = ", ".join(e.get("reasons", [])[:2])
        note = e.get("note", "")[:30]
        updated = e.get("updated_at", "")[:19]
        print(f"  [{e.get('status','?'):8s}] {tid[:16]}  {updated}  {q}")
        if reasons:
            print(f"             reasons: {reasons}")
        if note:
            print(f"             note: {note}")
    return 0


def _cmd_summary(args) -> int:
    data = load_status()
    if not data:
        print("状态文件为空（尚未采集 badcase）")
        return 0
    counts: dict[str, int] = {}
    for entry in data.values():
        s = entry.get("status", "pending")
        counts[s] = counts.get(s, 0) + 1
    total = sum(counts.values())
    print(f"BadCase 状态汇总（共 {total} 条）\n")
    for s in VALID_STATUSES:
        c = counts.get(s, 0)
        bar = "█" * c + "░" * (total - c)
        tag = "开放（回归集）" if s in DEFAULT_INCLUDE else "已关闭"
        print(f"  {s:10s}  {c:4d}  {bar[:20]}  {tag}")
    open_count = sum(counts.get(s, 0) for s in DEFAULT_INCLUDE)
    print(f"\n  回归集规模: {open_count}/{total}")

    # 优化③：按错误类型分布（人工标注后生效，未标注显示 N/A）
    by_type: dict[str, int] = {}
    for entry in data.values():
        bt = entry.get("bad_type", "")
        if bt:
            by_type[bt] = by_type.get(bt, 0) + 1
    if by_type:
        print("\n  错误类型分布:")
        from agent.eval.bad_types import BAD_TYPE_LABELS

        for bt, c in sorted(by_type.items(), key=lambda kv: -kv[1]):
            label = BAD_TYPE_LABELS.get(bt, bt)
            print(f"    {label:12s} ({bt})  {c}")
    return 0


def _cmd_mark(args) -> int:
    ok = mark(args.trace_id, args.status, note=args.note or "")
    if ok:
        print(f"✅ {args.trace_id[:16]} → {args.status}" + (f"  note: {args.note}" if args.note else ""))
    return 0 if ok else 1


def _cmd_review(args) -> int:
    """交互式逐条复审 pending badcase：自动打开 Langfuse trace，等你输入判断。

    操作键：
      f  → fixed（已修复）
      i  → invalid（非真问题）
      r  → reviewed（确认是问题，待修）
      s  → skip（跳过，下次再看）
      q  → quit（退出）
    """
    import webbrowser

    langfuse_host = os.environ.get("LANGFUSE_BASE_URL", "") or os.environ.get("LANGFUSE_HOST", "")
    project_id = os.environ.get("LANGFUSE_PROJECT_ID", "")

    data = load_status()
    pending = [
        (tid, entry)
        for tid, entry in data.items()
        if entry.get("status", "pending") in DEFAULT_INCLUDE
    ]
    pending.sort(key=lambda x: x[1].get("updated_at", ""), reverse=True)

    if not pending:
        print("没有待处理的 badcase 🎉")
        return 0

    print(f"共 {len(pending)} 条待处理（f=修好 i=误报 r=确认待修 s=跳过 q=退出）\n")
    marked = 0
    for idx, (tid, entry) in enumerate(pending, 1):
        q = entry.get("question", "") or "(无问题)"
        reasons = ", ".join(entry.get("reasons", []))
        db = entry.get("db_name", "")
        print(f"─── [{idx}/{len(pending)}] {tid[:16]} ───")
        print(f"  问题: {q}")
        print(f"  原因: {reasons}")
        if db:
            print(f"  数据库: {db}")

        # 打开 Langfuse trace
        if langfuse_host and project_id:
            url = f"{langfuse_host}/project/{project_id}/traces/{tid}"
            print(f"  🔗 {url}")
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass

        try:
            action = input("  操作 [f/i/r/s/q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            break
        if action == "q":
            break
        if action == "s":
            print("  ⏭ 跳过")
            continue
        status_map = {"f": "fixed", "i": "invalid", "r": "reviewed"}
        new_status = status_map.get(action)
        if not new_status:
            print("  ❓ 无效输入，跳过")
            continue
        note = ""
        if action in ("f", "i"):
            try:
                note = input("  说明（可选，回车跳过）: ").strip()
            except (EOFError, KeyboardInterrupt):
                pass
        if mark(tid, new_status, note=note):
            print(f"  ✅ → {new_status}" + (f"  note: {note}" if note else ""))
            marked += 1
        print()

    print(f"\n完成：标记 {marked} 条")
    return 0


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(
        description="BadCase 状态标记（pending/reviewed/fixed/invalid）",
    )
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="列出 badcase 状态")
    p_list.add_argument(
        "--status", default="",
        help="按状态过滤，逗号分隔（默认全部；如 pending,reviewed）",
    )

    sub.add_parser("summary", help="状态分布汇总")
    sub.add_parser("review", help="交互式逐条复审 pending badcase（自动打开 Langfuse trace）")

    p_mark = sub.add_parser("mark", help="标记状态")
    p_mark.add_argument("trace_id", help="目标 trace_id（支持前缀匹配，至少 8 字符即可唯一定位）")
    p_mark.add_argument("status", choices=VALID_STATUSES, help="目标状态")
    p_mark.add_argument("--note", default="", help="标记说明（可选）")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch = {"list": _cmd_list, "summary": _cmd_summary, "mark": _cmd_mark, "review": _cmd_review}
    sys.exit(dispatch[args.command](args))


if __name__ == "__main__":
    main()
