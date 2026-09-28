"""SemanticDbDetector — 解析「数据库 → Wren 项目」映射，判断某库是否已建模（语义层）。

背景：WrenAI 语义层按 Wren 项目绑定数据源。每个库可在 db_config.json 里配置
`wren_project`（前端「数据库配置管理」下拉选择），配了就启动该库专属的 wrenai
MCP server（工具名带库名前缀，如 `wrenai_imdb_run_sql`）；未配置的库走
db_mcp_server 直连。

已建模库的来源（两层，兜底兼容存量）：
1. **显式配置**：db_config.json 中 `wren_project` 非空的库（key = DBConfig.name，
   与前端 configurable.db_name 一致）。
2. **兜底（存量迁移零成本）**：默认 `WREN_PROJECT_PATH` 项目里
   `config/connection_*.json` 建模过的物理库名（imdb 无 wren_project 字段仍识别为
   已建模）。

用法：
    detector = SemanticDbDetector()
    detector.discover()                    # → {"imdb", "aix_report"}（已建模库名集合）
    detector.is_modeled("imdb")            # → True
    detector.project_path_for("imdb")      # → "…/imdb_project"
    wrenai_server_name("imdb")             # → "wrenai_imdb"（server / 工具前缀）
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

# 共享 git archive 子树物化（skill 版本化同用）；保留 _git_archive_materialize 别名
from agent.utils.git_archive import git_archive_materialize as _git_archive_materialize  # noqa: F401
from agent.utils import git_repo

_logger = logging.getLogger(__name__)


def wrenai_server_name(db_name: str) -> str:
    r"""由 db_name 推导 wrenai MCP server 名 / 工具前缀（纯函数，调用方统一用）。

    `tool_name_prefix`（langchain_mcp_adapters）把 server 名与工具名纯拼接
    （`f"{server_name}_{tool.name}"`，无 sanitize），而 OpenAI 兼容端点校验函数名
    只允许 `^[a-zA-Z0-9_-]+$`，故 server 名必须是纯 ASCII 合法标识符。

    陷阱：Python `\W` 是 Unicode 词符——中文/西里尔等是词符，不会被
    `re.sub(r"\W+", "_", ...)` 折叠（2026-09 生产事故：库名
    `WIT运营管理平台数据库` 直出工具名 `wrenai_WIT运营管理平台数据库_run_sql`
    → 400 invalid tools[].function.name）。因此第一遍 `\W` 折叠（兼容旧行为，
    空格/斜杠/点等 → 下划线）后，再剔除残留的非 ASCII 字符得到可读 ASCII 骨架
    （`WIT运营管理平台数据库` → `WIT`）。纯 ASCII 库名输出与旧逻辑逐字节一致，零回归。

    2026-09-04 起**不再给骨架拼 8 位哈希**：哈希段对模型无语义又长，deepseek 系
    模型抄工具名时会在哈希段多写/漏写字符（生产实证
    `wrenai_WIT_d4cdc1c1d2_get_instructions`，c1 串抄错），且 Langfuse 里显示难懂。
    骨架非空 → 直接用可读骨架，工具名（`wrenai_WIT_run_sql`）模型易抄、显示清晰。
    仅当骨架为空（库名全中文/纯符号，如 `运营管理平台`）才退回 sha1[:8] 保证非空合法。
    """
    return "wrenai_" + _server_slug(db_name)


def _server_slug(db_name: str) -> str:
    """db_name → 合法 server 名片段（匹配 `^[a-zA-Z0-9_-]+$`，非空、确定）。

    撞名兜底：两个不同中文库折到同骨架（极罕见）时，mcp_tool._get_sub_server_config
    对 wrenai server 名做冲突检测并告警跳过该库（提示改库名），不再由哈希静默区分
    ——宁缺毋滥，避免两库工具前缀歧义污染提示词。Langfuse 显示层另有
    langfuse_client.display_wrenai_tool_name 把净化前缀换回库名全名。
    """
    name = db_name or ""
    # 第一遍：沿用旧规则折叠非词符（空格/斜杠/点/连字符 → 下划线）
    base = re.sub(r"\W+", "_", name).strip("_")
    if base and re.fullmatch(r"[A-Za-z0-9_-]+", base):
        return base  # 纯 ASCII 合法 → 原样返回（与旧实现一致）
    # 中文等 Unicode 词符漏网 → 剔成 ASCII 骨架（去哈希，保留可读性）
    skeleton = re.sub(r"[^A-Za-z0-9_-]", "", base)
    skeleton = re.sub(r"_+", "_", skeleton).strip("_")
    if skeleton:
        return skeleton
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]


def db_name_from_wrenai_tool(tool_name: str) -> str:
    """`wrenai_<slug>_<tool>` → 库名反查（取不到返回 ""）。P1-16 执行侧判权用。

    **不能"反解 slug"**：`_server_slug` 是有损折叠（`WIT运营管理平台数据库` → `WIT`），
    一个 slug 可能对应多个库名，反解必然错。所以反过来做 —— 拿**当前工作区已建模库**
    逐个正算 `wrenai_server_name(db) + "_"` 去匹配前缀。命中不了（工作区没这个库 /
    slug 撞名被跳过 / discover 读不出来）→ 返回 ""，调用方按**放行**处理
    （判权不能因为"查不出库名"就拒掉合法调用）。

    只认 `_run_sql` / `get_db_info` 这类**查询工具**：`wrenai_<slug>_list_knowledge`
    同样带库名，一并反查即可（本函数不区分工具后缀，反查的是 server 前缀）。
    """
    name = (tool_name or "").strip()
    if not name.startswith("wrenai_"):
        return ""
    try:
        for db in get_detector().discover():
            if name.startswith(wrenai_server_name(db) + "_"):
                return db
    except Exception:  # noqa: BLE001  发现集合读不出来 → 返回 ""（调用方放行）
        _logger.warning("[semantic_db] 反查 %s 的库名失败", name, exc_info=True)
    return ""


# ── 语义库 A/B：WREN_SEMANTIC_OVERRIDE 版本物化 ───────────────
# 实验 worker 进程设 WREN_SEMANTIC_OVERRIDE（逗号分隔多库）。命中后把该库的 Wren
# 项目物化到 git ref 所指版本（git archive / 远程浅克隆），wrenai MCP server 的
# --project 指向物化目录 → 同查询同 prompt 只换语义库版本。
#
# 取值两种形态：
#   db=ref          从该库当前项目所在 git 仓库取 ref（默认，物化"正在服务的语义库"）
#   db=path@ref     从显式 path 取 ref（如历史版本在 nl2sql 仓库 src/test/wrenai_exec_Chinook@v6.0.0）
#
# 版本真源与 skill 侧同构：语义库仓库（正在服务的项目目录）自己的 origin 就是版本
# 远程。本地 git archive 取不到该 ref（刚推 tag、容器未 fetch）时，src="" 会直取
# origin 浅克隆——不再静默退化跑当前版本（run 预检用 materialize_semantic_ref 显式
# 物化，失败即报错）。物化目录落 <data_root>/offline_experiment/semantic_refs/<db>/<ref>/
# 并写 `.nl2sql_wren_ok` marker（记录 src|ref）——API 预检物化后 worker 子进程零网络复用，
# 且跨容器换代存活（2026-09-12 由 tmp 迁入，见 _cache_dir）。
_semantic_override_cache: dict[tuple[str, str, str], Optional[str]] = {}
_semantic_override_lock = threading.Lock()
# 物化锁：按 (db, source, ref) 一把（见 `_materialize_key_lock`）。键数 ≈ 参与 A/B 的
# (库, 版本) 组合数，天然有界。
_materialize_locks: dict[tuple[str, str, str], threading.Lock] = {}
_materialize_locks_guard = threading.Lock()


def _parse_semantic_overrides() -> dict[str, tuple[str, str]]:
    """解析 WREN_SEMANTIC_OVERRIDE → {db: (src, ref)}；src 空串 = 用 base。"""
    raw = os.environ.get("WREN_SEMANTIC_OVERRIDE", "").strip()
    out: dict[str, tuple[str, str]] = {}
    if not raw:
        return out
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        db, spec = (s.strip() for s in part.split("=", 1))
        if not db or not spec:
            continue
        if "@" in spec:
            src, ref = (s.strip() for s in spec.split("@", 1))
        else:
            src, ref = "", spec
        if db and ref:
            out[db] = (src, ref)
    return out


def _safe_ref(ref: str) -> str:
    """ref（tag/commit/branch）转目录名安全片段。"""
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", ref)[:64] or "ref"


def _lookup_override(overrides: dict[str, tuple[str, str]], db_name: str) -> Optional[tuple[str, str]]:
    """优先精确匹配，其次大小写不敏感匹配（db_config name 与查询 db_name 可能大小写不一致）。"""
    if db_name in overrides:
        return overrides[db_name]
    folded = db_name.casefold()
    for k, v in overrides.items():
        if k.casefold() == folded:
            return v
    return None


# Wren 项目合法性标记：版本间目录布局不同（v2/v6/HEAD 用 models/；v3~v5 用
# wren_project.yml；HEAD/v6 另含 target/mdl.json）——命中任一即视为合法项目。
_WREN_MARKERS = ("wren_project.yml", "models", "target/mdl.json", "config")
# 物化目录 marker：记录 src|ref，供跨进程复用（API 预检 → worker 子进程零网络）
_MARKER_FILE = ".nl2sql_wren_ok"


def _wren_markers_hit(root: Path) -> bool:
    return any((root / m).exists() for m in _WREN_MARKERS)


def _source_tag(source: str) -> str:
    """把物化源路径压缩成目录名安全且**不重名**的片段。

    `src` 是「按需覆盖的物化源」（`WREN_SEMANTIC_OVERRIDE=db=path@ref` 里的 path），
    空串表示「用正在服务的那个库」。用「末段名 + 全路径哈希」而不是 `_safe_ref(src)`：
    后者把 `/`、`:` 都换成 `_` 后不同路径会撞成同一个名字（`/a/b/c` 与 `/a_b/c`），
    而这里撞名 = 两个来源共用目录 = 互相 `rmtree`（正是本次要修的坑）。末段名保留
    可读性（一眼看出是哪个库）。
    """
    if not source:
        return "base"
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
    return f"{_safe_ref(Path(source).name)[:24]}-{digest}"


def _cache_dir(db_name: str, source: str, ref: str) -> Path:
    """物化缓存目录：`.../offline_experiment/semantic_refs/<db>/<src标签>_<ref>/`。

    2026-09-12 由系统临时目录（`%TEMP%/nl2sql_wren_semantic_cache/`，生产 = 容器 /tmp）
    迁入：tmp 随容器换代清空 → 每次发版后各语义库版本首用都要重新浅克隆（需 GitLab
    可达 + `/app/data/.ssh` key），物化后若跑过 wren 构建也白做。放 data_root 下与
    skill_refs/ prompt_refs/ 同属离线实验产物、运维一处可见；仍在 `FILE_PERMISSIONS`
    的 `deny /**` 之外（不进 `shared/`），在线 agent 读不到。旧 tmp 目录不自动迁移。

    **目录名必须同时含 src 与 ref**（2026-09-24 审计修正）：进程缓存键是
    `(db_name, source, ref)`、marker 也是 `source|ref`，而旧命名只含 `ref` ⇒
    `semantic_project_path`（override 命中，src 可非空）与 `materialize_semantic_ref`
    （src="")对同一个库的两个不同来源会**用同一个目录**：后到的那个 `rmtree` 掉前一个
    正在服务的那份，再换进另一仓库的内容（症状 = 问数报 `target/mdl.json` 缺失 /
    A/B 结论被污染）。加进 src 标签后，两个来源各用各的目录，这条路径彻底断开。
    旧命名的目录会成为孤儿（**不主动删**：可能还有在跑的 A/B run 正指着它）。
    """
    from agent.workspace_manager import get_workspace_manager

    return (
        get_workspace_manager().offline_experiment_dir
        / "semantic_refs" / _safe_ref(db_name) / f"{_source_tag(source)}_{_safe_ref(ref)}"
    )


def _read_marker(root: Path) -> Optional[tuple[str, str]]:
    """读缓存根上的 marker → `(key, 项目相对根的位置)`；没有/读不动 → None。

    两行格式：第一行是物化身份 `source|ref`，第二行是项目目录相对**缓存根**的位置
    （`.` = 缓存根本身就是项目目录）。老的单行 marker 视为 `("...", ".")`。
    """
    try:
        p = root / _MARKER_FILE
        if not p.is_file():
            return None
        lines = p.read_text(encoding="utf-8").splitlines()
        return (lines[0].strip(), (lines[1].strip() if len(lines) > 1 else "."))
    except Exception:  # noqa: BLE001
        return None


def _write_marker(root: Path, key: str, project_rel: str = ".") -> None:
    """在**缓存根**（不是项目目录）写 marker：记身份 + 项目目录在哪。

    为什么要记第二行：`git_archive_materialize` 对「仓库子目录」型的物化源
    （`base` 在 app 主仓库内，如 `src/test/wrenai_exec_*`）会把项目放在
    `<缓存根>/<相对路径>/` 下 ⇒ 只看缓存根本身**判断不出**它是不是一个项目，
    旧实现的快路径对这类来源永远不命中 ⇒ 每次调用都重跑一次 git archive。
    """
    try:
        (root / _MARKER_FILE).write_text(f"{key}\n{project_rel}\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _logger.warning("[semantic_db] 写 marker 失败: %s", e)


def _materialize_from_origin(source: Path, ref: str, dest_root: Path) -> bool:
    """本地取不到 ref（刚推 tag 容器未 fetch / 浅克隆无历史 tag）→ 直取仓库 origin。

    仅 src=""（正在服务的语义库，独立小仓库）走远程；显式 path@ref 的历史版本保持
    本地 archive。浅克隆 ref 到 pid 独立临时目录后拷出项目树（clone 根即 Wren 项目
    根：wren_project.yml 在仓库根）。返回是否得到合法项目；无 origin / 克隆失败 → False。
    """
    try:
        ok, out = git_repo._run(["remote", "get-url", "origin"], cwd=str(source), timeout=15)
        origin = out.strip() if ok else ""
    except Exception:  # noqa: BLE001
        origin = ""
    if not origin:
        return False
    parent = dest_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    work = parent / f".work_{dest_root.name}_{os.getpid()}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    try:
        git_repo.clone_shallow(origin, ref, str(work), timeout=300)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[semantic_db] 浅克隆 ref=%s 失败: %s", ref, e)
        return False
    try:
        if dest_root.exists():
            shutil.rmtree(dest_root, ignore_errors=True)
        dest_root.mkdir(parents=True, exist_ok=True)
        for child in list(Path(work).iterdir()):
            if child.name == ".git":
                continue
            target = dest_root / child.name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(child), str(target))
        return _wren_markers_hit(dest_root)
    except Exception as e:  # noqa: BLE001
        _logger.warning("[semantic_db] 远程物化 ref=%s 失败: %s", ref, e)
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _materialize_key_lock(key: tuple[str, str, str]) -> threading.Lock:
    """按 `(db, source, ref)` 取一把锁：同一个物化目标只允许一个线程在跑。

    与 `skills_versioning._key_lock` 同款。键与目录名同源（都含 source）⇒ 不看目录名
    也能确定「不会有两把锁保护同一个目录」。只在事件循环线程/同步调用方上取，dict
    自身用小锁保护。
    """
    with _materialize_locks_guard:
        lock = _materialize_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _materialize_locks[key] = lock
        return lock


def _reuse_if_ready(dest_root: Path, marker_key: str) -> Optional[str]:
    """磁盘快路径：缓存根上是本 key 物化出的合法项目 → 返回项目目录（跨进程零网络）。

    判据三条缺一不可：marker 身份一致、marker 记的项目位置存在、那里有 wren 项目标记。
    """
    mk = _read_marker(dest_root)
    if mk is None or mk[0] != marker_key:
        return None
    project = dest_root if mk[1] in ("", ".") else dest_root / mk[1]
    return str(project) if _wren_markers_hit(project) else None


def _new_stage_dir(dest_root: Path) -> Path:
    """本次物化的暂存目录（与 dest_root 同盘、隐藏）。**取名即占位**。

    兄弟目录而不是 `dest_root` 本身：物化过程要往目录里写一堆文件，写在**正在被读**
    的位置上就会出现半成品（与 wren 项目那套「暂存副本 + 原子换入」同一理由）。
    靠 mkdir 的 `FileExistsError` 判重名，同毫秒内的两次调用也不会拿到同一个名字。
    """
    parent = dest_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / f".stage-{dest_root.name}"
    n = 0
    while True:
        try:
            stage.mkdir()
            return stage
        except FileExistsError:
            n += 1
            stage = parent / f".stage-{dest_root.name}-{n}"


# 目录/文件 rename 的重试参数（只对 Windows 的「瞬时被拒」有意义，见 `_rename_retry`）
_RENAME_RETRIES = 8
_RENAME_RETRY_SLEEP = 0.02


def _rename_retry(src: Path, dst: Path) -> None:
    """同盘 `os.replace`，被瞬时拒绝时退避重试几次。

    Windows 上 rename 会被**瞬时**拒绝（`WinError 5`）：目标被别的进程打开、刚被删的
    文件还在 delete-pending、AV/DLP 过滤驱动在扫刚写出来的文件……本次实测在系统临时
    目录里偶发一次、隔 50ms 重试即成功（Linux 不受影响：rename 对被打开的目标无条件
    成功，生产跑 Linux ⇒ 第一次就返回，重试等于零成本）。失败重试是安全的：rename
    失败时源和目标都没动。
    """
    last: Optional[OSError] = None
    for attempt in range(_RENAME_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:  # Windows only
            last = e
            time.sleep(_RENAME_RETRY_SLEEP * (attempt + 1))
    raise OSError(f"rename {src.name} → {dst.name} 失败（重试 {_RENAME_RETRIES} 次）：{last}") from last


def _install_staged(stage: Path, dest_root: Path) -> None:
    """把物化好的暂存目录**换入** dest_root（同盘 rename，读者看不到半成品）。

    目录已存在（换了 marker 身份、或上一个版本）时先把它 rename 挪开再删：直接
    `rmtree` 会让正在读它的进程看到「文件一个个消失」。两次 rename 之间目录名短暂
    空缺（Linux 上是 µs 级目录项更新；只有跨进程并发到同一个 key 才会碰上）。
    """
    trash: Optional[Path] = None
    if dest_root.exists():
        trash = dest_root.parent / f".old-{dest_root.name}-{os.getpid()}"
        if trash.exists():
            shutil.rmtree(trash, ignore_errors=True)
        _rename_retry(dest_root, trash)  # 同盘 rename
    try:
        _rename_retry(stage, dest_root)
    except OSError:
        if trash is not None:
            _rename_retry(trash, dest_root)  # 回滚：原目录回到原处
        raise
    if trash is not None:
        shutil.rmtree(trash, ignore_errors=True)


def _materialize_semantic(db_name: str, src: str, ref: str, base: Path) -> Optional[str]:
    """把该库语义库 ref 物化到缓存目录，返回项目目录；失败 → None。

    顺序：进程缓存 → 磁盘快路径（合法项目 + marker 匹配，跨进程零网络）→ 本地 git
    archive（dev / 离线 / 已在服务的版本）→ 远程浅克隆（src="" 且仓库有 origin）。
    未命中 override（spec None）由调用方判断，本函数不读 env。

    并发（2026-09-24 审计修正）：慢路径按 `(db, source, ref)` 加锁 —— 旧实现里
    `rmtree` + archive + move 全在锁外，同 key 并发（API 预检线程 + worker 子进程）
    会把一个半成品目录当成物化好的项目用。跨进程的那一半由「暂存目录 + 原子换入」
    （`_install_staged`）兜住：内容只在完整之后才出现在 dest_root 上。
    """
    source = Path(src).resolve() if src else base.resolve()
    key = (db_name, str(source), ref)
    with _semantic_override_lock:
        if key in _semantic_override_cache:
            return _semantic_override_cache[key]
    dest_root = _cache_dir(db_name, str(source), ref)
    marker_key = f"{source}|{ref}"
    ready = _reuse_if_ready(dest_root, marker_key)
    if ready is None:
        with _materialize_key_lock(key):
            ready = _reuse_if_ready(dest_root, marker_key)  # 等锁期间别人可能做好了
            if ready is None:
                ready = _materialize_now(db_name, src, ref, source, dest_root, marker_key, base)
    with _semantic_override_lock:
        _semantic_override_cache[key] = ready
    return ready


def _materialize_now(
    db_name: str, src: str, ref: str, source: Path, dest_root: Path, marker_key: str, base: Path,
) -> Optional[str]:
    """慢路径实体（调用方已持有该 key 的锁）：物化到暂存目录 → 换入 → 返回项目目录。"""
    stage = _new_stage_dir(dest_root)
    rel = "."
    materialized = _git_archive_materialize(source, ref, stage)
    if materialized is not None and _wren_markers_hit(materialized):
        project = materialized
        try:
            # 项目目录相对缓存根的位置（仓库子目录型来源会落在 stage 的下级）——
            # 写进 marker 第二行，快路径靠它把项目目录找回来
            rel = str(materialized.relative_to(stage)).replace("\\", "/")
        except ValueError:  # pragma: no cover  —— 不该发生（archive 解到 stage 里）
            rel = "."
        _write_marker(stage, marker_key, rel)
        _logger.info(
            "[semantic_db] 语义库 A/B：db=%s ref=%s → %s（markers=%s）",
            db_name, ref, project,
            [m for m in _WREN_MARKERS if (project / m).exists()],
        )
    elif not src:
        # 本地取不到该 ref（刚推 tag / 浅克隆无历史）→ 直取 origin，不再静默退化
        if _materialize_from_origin(source, ref, stage):
            rel = "."
            materialized = stage
            _write_marker(stage, marker_key, rel)
            _logger.info("[semantic_db] 语义库 A/B（远程）：db=%s ref=%s → %s", db_name, ref, dest_root)
        else:
            shutil.rmtree(stage, ignore_errors=True)
            _logger.warning(
                "[semantic_db] 语义库版本物化失败 db=%s ref=%s（本地无该 ref 且从 origin 取不到："
                "tag 不存在/未推送/网络不可达/未绑 git）→ 回退 %s",
                db_name, ref, base,
            )
            return None
    else:
        shutil.rmtree(stage, ignore_errors=True)
        _logger.warning(
            "[semantic_db] 语义库版本物化失败/无项目标记 db=%s ref=%s → 回退 %s",
            db_name, ref, base,
        )
        return None
    _install_staged(stage, dest_root)
    return str(dest_root if rel in ("", ".") else dest_root / rel)


def semantic_project_path(db_name: str, base: Path) -> Optional[str]:
    """若 WREN_SEMANTIC_OVERRIDE 命中 db_name，物化该 ref 并返回物化目录。

    物化源：override 显式给 path 时用 path（如历史版本在 nl2sql 仓库内），否则用
    base（正在服务的语义库所在 git 仓库）。未命中 / 物化失败 / 无项目标记 → None
    （调用方回退 base 当前版本）。进程级按 (db, src, ref) 缓存一次。
    """
    overrides = _parse_semantic_overrides()
    spec = _lookup_override(overrides, db_name)
    if not spec:
        return None
    src, ref = spec
    return _materialize_semantic(db_name, src, ref, base)


def reset_semantic_override_cache() -> int:
    """清「语义库版本物化」缓存（由 `invalidate_db_discovery_caches()` 统一调用）。返回清掉的条数。

    缓存值是物化目录的绝对路径（`<offline_experiment>/semantic_refs/...`）。语义库
    版本/环境变量变了必须重算 —— 否则 A/B 实验拿到的仍是上一次物化出来的那份。
    只清内存里的映射，**不删磁盘目录**：重算时还能走 marker 快路径。
    """
    with _semantic_override_lock:
        n = len(_semantic_override_cache)
        _semantic_override_cache.clear()
    return n


def materialize_semantic_ref(db_name: str, ref: str) -> Optional[str]:
    """显式物化 db 的语义库 ref（不读 WREN_SEMANTIC_OVERRIDE env，供 run 预检调）。

    base 取该库正在服务的项目目录（get_detector().project_path_for）；db 未建模 /
    ref 不可得（本地无且无 origin）→ None。返回物化的 Wren 项目目录。
    """
    base = get_detector().project_path_for(db_name)
    if not base:
        return None
    return _materialize_semantic(db_name, "", ref, Path(base))


# ── db_name 归一化（查询集 → db_config 配置名）─────────────────
# 语义路由（is_modeled / wrenai server 前缀 / dynamic_prompt）严格按
# DBConfig.name 匹配；查询集（Dataset item / 手工查询文件）里 db_name 可能是
# name 的小写变体（chinook_aliyun vs Chinook_Aliyun）或物理库 database 名
# （chinook），会被判未建模 → 强制走直连 B，语义库 A/B 不生效。
# 这里做三档匹配：name casefold → 唯一物理库名。加载后进程级缓存。
_db_name_norm_cache: Optional[tuple[dict[str, str], dict[str, str]]] = None
_db_name_norm_lock = threading.Lock()


def _load_db_name_norm() -> tuple[dict[str, str], dict[str, str]]:
    """装载 db_config → (name.casefold()→name, 唯一 database.casefold()→name)。"""
    global _db_name_norm_cache
    with _db_name_norm_lock:
        if _db_name_norm_cache is not None:
            return _db_name_norm_cache
    name_map: dict[str, str] = {}
    db_to_names: dict[str, list[str]] = {}
    try:
        from mcp_server.db_mcp_server.db.core.db_config_store import get_store

        for cfg in get_store().get_all_decrypted():
            nm = getattr(cfg, "name", "") or ""
            if nm:
                name_map[nm.casefold()] = nm
            db = getattr(cfg, "database", "") or ""
            if db:
                db_to_names.setdefault(db.casefold(), []).append(nm)
    except Exception:  # noqa: BLE001  读不到 db_config 时不归一化（原样返回）
        _logger.warning("[semantic_db] 读 db_config 失败，db_name 不归一化")
    # 仅当物理库名唯一映射到一个配置名时才兜底（多配置共用 database 时跳过，
    # 如 Chinook_Weint / Chinook_AutoIncrement 都指向 Chinook_AutoIncrement）
    db_map = {
        dk: nms[0]
        for dk, nms in db_to_names.items()
        if len(set(nms)) == 1 and nms[0]
    }
    result = (name_map, db_map)
    with _db_name_norm_lock:
        _db_name_norm_cache = result
    return result


def reset_db_name_norm_cache() -> None:
    """清 db_name 归一化缓存。

    不清的后果**不是显示问题**：新建模的库名归一化失败 → `is_modeled()`
    判 False → 该库的查询静默从语义层掉到 dbmcp 直连（口径/物理 SQL 全变），
    而且不报错。本缓存原先**没有任何失效入口**（P1-11 补上），
    现在由 `invalidate_db_discovery_caches()` 统一调用 —— **别只调 detector 的那个**。
    """
    global _db_name_norm_cache
    with _db_name_norm_lock:
        _db_name_norm_cache = None


def invalidate_db_discovery_caches() -> None:
    """db_config / 语义库发生**写变更**后统一失效「发现类」缓存 —— 唯一推荐入口。

    三个缓存必须一起动，漏一个就是静默错：
      - `SemanticDbDetector` 缓存：已建模库集合 / 项目路径 → 决定工具可见性与 wren 定位；
      - `_db_name_norm_cache`：库名归一化 → 漏则新建模的库被判"未建模"，静默掉到 dbmcp 直连；
      - `_semantic_override_cache`：语义库版本物化目录 → 漏则 A/B 跑的是旧版本那份。

    历史：这三个原先是「切工作区时清一遍」（`workspace_manager/cache_reset.py`，
    2026-09-25 随多工作区机件删除）。工作区路径已钉死、不再有切换，缓存只会因为
    **db_config / 语义库被改写**而失效 —— 所以改挂到这里，并由
    `api/db_config.py`（增删改库 / 手动对账）、`api/wren_semantic.py`（语义库变更）
    在写路径上调用。调用点极少，改动时请一并核对：
    `grep -rn invalidate_db_discovery_caches src/`。
    """
    get_detector().invalidate()
    reset_db_name_norm_cache()
    reset_semantic_override_cache()


def normalize_db_name(db_name: str) -> str:
    """把查询里的 db_name 归一化为 db_config 配置名（大小写不敏感）。

    匹配顺序：name 精确/大小写 → 唯一物理库 database 名兜底；均未命中原样返回。
    只影响路由判定（is_modeled 等），不改变工具执行逻辑。
    """
    if not db_name:
        return db_name
    name_map, db_map = _load_db_name_norm()
    key = db_name.casefold()
    return name_map.get(key) or db_map.get(key) or db_name


class SemanticDbDetector:
    """解析 db_config 中每库的 wren_project 配置 + 默认项目兜底扫描。"""

    def __init__(self, project_path: Optional[str] = None) -> None:
        from agent.settings.setting import settings

        # 默认项目可能未配置（.env 无 WREN_PROJECT_PATH）——为 None 时跳过 legacy
        # 兜底扫描，不能直接 Path(None)（会 TypeError）。显式 wren_project 不受影响。
        raw = project_path or settings.WREN_PROJECT_PATH
        self._project_path = Path(raw) if raw else None
        self._lock = threading.RLock()
        self._cache: dict[str, str] = {}  # db_name(name) → wren 项目绝对路径
        self._cache_valid = False

    # ── 数据源 ────────────────────────────────────────────

    def _load_mapping(self) -> dict[str, str]:
        """显式映射：db_config 中 wren_project 非空的库（key = DBConfig.name）。

        延迟 import db_config_store，避免 mcp_server↔agent 包循环依赖
        （db_config_store 本身不 import agent 包，无循环）。
        """
        mapping: dict[str, str] = {}
        try:
            from mcp_server.db_mcp_server.db.core.db_config_store import get_store

            for cfg in get_store().get_all_decrypted():
                if getattr(cfg, "wren_project", ""):
                    mapping[cfg.name] = cfg.wren_project
        except Exception as e:  # noqa: BLE001  配置读取失败不影响判断（兜底仍生效）
            _logger.warning("[semantic_db] 读取 db_config wren_project 失败: %s", e)
        return mapping

    def _scan_legacy(self) -> set[str]:
        """兜底：扫描默认 WREN_PROJECT_PATH 项目 config/connection_*.json。

        与旧版 `_scan()` 同逻辑——默认项目里建模过的物理库名集合，
        保证 imdb（无 wren_project 字段的存量配置）仍被识别为已建模。
        """
        names: set[str] = set()
        if not self._project_path:  # 默认项目未配置 → 无 legacy 兜底
            return names
        config_dir = self._project_path / "config"
        if not config_dir.is_dir():
            return names
        for f in sorted(config_dir.glob("connection_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                props = data.get("properties", {}) or {}
                db = props.get("database") or props.get("database_name") or ""
                if db:
                    names.add(str(db))
            except (json.JSONDecodeError, OSError) as e:
                _logger.warning("[semantic_db] 读取 %s 失败: %s", f, e)
        if names:
            _logger.info("[semantic_db] 默认项目已建模数据源: %s", sorted(names))
        return names

    # ── 解析 ──────────────────────────────────────────────

    def _resolve(self) -> dict[str, str]:
        """合并显式映射 + 默认项目兜底，得 db_name(name) → 项目路径。"""
        mapping = self._load_mapping()
        legacy = self._scan_legacy()
        for db in legacy:
            mapping.setdefault(db, str(self._project_path))
        return mapping

    def _refresh(self) -> None:
        with self._lock:
            if not self._cache_valid:
                self._cache = self._resolve()
                self._cache_valid = True
                if self._cache:
                    _logger.info(
                        "[semantic_db] 已建模库→项目映射: %s",
                        {k: v for k, v in sorted(self._cache.items())},
                    )

    # ── 对外接口 ──────────────────────────────────────────

    def discover(self, refresh: bool = False) -> set[str]:
        """返回已建模库名（name）集合（带缓存）。"""
        with self._lock:
            if refresh:
                self._cache_valid = False
            self._refresh()
            return set(self._cache)

    def invalidate(self) -> None:
        """使缓存失效，下次读取时从 db_config 重建。

        管理 API 在 upsert/delete 后调用，让 semantic 标记即时反映 wren_project
        变更，无需重启进程（detector 是进程级单例，_cache_valid 置 False 即重建）。
        """
        with self._lock:
            self._cache_valid = False
            self._cache = {}

    def is_modeled(self, db_name: str) -> bool:
        """判断某库是否已建模（语义层）——严格按 name 判定。

        路由场景（dynamic_prompt / _inject_db_name）必须精确匹配 name：
        若放宽到 physical database 兜底，name 与 database 不同名时（如
        name=clickhouse, database=aix_jinkoxn）会误判 modeled，而实际启动的是
        wrenai_<database> server，导致 LLM 按 wrenai_<name>_ 找不到工具。
        前端 semantic 标记如需 database 兜底，由调用方 `is_modeled(name) or
        is_modeled(database)` 显式做。
        """
        return db_name in self.discover()

    def project_path_for(self, db_name: str) -> Optional[str]:
        """返回 db_name 对应的 Wren 项目绝对路径；未建模返回 None。

        解析顺序：
        1. `_cache` 按 name（或默认项目 connection 的物理库名）命中；
        2. 该库的 physical database 字段在 `_cache` 中命中（name 与 database
           不同名时，用于前端 semantic 标记等宽口径场景）。
        """
        if not db_name:
            return None
        base: Optional[str] = None
        with self._lock:
            self._refresh()
            if db_name in self._cache:
                base = self._cache[db_name]
        # database 字段兜底（宽口径）：connection 里可能用 physical database 建模
        if base is None:
            try:
                from mcp_server.db_mcp_server.db.core.db_config_store import get_store

                cfg = get_store().get(db_name)
                if getattr(cfg, "wren_project", ""):
                    base = cfg.wren_project
                elif cfg.database and cfg.database in self._cache:
                    base = self._cache[cfg.database]
            except Exception:  # noqa: BLE001  找不到配置记录则按未建模处理
                pass
        if not base:
            return None
        # 语义库 A/B：WREN_SEMANTIC_OVERRIDE 命中 → 物化该 git ref；否则原路径
        return semantic_project_path(db_name, Path(base)) or base


# 单例
_detector: Optional[SemanticDbDetector] = None


def get_detector() -> SemanticDbDetector:
    global _detector
    if _detector is None:
        _detector = SemanticDbDetector()
    return _detector
