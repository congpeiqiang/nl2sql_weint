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
import tempfile
import threading
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
    空格/斜杠/点等 → 下划线）后，再剔除残留的非 ASCII 字符并追加原名的短哈希
    （防不同中文库折叠后撞名）。纯 ASCII 库名输出与旧逻辑逐字节一致，零回归。
    """
    return "wrenai_" + _server_slug(db_name)


def _server_slug(db_name: str) -> str:
    """db_name → 合法 server 名片段（匹配 `^[a-zA-Z0-9_-]+$`，非空、确定）。"""
    name = db_name or ""
    # 第一遍：沿用旧规则折叠非词符（空格/斜杠/点/连字符 → 下划线）
    base = re.sub(r"\W+", "_", name).strip("_")
    if base and re.fullmatch(r"[A-Za-z0-9_-]+", base):
        return base  # 纯 ASCII 合法 → 原样返回（与旧实现一致）
    # 中文等 Unicode 词符漏网 → 剔成 ASCII 骨架
    skeleton = re.sub(r"[^A-Za-z0-9_-]", "", base)
    skeleton = re.sub(r"_+", "_", skeleton).strip("_")
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{skeleton}_{digest}" if skeleton else digest


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
# 物化，失败即报错）。物化目录落 <tmp>/nl2sql_wren_semantic_cache/<db>/<ref>/ 并写
# `.nl2sql_wren_ok` marker（记录 src|ref）——API 预检物化后 worker 子进程零网络复用。
_semantic_override_cache: dict[tuple[str, str, str], Optional[str]] = {}
_semantic_override_lock = threading.Lock()


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


def _cache_dir(db_name: str, ref: str) -> Path:
    return (
        Path(tempfile.gettempdir()) / "nl2sql_wren_semantic_cache"
        / _safe_ref(db_name) / _safe_ref(ref)
    )


def _marker_matches(root: Path, key: str) -> bool:
    try:
        p = root / _MARKER_FILE
        return p.is_file() and p.read_text(encoding="utf-8").strip() == key
    except Exception:  # noqa: BLE001
        return False


def _write_marker(root: Path, key: str) -> None:
    try:
        (root / _MARKER_FILE).write_text(key, encoding="utf-8")
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


def _materialize_semantic(db_name: str, src: str, ref: str, base: Path) -> Optional[str]:
    """把该库语义库 ref 物化到缓存目录，返回项目目录；失败 → None。

    顺序：进程缓存 → 磁盘快路径（合法项目 + marker 匹配，跨进程零网络）→ 本地 git
    archive（dev / 离线 / 已在服务的版本）→ 远程浅克隆（src="" 且仓库有 origin）。
    未命中 override（spec None）由调用方判断，本函数不读 env。
    """
    source = Path(src).resolve() if src else base.resolve()
    key = (db_name, str(source), ref)
    with _semantic_override_lock:
        if key in _semantic_override_cache:
            return _semantic_override_cache[key]
    dest_root = _cache_dir(db_name, ref)
    marker_key = f"{src}|{ref}"
    # 快路径：目录已物化本 key 的合法项目 → 复用（API 预检后 worker 子进程零网络）
    if _wren_markers_hit(dest_root) and _marker_matches(dest_root, marker_key):
        with _semantic_override_lock:
            _semantic_override_cache[key] = str(dest_root)
        return str(dest_root)
    if dest_root.exists():
        shutil.rmtree(dest_root, ignore_errors=True)
    materialized = _git_archive_materialize(source, ref, dest_root)
    result: Optional[str] = None
    if materialized is not None and _wren_markers_hit(materialized):
        result = str(materialized)
        _write_marker(materialized, marker_key)
        _logger.info(
            "[semantic_db] 语义库 A/B：db=%s ref=%s → %s（markers=%s）",
            db_name, ref, materialized,
            [m for m in _WREN_MARKERS if (materialized / m).exists()],
        )
    elif not src:
        # 本地取不到该 ref（刚推 tag / 浅克隆无历史）→ 直取 origin，不再静默退化
        if _materialize_from_origin(source, ref, dest_root):
            result = str(dest_root)
            _write_marker(dest_root, marker_key)
            _logger.info("[semantic_db] 语义库 A/B（远程）：db=%s ref=%s → %s", db_name, ref, dest_root)
        else:
            _logger.warning(
                "[semantic_db] 语义库版本物化失败 db=%s ref=%s（本地无该 ref 且从 origin 取不到："
                "tag 不存在/未推送/网络不可达/未绑 git）→ 回退 %s",
                db_name, ref, base,
            )
    else:
        _logger.warning(
            "[semantic_db] 语义库版本物化失败/无项目标记 db=%s ref=%s → 回退 %s",
            db_name, ref, base,
        )
    with _semantic_override_lock:
        _semantic_override_cache[key] = result
    return result


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
