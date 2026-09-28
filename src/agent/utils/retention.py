# -*- coding: utf-8 -*-
"""各存储的保留策略（retention）+ 磁盘水位（P2-5）。

## 要解决什么

生产盘是有限的，而下面这些**只涨不跌**：`trace_events`（每次 run 的每个事件，含 LLM 载荷）、
`eval_queue` 的终态行、工作区里的 `large_tool_results/`（溢出落盘的大工具结果）、
`conversation_history/`（自动压缩的转储）、`tmp/`（文件工具草稿）、
`nl2sql_process_data/`（每工具调用的 debug dump）。原先只有
`trace_bind_store` 有上限。盘满了的表现是**整站一起坏**（SQLite 写不进、日志写不出、
checkpoint 落盘失败），所以既要有「按龄清理」，也要有「盘要满了先喊一声」。

## 两类目标，默认策略**故意不同**（改代码别一刀切）

- **机器数据**（默认**开**）：`trace_events` / `eval_queue` / `large_tool_results` /
  `conversation_history` / `workspace_tmp` / `nl2sql_process_data` —— 删了不会丢用户能看见的东西。
- **用户可见物**（默认**关**，`0`）：`report`（报告/图表文件，前端还能点开）与 `feedback`
  （反馈与标注，是**金标数据**，也是 Good Set/BadCase 的来源）。
  它们**不是**"忘了配"，而是**要用户点头**：`report` 打开时连同 `report_owner` 归属账本一起清
  （否则删了文件留了账，GET 会变成"有归属但文件不在"）；`feedback` 打开时只按**反馈时间**
  删行，且**默认永不**（不设默认天数 —— 允许删标注这件事本身就是个决定）。

## 约定（与 P2-3/P2-4 一致）

- **`0` = 关闭该目标**；**非数字 → 退回默认**（静默关掉清理比报错危险得多）；负值按关闭算。
- **采不到/不存在的路径**（工作区没建过 `tmp/`、库文件还没生成）→ **跳过**，不报错、不算 0 条。
- **dry-run 绝不写**（只统计"会删多少"）；每个目标独立 try，一个坏不影响其余。
- 清理**不在事件循环上**做（文件遍历 + DELETE 都是同步阻塞，见 P1-14）：跑在后台线程里，
  默认每小时一轮；指标侧只读它缓存下来的尺寸（不在采样轮里遍历目录）。

## 用法

    # 在线：随进程 lifespan 起停（见 api.custom_app._lifespan）
    # 运维：uv run python -m agent.utils.retention --status | --run [--dry-run] | --vacuum
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent.utils.prom_metrics import metered_rlock
from agent.utils.sqlite_paths import store_db_path

_logger = logging.getLogger(__name__)

# 与其余存储同一把计量锁（store="retention"）——这里锁的是**整轮清理**，
# 避免"运维手动 --run"与后台线程同时删同一批文件
_LOCK = metered_rlock("retention")

DEFAULT_INTERVAL_SECONDS = 3600.0
DEFAULT_WORKSPACE_TMP_DAYS = 7.0

# 各目标默认保留天数；0 = 默认关闭
DEFAULT_DAYS: dict[str, float] = {
    "trace_events": 30.0,
    "eval_queue": 30.0,
    "large_tool_results": 30.0,
    "conversation_history": 30.0,
    "workspace_tmp": DEFAULT_WORKSPACE_TMP_DAYS,
    # 每工具调用一份的 debug dump（`middlewares/langfuse_span.py::_dump_process_data`）。
    # ⚠️ 别照抄评估清单里"每份 ≤8MB"的描述去调 `_DUMP_MAX_BYTES`：2026-09-24 实测
    # 生产 3698 个文件里**最大 111KB**，8MB 那个帽子从未生效；真正的风险是**文件数**
    # （3698 个 / 243 个线程目录）与**没人清理**（原先不在本表里）。
    "nl2sql_process_data": 30.0,
    "report": 0.0,
    "feedback": 0.0,
}

_STOP = threading.Event()
_THREAD: Optional[threading.Thread] = None
_THREAD_LOCK = threading.Lock()
_LAST_SIZES: dict[str, int] = {}
_LAST_RUN: dict[str, Any] = {}


def _env_float(name: str, default: float) -> float:
    """读 float 型 env：空/解析不了 → 默认（并记 warning，不静默）。"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        _logger.warning("[retention] %s=%r 不是数字，退回默认 %s", name, raw, default)
        return default


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _days_for(target: str) -> float:
    """该目标的保留天数（<=0 = 关闭）。env 名 = `NL2SQL_RETENTION_DAYS_<TARGET>`。"""
    return _env_float(f"NL2SQL_RETENTION_DAYS_{target.upper()}", DEFAULT_DAYS[target])


def enabled() -> bool:
    """总开关（`NL2SQL_RETENTION_ENABLED`，默认开）。"""
    raw = (os.getenv("NL2SQL_RETENTION_ENABLED") or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def interval_secs() -> float:
    v = _env_float("NL2SQL_RETENTION_INTERVAL_SECS", DEFAULT_INTERVAL_SECONDS)
    return v if v > 0 else DEFAULT_INTERVAL_SECONDS


# ── 路径解析（全部走 workspace manager，不自己拼字符串）──────────


def _data_root() -> Path:
    try:
        from agent.workspace_manager import get_workspace_manager

        return Path(get_workspace_manager().data_root)
    except Exception:  # noqa: BLE001
        return Path(os.getenv("AGENT_DATA_ROOT", "") or ".")


def _trace_db() -> Path:
    try:
        from agent.workspace_manager import get_workspace_manager

        return Path(get_workspace_manager().shared_trace_db)
    except Exception:  # noqa: BLE001
        return _data_root() / "shared" / "trace" / "traces.sqlite"


def _unsafe_root(p: Path) -> bool:
    """工作区根是否是**危险路径**（文件系统根 / 用户家目录）→ 拒绝在其中删东西。

    单工作区后工作区路径来自 `AGENT_DATA_ROOT`（env 或 .env），配错一个字母就可能让
    "工作区的 tmp/ 目录"变成 `/tmp` 或 `~/tmp` —— 删除面完全失控。这里只挡这两种最笨
    的形态（真被指到 `/home` 之类仍是运维问题），成本是一次比较。
    """
    try:
        rp = p.resolve()
    except Exception:  # noqa: BLE001
        return True
    if rp.parent == rp:  # 文件系统根（/ 或 C:\）
        return True
    try:
        return rp == Path.home().resolve()
    except Exception:  # noqa: BLE001
        return False


def _ws_subdirs(name: str) -> list[Path]:
    """工作区下的某个子目录（单工作区 ⇒ 最多一个）；不存在则返回空列表。

    2026-09-25：工作区从「注册表里 N 个」塌成「钉死的一个」
    （`<AGENT_DATA_ROOT>/workspace`，未配 data root 时 `src/agent/workspace`），
    所以这里直接问 `WorkspaceManager`，不再遍历注册表。

    **别改回"扫目录"/返回空**：这是 `tmp/`、`report/` 清理的**唯一**目录来源，
    一旦扫不到就等于保留策略静默失效（磁盘只涨不落）。返回空列表仍是合法语义 ——
    调用方会记 `skipped="无该目录"`。
    """
    try:
        from agent.workspace_manager import get_workspace_manager

        root = Path(get_workspace_manager().active_workspace)
    except Exception as e:  # noqa: BLE001
        _logger.debug("[retention] 解析工作区失败（只用 data_root 兜底）: %s", e)
        root = _data_root()

    if _unsafe_root(root):
        _logger.warning("[retention] 跳过可疑工作区根（根目录/家目录）: %s", root)
        return []
    try:
        d = (root / name).resolve()
    except Exception:  # noqa: BLE001
        return []
    return [d] if d.is_dir() else []


# ── 目标实现 ──────────────────────────────────────────────────


def _purge_trace_events(cutoff: float, dry_run: bool) -> dict[str, Any]:
    """按 `timestamp`（epoch 秒）删事件 + 按 `created_at`（epoch 秒）删会话谱系。"""
    db = _trace_db()
    if not db.exists():
        return {"skipped": "库不存在", "removed": 0}
    conn = sqlite3.connect(str(db), timeout=15.0)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        rows = conn.execute(
            "SELECT COUNT(*) FROM trace_events WHERE timestamp < ?", (cutoff,)
        ).fetchone()
        lineage = conn.execute(
            "SELECT COUNT(*) FROM session_lineage WHERE created_at < ?", (cutoff,)
        ).fetchone()
        n_ev = int(rows[0]) if rows else 0
        n_lg = int(lineage[0]) if lineage else 0
        if dry_run:
            return {"removed": n_ev + n_lg, "detail": f"events={n_ev} lineage={n_lg}"}
        if n_ev:
            conn.execute("DELETE FROM trace_events WHERE timestamp < ?", (cutoff,))
        if n_lg:
            conn.execute("DELETE FROM session_lineage WHERE created_at < ?", (cutoff,))
        conn.commit()
        return {
            "removed": n_ev + n_lg,
            "detail": f"events={n_ev} lineage={n_lg}",
            # 删行**不缩文件**：SQLite 只把页标成可复用（增长停了，盘上数字不动）。
            # 要真缩盘得 `--vacuum`（全库重写，长锁，别在跑着服务时做）。
            "note": "删行不缩文件，需 --vacuum 才回收",
        }
    finally:
        conn.close()


def _purge_eval_queue(cutoff_iso: str, dry_run: bool) -> dict[str, Any]:
    """只删**终态**行（done/failed）；pending/running 一律不动。"""
    db = store_db_path(_data_root(), "eval_queue")
    if not db.exists():
        return {"skipped": "库不存在", "removed": 0}
    conn = sqlite3.connect(str(db), timeout=15.0)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        row = conn.execute(
            "SELECT COUNT(*) FROM eval_queue WHERE state IN ('done','failed')"
            " AND COALESCE(NULLIF(updated_at,''), created_at) < ?",
            (cutoff_iso,),
        ).fetchone()
        n = int(row[0]) if row else 0
        if n and not dry_run:
            conn.execute(
                "DELETE FROM eval_queue WHERE state IN ('done','failed')"
                " AND COALESCE(NULLIF(updated_at,''), created_at) < ?",
                (cutoff_iso,),
            )
            conn.commit()
        return {"removed": n}
    finally:
        conn.close()


def _purge_feedback(cutoff_iso: str, dry_run: bool) -> dict[str, Any]:
    """按反馈时间删 feedback 行（**默认关闭**：这是金标数据的来源）。

    只删 `feedback`（用户打分本身），**不动 `feedback_annotation`**：标注行是人工劳动，
    要清也得人工清（annotate 页有硬删除入口，见 annotate-tab-delete-hard-delete）。
    """
    db = _data_root() / "shared" / "feedback" / "message_feedback.db"
    if not db.exists():
        return {"skipped": "库不存在", "removed": 0}
    conn = sqlite3.connect(str(db), timeout=15.0)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        row = conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE COALESCE(NULLIF(updated_at,''), created_at) < ?",
            (cutoff_iso,),
        ).fetchone()
        n = int(row[0]) if row else 0
        if n and not dry_run:
            conn.execute(
                "DELETE FROM feedback WHERE COALESCE(NULLIF(updated_at,''), created_at) < ?",
                (cutoff_iso,),
            )
            conn.commit()
        return {"removed": n}
    finally:
        conn.close()


def _dir_bytes(d: Path) -> int:
    total = 0
    try:
        # followlinks=False：符号链接本身只算自身大小，绝不越出目录去数别人的盘
        for root, _dirs, files in os.walk(d, followlinks=False):
            for f in files:
                try:
                    p = Path(root) / f
                    if p.is_symlink():
                        continue
                    total += p.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _purge_dir_by_mtime(d: Path, cutoff: float, dry_run: bool) -> dict[str, Any]:
    """按 mtime 清目录里的文件（空目录也顺手删掉；目录本身留着）。

    返回里带 `deleted`（**文件名**列表，非全路径）—— `report/` 要用它去成对清账本。
    """
    n = 0
    size = 0
    deleted: list[str] = []
    try:
        for root, _dirs, files in os.walk(d, followlinks=False):
            for f in files:
                p = Path(root) / f
                try:
                    if p.is_symlink():
                        continue
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime >= cutoff:
                    continue
                n += 1
                size += st.st_size
                if dry_run:
                    continue
                try:
                    p.unlink()
                    deleted.append(f)
                except OSError as e:  # noqa: PERF203  单个文件失败不影响其余
                    _logger.warning("[retention] 删文件失败 %s: %s", p, e)
        if not dry_run:
            # 自底向上删掉空目录（顶层目录保留）
            for root, dirs, files in os.walk(d, topdown=False, followlinks=False):
                if Path(root) == d:
                    continue
                try:
                    if not any(Path(root).iterdir()):
                        Path(root).rmdir()
                except OSError:
                    pass
    except OSError as e:
        return {"removed": n, "freed_bytes": size, "deleted": deleted, "error": str(e)[:200]}
    return {"removed": n, "freed_bytes": size, "deleted": deleted}


def _purge_report(cutoff: float, dry_run: bool) -> dict[str, Any]:
    """报告/图表文件 + `report_owner` 归属账本（**默认关闭**）。

    必须**成对**删：只删文件会留下"有归属但文件不在"的账本行；只删账本则文件变"无主"。
    账本清的是**刚删掉的那几个文件名**（不是"账本里所有不存在于该目录的名字"——
    文件名可能属于别的工作区的 report 目录，按"不存在"去清会误删别人的账）。
    """
    total = 0
    freed = 0
    orphans = 0
    for d in _ws_subdirs("report"):
        res = _purge_dir_by_mtime(d, cutoff, dry_run)
        total += res.get("removed", 0)
        freed += res.get("freed_bytes", 0)
        if dry_run or not res.get("deleted"):
            continue
        try:
            from agent.auth.grants import delete_report_owners

            orphans += delete_report_owners(res["deleted"])
        except Exception as e:  # noqa: BLE001  文件已删、账本没清不致命（读侧本来就 fail-open 到 404）
            _logger.warning("[retention] 清 report_owner 失败: %s", e)
    return {"removed": total, "freed_bytes": freed, "owner_rows": orphans}


def _purge_workspace_dirs(name: str, cutoff: float, dry_run: bool) -> dict[str, Any]:
    removed = 0
    freed = 0
    dirs = _ws_subdirs(name)
    if not dirs:
        return {"skipped": "无该目录", "removed": 0}
    for d in dirs:
        res = _purge_dir_by_mtime(d, cutoff, dry_run)
        removed += res.get("removed", 0)
        freed += res.get("freed_bytes", 0)
    return {"removed": removed, "freed_bytes": freed}


# ── 目标表：名字 → 清理实现（统一签名 `fn(cutoff_ts, dry_run) -> dict`）──


def _targets() -> dict[str, dict[str, Any]]:
    """每个目标：怎么清、量什么、说明（供 --status 输出）。"""

    def evalq(cutoff: float, dry: bool) -> dict[str, Any]:
        return _purge_eval_queue(_iso(cutoff), dry)

    def feedback(cutoff: float, dry: bool) -> dict[str, Any]:
        return _purge_feedback(_iso(cutoff), dry)

    def large(cutoff: float, dry: bool) -> dict[str, Any]:
        return _purge_workspace_dirs("large_tool_results", cutoff, dry)

    def hist(cutoff: float, dry: bool) -> dict[str, Any]:
        return _purge_workspace_dirs("conversation_history", cutoff, dry)

    def tmp(cutoff: float, dry: bool) -> dict[str, Any]:
        return _purge_workspace_dirs("tmp", cutoff, dry)

    def procdata(cutoff: float, dry: bool) -> dict[str, Any]:
        return _purge_workspace_dirs("nl2sql_process_data", cutoff, dry)

    return {
        "trace_events": {
            "fn": _purge_trace_events,
            "desc": "trace_events + session_lineage（按事件时间戳）",
            "kind": "按龄删行",
        },
        "eval_queue": {
            "fn": evalq,
            "desc": "eval_queue 的**终态**行（done/failed；pending/running 不动）",
            "kind": "按龄删行",
        },
        "large_tool_results": {
            "fn": large,
            "desc": "工作区 large_tool_results/（溢出落盘的大工具结果）",
            "kind": "按龄删文件",
        },
        "conversation_history": {
            "fn": hist,
            "desc": "工作区 conversation_history/（自动压缩的转储）",
            "kind": "按龄删文件",
        },
        "workspace_tmp": {
            "fn": tmp,
            "desc": "工作区 tmp/（文件工具草稿）",
            "kind": "按龄删文件",
        },
        "nl2sql_process_data": {
            "fn": procdata,
            "desc": "工作区 nl2sql_process_data/（每工具调用的 debug dump，按 thread/skill 分目录）",
            "kind": "按龄删文件",
        },
        "report": {
            "fn": _purge_report,
            "desc": "报告/图表文件 + report_owner 账本（**默认关闭**：用户可见物）",
            "kind": "按龄删文件",
        },
        "feedback": {
            "fn": feedback,
            "desc": "feedback 行（**默认关闭**：金标数据；标注行不动）",
            "kind": "按龄删行",
        },
    }


def cutoff_days(target: str) -> float:
    return _days_for(target)


def _iso(cutoff_ts: float) -> str:
    """时间戳 → 与库内**同一口径**的 ISO 文本。

    ⚠️ 别写成 `strftime("%Y-%m-%d %H:%M:%S")`（空格分隔）：`eval_queue._now()` /
    `feedback._now_iso()` 用的都是 `datetime.now(timezone.utc).isoformat()`
    （`2026-09-24T13:32:09+00:00`）。SQLite 里比的是**字符串**，空格(0x20) < 'T'(0x54)
    ⇒ 空格口径的 cutoff 在同一天里恒"更小" ⇒ 当天的行全被判成"还没到期"。
    `timespec="seconds"` 是为了和 `eval_queue` 完全同构（feedback 带微秒，差 1 秒无关紧要）。
    """
    return datetime.fromtimestamp(cutoff_ts, timezone.utc).isoformat(timespec="seconds")


# ── 尺寸与磁盘水位 ────────────────────────────────────────────


def _db_bytes(p: Path) -> int:
    """库文件 + -wal/-shm（盘占用要看这三件套）。"""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            f = Path(str(p) + suffix)
            if f.exists():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def measure_areas() -> dict[str, int]:
    """各区域当前占用（字节）。**只有这个函数会遍历目录**，故一米一次。"""
    root = _data_root()
    areas: dict[str, int] = {
        "trace_db": _db_bytes(_trace_db()),
        "eval_queue_db": _db_bytes(store_db_path(root, "eval_queue")),
        "feedback_db": _db_bytes(root / "shared" / "feedback" / "message_feedback.db"),
        "trace_bind_db": _db_bytes(store_db_path(root, "trace_bind")),
        "pending_terminal_db": _db_bytes(store_db_path(root, "pending_terminal")),
        "checkpoint": _db_bytes(root / "shared" / "checkpoint" / "checkpoints.sqlite"),
        "logs": _dir_bytes(root / "logs"),
    }
    for label, sub in (
        ("report", "report"),
        ("large_tool_results", "large_tool_results"),
        ("conversation_history", "conversation_history"),
        ("workspace_tmp", "tmp"),
        ("nl2sql_process_data", "nl2sql_process_data"),
    ):
        total = 0
        for d in _ws_subdirs(sub):
            total += _dir_bytes(d)
        areas[label] = total
    return areas


def disk_status(path: Optional[Path] = None) -> dict[str, Any]:
    """磁盘水位（`shutil.disk_usage`，一次系统调用；采样轮里也便宜）。"""
    p = Path(path or _data_root())
    try:
        usage = shutil.disk_usage(str(p))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200], "path": str(p)}
    total = float(usage.total) or 1.0
    free = float(usage.free)
    return {
        "path": str(p),
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": int(free),
        "used_ratio": round(float(usage.used) / total, 4),
        "free_ratio": round(free / total, 4),
    }


def area_sizes() -> dict[str, int]:
    """上次清理轮缓存下来的各区域尺寸（**不在采样轮里遍历目录**）。"""
    with _LOCK:
        return dict(_LAST_SIZES)


def last_run() -> dict[str, Any]:
    with _LOCK:
        return dict(_LAST_RUN)


# ── 一轮清理 ──────────────────────────────────────────────────


def run_once(dry_run: bool = False) -> dict[str, Any]:
    """跑一轮：逐目标清理 → 重新量尺寸。任何目标出错都不影响其余。"""
    started = time.time()
    out: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "enabled": enabled(),
        "targets": {},
        "removed": 0,
        "freed_bytes": 0,
        "duration_secs": 0.0,
    }
    if not out["enabled"]:
        out["skipped"] = "NL2SQL_RETENTION_ENABLED=0"
        return out

    targets = _targets()
    with _LOCK:
        for name, spec in targets.items():
            days = _days_for(name)
            entry: dict[str, Any] = {"days": days}
            if days <= 0:
                entry["disabled"] = True
                out["targets"][name] = entry
                continue
            cutoff = _now_ts() - days * 86400.0
            try:
                res = spec["fn"](cutoff, dry_run)
                entry.update(res or {})
                out["removed"] += int(entry.get("removed", 0) or 0)
                out["freed_bytes"] += int(entry.get("freed_bytes", 0) or 0)
                if entry.get("removed"):
                    _logger.info(
                        "[retention] %s：%s %d 项（%d 天前%s）",
                        name, "将清" if dry_run else "已清",
                        int(entry["removed"]), int(days), "，dry-run" if dry_run else "",
                    )
            except Exception as e:  # noqa: BLE001  单目标失败不影响其余
                entry["error"] = str(e)[:300]
                _logger.warning("[retention] %s 清理失败: %s", name, str(e)[:200])
            out["targets"][name] = entry

        if not dry_run:
            try:
                sizes = measure_areas()
                global _LAST_SIZES
                _LAST_SIZES = sizes
                out["sizes"] = sizes
            except Exception as e:  # noqa: BLE001
                out["sizes_error"] = str(e)[:200]

        out["disk"] = disk_status()
        out["duration_secs"] = round(time.time() - started, 3)
        global _LAST_RUN
        _LAST_RUN = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dry_run": bool(dry_run),
            "removed": out["removed"],
            "freed_bytes": out["freed_bytes"],
            "duration_secs": out["duration_secs"],
        }
    return out


def status_summary() -> dict[str, Any]:
    """运维/自检用：每个目标的天数与本次是否清理 + 上次清理结果 + 磁盘水位。"""
    targets = {}
    for name in _targets():
        days = _days_for(name)
        targets[name] = {"days": days, "enabled": days > 0, "默认": DEFAULT_DAYS[name]}
    return {
        "enabled": enabled(),
        "interval_secs": interval_secs(),
        "data_root": str(_data_root()),
        "targets": targets,
        "last_run": last_run(),
        "disk": disk_status(),
    }


def vacuum(db: str = "trace") -> dict[str, Any]:
    """回收库文件空间（**只在运维手动调用时做**）。

    自动清理**不** VACUUM：它会把整个库重写一遍（长事务 + 长锁），在跑着服务时做
    会阻塞所有人。删行只让页**可复用**（文件不缩），要真缩盘得停机窗口或接受这几十秒。
    """
    p = _trace_db() if db == "trace" else store_db_path(_data_root(), "eval_queue")
    if not p.exists():
        return {"error": "库不存在", "path": str(p)}
    before = _db_bytes(p)
    conn = sqlite3.connect(str(p), timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = _db_bytes(p)
    return {
        "path": str(p),
        "before_bytes": before,
        "after_bytes": after,
        "freed_bytes": max(0, before - after),
    }


# ── 后台轮询（随 lifespan 起停；同步工作，天然不在事件循环上）──────


def _loop() -> None:
    interval = interval_secs()
    while not _STOP.is_set():
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            _logger.warning("[retention] 清理轮次异常: %s", str(e)[:200])
        _STOP.wait(timeout=interval)


def start_maintenance() -> bool:
    """启动保留策略维护线程（幂等：已在跑返回 False）。进程 lifespan 里调用。"""
    global _THREAD
    with _THREAD_LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return False
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, daemon=True, name="retention-loop")
        _THREAD.start()
        _logger.info(
            "[retention] 维护线程已启动（间隔 %ss，总开关=%s）", interval_secs(), enabled()
        )
        return True


def stop_maintenance(timeout: float = 2.0) -> None:
    """停维护线程（不抛异常、不阻断停机；没跑完的那一轮下次继续）。"""
    _STOP.set()
    with _THREAD_LOCK:
        t = _THREAD
    if t is not None and t.is_alive():
        t.join(timeout=timeout)


# ── CLI ───────────────────────────────────────────────────────


def _cli() -> int:
    ap = argparse.ArgumentParser(description="存储保留策略 + 磁盘水位（P2-5）")
    ap.add_argument("--status", action="store_true", help="各目标天数/上次结果/磁盘水位")
    ap.add_argument("--run", action="store_true", help="立刻清一轮")
    ap.add_argument("--dry-run", action="store_true", help="只统计会删多少（配 --run）")
    ap.add_argument("--vacuum", choices=["trace", "eval"], help="回收库文件空间（运维手动）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.vacuum:
        print(json.dumps(vacuum(args.vacuum), ensure_ascii=False, indent=2))
        return 0
    if args.run:
        print(json.dumps(run_once(dry_run=args.dry_run), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(status_summary(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
