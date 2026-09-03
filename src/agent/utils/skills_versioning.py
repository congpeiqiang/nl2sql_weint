"""skill 版本化（SKILLS_REF env + 物化）——离线 A/B 实验的 skill 维度。

skill 版本管理在 git。实验 worker 进程在 import graph 前设 `SKILLS_REF`，agent 构建
（nl2sql_agent / main_agent 的 SkillsMiddleware sources）时读物化后的 skill 目录，从而让
同查询同 prompt 只换 skill 版本。

取值形态（与 WREN_SEMANTIC_OVERRIDE 一致）：
    <ref>         从仓库内置 skill 目录所在 git 仓库取 ref（默认）
    <path>@<ref>  从显式 path 取 ref（如其他仓库的 skill 集）

物化源分两种情况：
- **开发机**：代码在 nl2sql git 检出内 → 直接 `git archive` 本地 ref（离线可用）。
- **生产**：发版 tar `--exclude=.git`，容器内整个代码目录无 `.git`（2026-09 实测）
  → 下拉恒空、物化静默退化。解法：skill 版本从远程取——列出用 `git ls-remote`
  直读 origin（TTL 缓存，推 tag 后 ≤30s 出现，无需服务器手工 fetch）；物化用
  `git clone --depth 1 --branch <ref>` 浅克隆到临时目录后拷出 skills 子树。
  远程 origin 不硬编码进代码/仓库（防 github 公开镜像泄露内网）：优先读环境变量
  `SKILLS_GIT_REMOTE`（生产在服务端 `.env.prod` 注入），否则取 enclosing repo 的
  `origin` remote（开发机自动拿到 gitlab 地址）。

tag 命名硬约定（下拉只认这些）：`skills/` 或 `skills-` 前缀——排除语义执行版
`v1.0.0~v7.0.0` 等非 skill tag。commit + 打前缀 tag + 推送后，下拉自动出现。

未设置 SKILLS_REF → 原样返回默认 sources（当前磁盘 skill），生产与普通实验默认行为
不变。物化目录放 <data_root>/skill_refs/<safe_ref>/（须在 data_root 内，否则
vfs_root_backend root=data_root 无法解析）。物化成功写 `.skills_ok` marker
（记录 `src@ref`）——同 ref 二次物化（如 API 预检后各 worker 子进程）直接复用目录，
零网络。物化失败 → None：API 层在 run 预检显式报错，不再静默退化跑默认 skill。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from agent.utils import git_repo
from agent.utils.git_archive import git_archive_materialize
from agent.utils.git_repo import clone_shallow

_logger = logging.getLogger(__name__)

SKILLS_REF_ENV = "SKILLS_REF"

# 远程列出 TTL：推 tag 后 ≤30s 出现在下拉（GitLab 为真源，不用服务器手工 fetch）
_REMOTE_TTL = 30.0
# 可变 ref（branch / 非 skills- 前缀 tag）物化超时后需重物化；skills 前缀 tag 不可变，
# 只要 marker 匹配即永久复用。
_REFRESH_MAX_AGE = 3600.0
_MARKER = ".skills_ok"

# 进程级物化缓存（ref → 物化目录或 None）；worker 单 ref，幂等
_skills_cache: dict[str, Optional[Path]] = {}
_skills_lock = threading.Lock()
# 每 key 一把锁：同进程并发 run 同时物化同一新 tag 时不互踩同一 work 目录
_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())
# 进程级 ls-remote 缓存（origin → (monotonic 时间戳, {tags, branches})），并发不重复打网络
_remote_cache: dict[str, tuple[float, dict]] = {}
_remote_lock = threading.Lock()


def tag_like_skill(name: str) -> bool:
    """skill 版本 tag 命名硬约定：skills/ 或 skills- 前缀（排除语义执行版 v1~v7 等）。"""
    name = name or ""
    return name.startswith("skills/") or name.startswith("skills-")


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
    """仓库内置 skill 目录：src/agent/shared/skills（dev 在 git、tags 版本化；生产无 .git）。"""
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


def enclosing_repo(base: Path) -> tuple[str, str] | None:
    """base 所在 git 仓库的 (repo_root, rel)；非 git 目录（生产容器）→ None。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        root = proc.stdout.strip()
        if not root:
            return None
        rel = os.path.relpath(str(base), root).replace("\\", "/")
        return root, rel
    except Exception:  # noqa: BLE001
        return None


def effective_origin(repo_root: str | None = None) -> str:
    """skill 远程源 URL。优先级：env SKILLS_GIT_REMOTE > enclosing repo 的 origin remote。

    返回空串 = 无远程源（未配置 + 无本地仓库）。不硬编码内网 gitlab 地址进代码。
    """
    env_origin = os.environ.get("SKILLS_GIT_REMOTE", "").strip()
    if env_origin:
        return env_origin
    if repo_root:
        try:
            ok, out = git_repo._run(["remote", "get-url", "origin"], cwd=repo_root, timeout=15)
            if ok and out.strip():
                return out.strip()
        except Exception:  # noqa: BLE001
            pass
    return ""


def remote_refs(origin: str, *, force: bool = False) -> Optional[dict]:
    """`git ls-remote` 直读远程 tags/branches（进程级 TTL 缓存 _REMOTE_TTL）。

    Returns:
        {"tags": [...], "branches": [...]}（排序、注解 tag 已剥）；origin 为空或
        gitlab 不可达 → 返回最近一次成功缓存；无缓存 → None。
    """
    origin = (origin or "").strip()
    if not origin:
        return None
    now = time.monotonic()
    with _remote_lock:
        hit = _remote_cache.get(origin)
        if hit and not force and now - hit[0] < _REMOTE_TTL:
            return hit[1]
    ok, out = git_repo._run(["ls-remote", "--refs", "--tags", "--heads", origin], timeout=20)
    if not ok:
        _logger.warning("[skills_versioning] ls-remote %s 失败: %s", origin, (out or "")[:200])
        with _remote_lock:
            hit = _remote_cache.get(origin)
            return hit[1] if hit else None
    tags: list[str] = []
    branches: list[str] = []
    for ln in out.splitlines():
        _sha, _, ref = ln.partition("\t")
        if ref.startswith("refs/tags/"):
            name = ref[len("refs/tags/"):]
            if not name.endswith("^{}"):  # 剥注解 tag 的 peeled 重复行
                tags.append(name)
        elif ref.startswith("refs/heads/"):
            branches.append(ref[len("refs/heads/"):])
    result = {"tags": sorted(tags), "branches": sorted(branches)}
    with _remote_lock:
        _remote_cache[origin] = (now, result)
    return result


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


def _dest_valid(dest_root: Path) -> bool:
    """物化目录合法：main/ 与 nl2sql/ 两组都在且各含 ≥1 个 SKILL.md。"""
    return _group_has_skills(dest_root, "main") and _group_has_skills(dest_root, "nl2sql")


def _marker_ok(dest_root: Path, key: str) -> bool:
    """marker 内容与本 key（src@ref）一致 → 该目录就是本 ref 物化的。"""
    try:
        p = dest_root / _MARKER
        return p.is_file() and p.read_text(encoding="utf-8").strip() == key
    except Exception:  # noqa: BLE001
        return False


def _write_marker(dest_root: Path, key: str) -> None:
    try:
        (dest_root / _MARKER).write_text(key, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _logger.warning("[skills_versioning] 写 marker 失败: %s", e)


def _marker_fresh(dest_root: Path) -> bool:
    """可变 ref（branch）物化是否仍新鲜（< _REFRESH_MAX_AGE）。"""
    try:
        return (dest_root / _MARKER).stat().st_mtime > time.time() - _REFRESH_MAX_AGE
    except Exception:  # noqa: BLE001
        return False


def _materialize_local(base: Path, ref: str, dest_root: Path, key: str) -> Optional[Path]:
    """从 base 所在 git 仓库 `git archive` 物化（dev 本地检出路径，离线可用）。"""
    if dest_root.exists():
        shutil.rmtree(dest_root, ignore_errors=True)
    dest_root.mkdir(parents=True, exist_ok=True)
    materialized = git_archive_materialize(base, ref, dest_root)
    if materialized is not None:
        # 物化落点带 rel 前缀 → 提升；否则已直接落在 dest_root
        if materialized != dest_root:
            _flatten_archive_prefix(materialized, dest_root)
            materialized = dest_root
        if _dest_valid(dest_root):
            _write_marker(dest_root, key)
            _logger.info("[skills_versioning] skill 本地物化：ref=%s → %s", ref, dest_root)
            return dest_root
        _logger.warning("[skills_versioning] 本地物化缺 main/nl2sql 技能组 ref=%s → None", ref)
    return None


def _materialize_remote(ref: str, dest_root: Path, origin: str, key: str) -> Optional[Path]:
    """远程浅克隆 ref → 拷出 src/agent/shared/skills（生产无 .git / GitLab-only 新 tag）。

    克隆到 pid 独立临时目录避免并发互踩；失败 → None（调用方显式报错，不静默退化）。
    """
    if not origin:
        _logger.warning(
            "[skills_versioning] skill 远程物化 ref=%s：无远程源（未配 SKILLS_GIT_REMOTE 且无本地仓库）",
            ref,
        )
        return None
    parent = dest_root.parent  # <data_root>/skill_refs/
    parent.mkdir(parents=True, exist_ok=True)
    work = parent / f".work_{dest_root.name}_{os.getpid()}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    try:
        clone_shallow(origin, ref, str(work), timeout=300)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[skills_versioning] 浅克隆 ref=%s 失败: %s", ref, e)
        return None
    try:
        sub = work / "src" / "agent" / "shared" / "skills"
        if not sub.is_dir():
            _logger.warning("[skills_versioning] ref=%s 克隆结果无 src/agent/shared/skills 子树", ref)
            return None
        if dest_root.exists():
            shutil.rmtree(dest_root, ignore_errors=True)
        shutil.copytree(str(sub), str(dest_root))
        if _dest_valid(dest_root):
            _write_marker(dest_root, key)
            _logger.info("[skills_versioning] skill 远程物化：ref=%s → %s", ref, dest_root)
            return dest_root
        _logger.warning("[skills_versioning] ref=%s 远程物化缺技能组 → None", ref)
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def materialize_skills_ref(ref: str, src: str = "") -> Optional[Path]:
    """按 git ref 物化 skill 目录到 <data_root>/skill_refs/<safe_ref>/，进程级缓存。

    顺序（src="" 默认源）：本地仓库 archive（dev / 离线）→ 远程浅克隆（GitLab 真源）；
    已物化目录 marker 匹配时直接复用（跨进程零网络）。src 非空 = <path>@<ref> 显式
    其他仓库，走 legacy archive。

    Returns:
        物化后的 skill 根目录（含 main/ 与 nl2sql/ 两组）；失败 → None（调用方决定
        显式 fail run，不静默退化当前磁盘 skill）。
    """
    key = f"{src}@{ref}"
    with _skills_lock:
        if key in _skills_cache:
            return _skills_cache[key]

    # 同 key 并发物化（同进程两个 run 预检同一新 tag）串行化，避免互踩 work 目录
    lock = _key_lock(key)
    with lock:
        with _skills_lock:
            if key in _skills_cache:
                return _skills_cache[key]

        from agent.workspace_manager import get_workspace_manager

        dest_root = get_workspace_manager().data_root / "skill_refs" / _safe_ref(ref)

        # 快路径：物化目录已合法且 marker 匹配 → 复用。tag 不可变故永久复用；可变 ref
        # （branch）需 marker 新鲜（<1h，覆盖 API 预检后 worker 子进程窗口）。
        if _dest_valid(dest_root) and _marker_ok(dest_root, key) and (
            tag_like_skill(ref) or _marker_fresh(dest_root)
        ):
            with _skills_lock:
                _skills_cache[key] = dest_root
            return dest_root

        result: Optional[Path] = None
        if not src:
            base = _default_skills_base()
            repo_rel = enclosing_repo(base)
            result = _materialize_local(base, ref, dest_root, key)
            if result is None:
                origin = effective_origin(repo_rel[0] if repo_rel else None)
                result = _materialize_remote(ref, dest_root, origin, key)
        else:
            # 显式源：legacy archive（路径须在 git 仓库内，否则 git_archive 返回 None）
            base = Path(src).resolve()
            result = _materialize_local(base, ref, dest_root, key)

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
