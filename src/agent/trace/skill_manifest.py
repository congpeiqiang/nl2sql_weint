# -*- coding: utf-8 -*-
"""Skill manifest 构建：扫描 SKILL.md frontmatter 生成技能清单。

供 M2 监控增强注入 Langfuse trace metadata.skills：
- 记录「本次运行实际注入的技能及版本」，便于事后回溯 skill 版本（M4 版本管理依赖）。
- 启动时扫描一次缓存（SKILL.md 极少变动；改技能后重启服务生效）。

frontmatter 约定（M4 起含 version）：
    ---
    name: nl2sql-sql-generation
    description: "..."
    version: 0.1.0   # 可选，M4 引入
    ---

manifest 结构：
    [{"name": ..., "description": ..., "path": <相对路径>, "version": ...}, ...]
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)

# 技能根目录：src/agent/shared/skills/{main,nl2sql}/*/SKILL.md
_SKILL_GROUPS = ("main", "nl2sql")


def _parse_frontmatter(text: str) -> dict:
    """解析 SKILL.md 的 YAML frontmatter（--- 包裹），容错返回 dict。"""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}
    # 只取第一个 frontmatter 块
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    meta: dict = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip("'\"")
            if val:
                meta[key] = val
    return meta


def build_skill_manifest(skills_root: Optional[Path] = None) -> list[dict]:
    """扫描 skills_root 下 {main,nl2sql}/*/SKILL.md，返回技能清单。"""
    from agent.workspace_manager import get_workspace_manager

    if skills_root is None:
        skills_root = get_workspace_manager().shared_skills_dir
    skills_root = Path(skills_root)

    manifest: list[dict] = []
    for group in _SKILL_GROUPS:
        group_dir = skills_root / group
        if not group_dir.is_dir():
            continue
        for skill_dir in sorted(group_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                meta = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                _logger.debug("[skill_manifest] 读取 %s 失败: %s", skill_md, e)
                continue
            name = meta.get("name") or skill_dir.name
            entry = {
                "name": name,
                "description": meta.get("description", ""),
                # 相对路径：skills/main/nl2sql-sql-generation/，可点回定位磁盘
                "path": str(skill_dir.relative_to(skills_root)).replace("\\", "/"),
            }
            if meta.get("version"):
                entry["version"] = meta["version"]
            manifest.append(entry)
    return manifest


# 启动时扫描缓存一次（多工作区下 shared_skills_dir 固定，跨会话有效）
_manifest_cache: list[dict] = []
_manifest_built = False


def get_skill_manifest() -> list[dict]:
    """返回缓存的技能清单（首次调用构建）。"""
    global _manifest_cache, _manifest_built
    if not _manifest_built:
        try:
            _manifest_cache = build_skill_manifest()
            _logger.info("[skill_manifest] built %d skills", len(_manifest_cache))
        except Exception as e:  # noqa: BLE001
            _logger.warning("[skill_manifest] 构建失败: %s", e)
            _manifest_cache = []
        _manifest_built = True
    return list(_manifest_cache)


# ── M6：Langfuse 版本解析（skill 资产纳入版本管理）────────────────────
# 每个 SKILL.md 已同步为 Langfuse prompt `skill/{group}/{skill_dir}`（sync_prompts --skills）。
# 这里把「本地扫描」的 manifest 升级为「Langfuse 解析版本 + source 标记」：
#   - source=langfuse：该 skill 在 Langfuse 上有 production（或 A/B label）版本 → version 取云端
#   - source=local  ：未同步/拉取失败/404 → version 取本地 frontmatter（运行时仍读本地磁盘兜底）
# 供 trace metadata.skills 注入，事后可重建「本次 run 命中的 skill 版本组合」。


def _build_enriched_manifest() -> list[dict]:
    """本地 manifest + 每 skill 解析 Langfuse 版本。失败整体回退本地 manifest。"""
    base = build_skill_manifest()
    try:
        from agent.trace.langfuse_client import get_prompt_version
    except Exception:  # noqa: BLE001
        get_prompt_version = None
    for entry in base:
        source = "local"
        if get_prompt_version is not None:
            pname = f"skill/{entry['path']}"
            try:
                ver = get_prompt_version(pname)
            except Exception:  # noqa: BLE001
                ver = None
            if ver is not None:
                entry["version"] = ver
                source = "langfuse"
        entry["source"] = source
    return base


_enriched_cache: list[dict] = []
_enriched_built = False


def get_enriched_skill_manifest() -> list[dict]:
    """返回 Langfuse 版本解析后的技能清单（缓存，首次构建）。"""
    global _enriched_cache, _enriched_built
    if not _enriched_built:
        try:
            _enriched_cache = _build_enriched_manifest()
            _logger.info("[skill_manifest] built %d enriched skills", len(_enriched_cache))
        except Exception as e:  # noqa: BLE001
            _logger.warning("[skill_manifest] enriched 构建失败，回退本地: %s", e)
            _enriched_cache = build_skill_manifest()
        _enriched_built = True
    return list(_enriched_cache)
