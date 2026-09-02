"""git archive 子树物化（语义库 A/B 与 skill 版本化共用）。

把 base 所在 git 仓库中 ref 版本的子目录物化到 dest_root。base 在 app 主仓库内
（如 src/test/wrenai_exec_Chinook、src/agent/shared/skills）时不能整仓库 worktree
（会把 app 代码也回退），只能 git archive 子树；独立仓库（imdb_project）时
relpath='.'，archive 整树即物化。

原实现在 semantic_db.py（_git_archive_materialize，2026-08 语义库 A/B 落地时引入）；
提取为公开函数供 skill 版本化（skills_versioning.py）复用同一套逻辑。
"""
from __future__ import annotations

import logging
import subprocess
import tarfile
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)


def git_archive_materialize(base: Path, ref: str, dest_root: Path) -> Optional[Path]:
    """把 base 所在 git 仓库中 ref 版本的子目录物化到 dest_root。

    Args:
        base: 物化源目录（git 仓库内，或其子目录；为 '.' 时取 base 本身）。
        ref: git ref（tag / commit / branch）。
        dest_root: 物化输出根目录（tar 解包前自动创建）。

    Returns:
        物化后的项目目录；失败返回 None（调用方负责静默退化）。
    """
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return None
        repo_root = Path(proc.stdout.strip())
        rel = base.resolve().relative_to(repo_root)
        rel_str = "." if str(rel) == "." else str(rel).replace("\\", "/")
        tar_path = dest_root / "_git_archive.tar"
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "archive", ref, rel_str, "-o", str(tar_path)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            _logger.warning(
                "[git_archive] git archive ref=%s rel=%s 失败: %s",
                ref, rel_str, (proc.stderr or "").strip()[:200],
            )
            return None
        try:
            with tarfile.open(tar_path) as tf:
                tf.extractall(dest_root)
        finally:
            tar_path.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[git_archive] 物化异常: %s", e)
        return None
    if rel_str == ".":
        return dest_root
    return dest_root / rel
