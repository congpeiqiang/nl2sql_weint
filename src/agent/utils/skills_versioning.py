"""skill 版本化（SKILLS_REF env + git archive 物化）——离线 A/B 实验的 skill 维度。

skill 版本管理在 git（与语义库同模式，见 semantic_db.WREN_SEMANTIC_OVERRIDE）。
实验 worker 进程在 import graph 前设 `SKILLS_REF`，agent 构建（nl2sql_agent /
main_agent 的 SkillsMiddleware sources）时读物化后的 skill 目录，从而让
同查询同 prompt 只换 skill 版本。

取值形态（与 WREN_SEMANTIC_OVERRIDE 一致）：
    <ref>         从仓库内置 skill 目录所在 git 仓库取 ref（默认）
    <path>@<ref>  从显式 path 取 ref（如其他仓库的 skill 集）

未设置 SKILLS_REF / 物化失败 → 原样返回默认 sources（当前磁盘 skill），
保证生产与普通实验的默认行为完全不变。物化目录放
<data_root>/skill_refs/<safe_ref>/（须在 data_root 内，否则 vfs_root_backend
root=data_root 无法解析）；git archive 子树带 rel 前缀（src/agent/shared/skills/…），
物化后提升到 dest_root，使 VFS 路径 /skill_refs/<safe_ref>/{main,nl2sql}/ 直接命中。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Optional

from agent.utils.git_archive import git_archive_materialize

_logger = logging.getLogger(__name__)

SKILLS_REF_ENV = "SKILLS_REF"

# 进程级物化缓存（ref → 物化目录或 None）；worker 单 ref，幂等
_skills_cache: dict[str, Optional[Path]] = {}
_skills_lock = threading.Lock()


def _safe_ref(ref: str) -> str:
    """ref（tag/commit/branch）转目录名安全片段（与 semantic_db 同语义）。"""
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", ref)[:64] or "ref"


def _group_has_skills(root: Path, group: str) -> bool:
    """物化后的技能组目录有效：存在且至少含一个带 SKILL.md 的技能子目录。

    SkillsMiddleware 消费格式 = <source>/<skill-name>/SKILL.md（容器目录下每个
    子目录是一个技能），所以校验的是组目录而非组内单个 SKILL.md。
    """
    g = root / group
    if not g.is_dir():
        return False
    return any((g / s / "SKILL.md").exists() for s in g.iterdir() if s.is_dir())


def _default_skills_base() -> Path:
    """仓库内置 skill 目录：src/agent/shared/skills（在 git，tags 版本化）。"""
    return Path(__file__).resolve().parents[1] / "shared" / "skills"


def _parse_skills_ref() -> tuple[str, str] | None:
    """解析 SKILLS_REF → (src, ref)；src 空串 = 用仓库内置目录。未设置 → None。"""
    raw = os.environ.get(SKILLS_REF_ENV, "").strip()
    if not raw:
        return None
    if "@" in raw:
        src, ref = (s.strip() for s in raw.split("@", 1))
    else:
        src, ref = "", raw
    if not ref:
        return None
    return src, ref


def _flatten_archive_prefix(sub: Path, dest_root: Path) -> None:
    """git archive 子树带 rel 前缀（src/agent/shared/skills/…）→ 把其内容提升到 dest_root。

    dest_root 已存在（物化根）；提升后删除深层空目录，使
    dest_root/{main,nl2sql}/ 直接为 skill 根。个别移动失败不影响（后续校验兜底）。
    """
    if sub == dest_root or not sub.is_dir():
        return
    for child in list(sub.iterdir()):
        target = dest_root / child.name
        if not target.exists():
            try:
                shutil.move(str(child), str(target))
            except Exception as e:  # noqa: BLE001
                _logger.warning("[skills_versioning] 提升 %s 失败: %s", child, e)
    try:
        shutil.rmtree(sub, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def materialize_skills_ref(ref: str, src: str = "") -> Optional[Path]:
    """按 git ref 物化 skill 目录到 <data_root>/skill_refs/<safe_ref>/，进程级缓存。

    Returns:
        物化后的 skill 根目录（含 main/ 与 nl2sql/ 两组）；失败 → None（调用方静默
        退化当前磁盘 skill，不中断实验）。
    """
    key = f"{src}@{ref}"
    with _skills_lock:
        if key in _skills_cache:
            return _skills_cache[key]

    from agent.workspace_manager import get_workspace_manager

    base = Path(src).resolve() if src else _default_skills_base()
    dest_root = get_workspace_manager().data_root / "skill_refs" / _safe_ref(ref)
    # 幂等：纯缓存目录（<data_root>/skill_refs/<ref>），重跑时先清掉旧物化，
    # 否则 _flatten_archive_prefix 遇已存在目录会移动失败、残留下层 src/ 副本
    if dest_root.exists():
        shutil.rmtree(dest_root, ignore_errors=True)
    materialized = git_archive_materialize(base, ref, dest_root)
    result: Optional[Path] = None
    if materialized is not None:
        # 物化落点带 rel 前缀 → 提升；否则已直接落在 dest_root
        if materialized != dest_root:
            _flatten_archive_prefix(materialized, dest_root)
            materialized = dest_root
        # 两组 skill 都要在（main 归主 agent，nl2sql 归子 agent）
        if _group_has_skills(dest_root, "main") and _group_has_skills(dest_root, "nl2sql"):
            result = dest_root
            _logger.info("[skills_versioning] skill 版本化：ref=%s → %s", ref, dest_root)
        else:
            _logger.warning(
                "[skills_versioning] 物化结果缺 main/nl2sql 技能组（每组须含 ≥1 个 SKILL.md）ref=%s → 退化磁盘 skill",
                ref,
            )
    else:
        _logger.warning("[skills_versioning] skill 版本物化失败 ref=%s → 退化磁盘 skill", ref)

    with _skills_lock:
        _skills_cache[key] = result
    return result


def effective_skills_sources(default: list[str], group: str) -> list[str]:
    """SKILLS_REF 设置且物化成功 → 换成物化版 sources；否则原样返回 default。

    Args:
        default: 调用方默认 sources（如 ["/shared/skills/nl2sql/"]）。
        group: 组名（"main" / "nl2sql"），决定 VFS 路径的末段。

    Returns:
        替换后的 sources 列表。默认行为（未设 SKILLS_REF / 物化失败）完全不变。
    """
    spec = _parse_skills_ref()
    if spec is None:
        return default
    src, ref = spec
    dest = materialize_skills_ref(ref, src)
    if dest is None:
        return default
    return [f"/skill_refs/{_safe_ref(ref)}/{group}/"]
