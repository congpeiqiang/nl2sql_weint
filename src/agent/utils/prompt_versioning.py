# -*- coding: utf-8 -*-
"""prompt 版本快照（PROMPT_REFS env + 物化）——离线 A/B 实验的 prompt 维度。

三个实验维度里，skill 版本在 git（SKILLS_REF 物化到
<data_root>/offline_experiment/skill_refs/）、语义库在 git（WREN_SEMANTIC_OVERRIDE +
物化），都能精确重放。prompt 版本只在 Langfuse，而
**label 是可变指针**——一旦被重新指向新版本，同一个 label 名就再也回不到旧正文；
`run_experiment._run_snapshot` 记下的 prompt_version_* 只够「事后知道跑的是第几版」，
不能重放（同一份 queries_*.json 重跑拿不到当时的 prompt）。本模块补上这一维度。

预检阶段按 label 把每个 prompt 的版本号 + 正文落到（父目录见 workspace_manager
的 offline_experiment_dir；离线实验的物化缓存统一收在 <data_root>/offline_experiment/
下，run 产物仍在 <active_workspace>/eval/experiment_runs/）：

    <data_root>/offline_experiment/prompt_refs/<safe(label)>@v<main_version>-<版本指纹>/
        manifest.json     # {label, versions: {name: ver}, fingerprint, fetched_at}
        main_system_prompt.txt
        nl2sql_system_prompt.txt
        .prompts_ok       # marker："<label>@<指纹>"

实验 worker 进程由 run_experiment 置 `PROMPT_REFS=<目录名>`，langfuse_client 的
get_prompt_text / get_prompt_version 优先读快照 → 零网络、结果可复现。与 SKILLS_REF
同构：未设置 `PROMPT_REFS` 时本模块全是 no-op，生产与普通实验行为零变化。

为什么目录名带指纹：main_system_prompt 与 nl2sql_system_prompt 是**两个独立的
Langfuse prompt**，版本号各自计数（几乎不可能相等），单一 `v<version>` 描述不了版本
组合。指纹 = 版本映射的 sha1 前 6 位，保证「同组合 → 同目录名（可复用）、异组合 →
异目录名（不会沿用旧快照跑错版本）」。`v<main_version>` 只是给人看的可读前缀。

注意：解析版本必须联网——「label 现在指向哪一版」本质上是在线问题，无法离线回答。
快照的价值在于**解析一次、之后完全离线**：worker 只读快照，marker 快路径避免重复
落盘，manifest 是版本映射的权威来源。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)

PROMPT_REFS_ENV = "PROMPT_REFS"

# 运行时真正从 Langfuse 装配正文的 prompt（唯一权威定义；此前 experiment.py /
# run_experiment.py 各存一份必须手工保持同步的副本，现统一从这里 import）。
# 不含 skill/*：skill 正文来自磁盘/git（SKILLS_REF 覆盖），Langfuse 侧只用于版本号展示。
PROMPT_NAMES = ("main_system_prompt", "nl2sql_system_prompt")

_MARKER = ".prompts_ok"
MANIFEST_NAME = "manifest.json"

# 进程级物化缓存（label → 快照目录或 None）；API 预检与 orchestrator 在同一进程
_prompt_cache: dict[str, Optional[Path]] = {}
_prompt_lock = threading.Lock()
# 每 label 一把锁：并发预检同一 label 时串行，避免互踩同一目录
_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def _safe_ref(ref: str) -> str:
    """目录名安全化（与 skills_versioning / semantic_db 同款写法）。

    比那两处多放行 `@`：目录名形如 `<label>@v12-3f9a1c`，`@` 是 label 与版本的分隔符，
    在 Windows/Linux 文件名里都合法，去掉反而失去可读性。
    """
    return re.sub(r"[^A-Za-z0-9_.\-@]", "_", ref)[:64] or "ref"


def _fingerprint(versions: dict[str, int]) -> str:
    payload = "\n".join(f"{n}={versions[n]}" for n in sorted(versions))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:6]


def _ref_name(label: str, versions: dict[str, int]) -> str:
    return f"{_safe_ref(label)}@v{versions['main_system_prompt']}-{_fingerprint(versions)}"


# ── 物化（预检阶段，联网一次）────────────────────────────────

def _fetch_one(name: str, label: str) -> Optional[tuple[str, int]]:
    """拉一个 prompt 的正文 + 版本号；任一缺失 → None。

    cache_ttl_seconds=0：版本解析取的是**真值**，不能吃 60s 缓存（否则刚打的 tag
    可能解析成旧版本）。max_retries=0：与装配处一致，SDK 内层重试只会放大宕机阻塞。
    """
    from agent.trace import langfuse_client as lf

    try:
        p = lf.get_client().get_prompt(
            name,
            label=label,
            type="text",
            cache_ttl_seconds=0,
            max_retries=0,
            fetch_timeout_seconds=3000,
        )
    except Exception as e:  # noqa: BLE001
        _logger.warning("[prompt_versioning] 拉取 %s(label=%s) 失败: %s", name, label, e)
        return None
    text = getattr(p, "prompt", "") or ""
    ver = getattr(p, "version", None)
    if not text.strip() or not isinstance(ver, int):
        _logger.warning(
            "[prompt_versioning] %s(label=%s) 返回不可用（空正文=%s / version=%r）",
            name, label, not text.strip(), ver,
        )
        return None
    return text, ver


def _load_manifest(dest: Path) -> Optional[dict]:
    """读快照 manifest；marker 缺失 / JSON 坏 / 结构不对 → None（视为不是合法快照）。"""
    try:
        if not (dest / _MARKER).is_file():
            return None
        m = json.loads((dest / MANIFEST_NAME).read_text(encoding="utf-8"))
        if not isinstance(m, dict) or not isinstance(m.get("versions"), dict):
            return None
        return m
    except Exception:  # noqa: BLE001
        return None


def reset_prompt_cache() -> int:
    """清 prompt 版本物化缓存（切工作区用，P1-11）。返回清掉的条数。

    缓存值同样是**工作区内的绝对路径**（`<offline_experiment>/prompt_refs/...`）。
    ⚠️ 与其它缓存一样是 best-effort：正在并发物化的 run 可能在清空之后又写回一条
    旧工作区路径（最坏=退回本次修复前的行为），不做全局停顿。
    """
    with _prompt_lock:
        n = len(_prompt_cache)
        _prompt_cache.clear()
    return n


def materialize_prompt_ref(label: str) -> Optional[Path]:
    """按 label 解析各 prompt 版本 + 正文并落盘，返回快照目录；失败 → None。

    label 空串按 "production" 语义处理——与 worker 一致（run_experiment 对空
    prompt_label 会 pop 掉 LANGFUSE_PROMPT_LABEL，resolve_prompt_label 落到 production）。

    任一 prompt 拿不到版本/正文 → 整体失败（**绝不部分成功**）：否则实验会跑成
    「主 prompt 快照 + 子 prompt 本地兜底」的混合体，A/B 结论不可信。
    """
    label = (label or "").strip() or "production"
    with _prompt_lock:
        if label in _prompt_cache:
            return _prompt_cache[label]

    lock = _key_lock(label)
    with lock:
        with _prompt_lock:
            if label in _prompt_cache:
                return _prompt_cache[label]
        result = _materialize(label)
        with _prompt_lock:
            _prompt_cache[label] = result
        return result


def _materialize(label: str) -> Optional[Path]:
    from agent.trace import langfuse_client as lf
    from agent.workspace_manager import get_workspace_manager

    # 失败要给人话：这两个开关关掉时「无版本可解析」，不是网络问题
    if not lf.langfuse_enabled():
        _logger.warning(
            "[prompt_versioning] LANGFUSE_ENABLE=false（Langfuse 整体关停）→ 无法解析 "
            "prompt 版本，prompt 快照失败"
        )
        return None
    if not lf.prompt_enabled():
        _logger.warning(
            "[prompt_versioning] LANGFUSE_PROMPT_ENABLED=0（强制本地 prompt 文件）→ "
            "无 Langfuse 版本可存，prompt 快照失败"
        )
        return None

    fetched: dict[str, tuple[str, int]] = {}
    for name in PROMPT_NAMES:
        got = _fetch_one(name, label)
        if got is None:
            _logger.warning(
                "[prompt_versioning] prompt %s(label=%s) 解析失败 → 快照整体失败"
                "（label 不存在 / 被改名 / Langfuse 不可达）", name, label,
            )
            return None
        fetched[name] = got

    versions = {n: v for n, (_, v) in fetched.items()}
    key = f"{label}@{_fingerprint(versions)}"
    dest = get_workspace_manager().offline_experiment_dir / "prompt_refs" / _ref_name(label, versions)

    # 快路径：目录已合法且 marker 匹配 → 复用（Langfuse 版本不可变，同版本组合内容恒定，
    # 不需要 skill 那种可变 ref 的新鲜度门）。省的是重复落盘，不是网络——目录名依赖版本号，
    # 名字本身就得先联网解析出来。
    exist = _load_manifest(dest)
    if exist is not None and (dest / _MARKER).read_text(encoding="utf-8").strip() == key:
        _logger.info("[prompt_versioning] 复用已有 prompt 快照 %s（%s）", dest, versions)
        return dest

    try:
        dest.mkdir(parents=True, exist_ok=True)
        for name, (text, _ver) in fetched.items():
            (dest / f"{_safe_ref(name)}.txt").write_text(text, encoding="utf-8")
        (dest / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "label": label,
                    "versions": versions,
                    "fingerprint": _fingerprint(versions),
                    "prompt_names": list(PROMPT_NAMES),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        # marker 最后写：进程中途挂掉时不留「看起来合法」的半成品
        (dest / _MARKER).write_text(key, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _logger.warning("[prompt_versioning] 写快照 %s 失败: %s", dest, e)
        return None

    _logger.info("[prompt_versioning] prompt 快照 → %s（%s）", dest, versions)
    return dest


# ── 读取（worker 侧，零网络）──────────────────────────────────

def prompt_snapshot_dir() -> Optional[Path]:
    """当前进程生效的 prompt 快照目录；PROMPT_REFS 未设 / 目录不合法 → None。

    不缓存：env 是进程级常量，而读 manifest 只是一次小文件 IO（装配期仅 2 次调用）。
    """
    raw = (os.environ.get(PROMPT_REFS_ENV, "") or "").strip()
    if not raw:
        return None
    from agent.workspace_manager import get_workspace_manager

    dest = get_workspace_manager().offline_experiment_dir / "prompt_refs" / _safe_ref(raw)
    return dest if _load_manifest(dest) is not None else None


def read_snapshot_prompt(name: str) -> Optional[tuple[str, Optional[int]]]:
    """从生效快照读某 prompt 的 (正文, 版本号)；未启用 / 不在快照里 / 读失败 → None。"""
    dest = prompt_snapshot_dir()
    if dest is None:
        return None
    m = _load_manifest(dest)
    ver = (m or {}).get("versions", {}).get(name)
    try:
        text = (dest / f"{_safe_ref(name)}.txt").read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _logger.warning("[prompt_versioning] 读快照正文 %s/%s.txt 失败: %s", dest.name, name, e)
        return None
    return text, (ver if isinstance(ver, int) else None)


def snapshot_version(name: str, label: str | None = None) -> Optional[int]:
    """快照里记录的某 prompt 版本号；未启用 / 不在快照里 → None。

    label 非 None 时要求与快照标签一致——防跨臂串味：worker 请求的是别的 label 的
    版本号时，绝不能把本快照的版本当成它的答案（那是错的数据，比 None 更坏）。
    """
    dest = prompt_snapshot_dir()
    if dest is None:
        return None
    m = _load_manifest(dest)
    if m is None:
        return None
    if label is not None and (label or "").strip() != (m.get("label") or ""):
        return None
    ver = (m.get("versions") or {}).get(name)
    return ver if isinstance(ver, int) else None
