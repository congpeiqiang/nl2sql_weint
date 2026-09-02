"""WorkspaceManager — 统一工作区路径解析，替换所有 `Path(__file__).resolve().parents[N]` 硬编码。

设计：
- 每个工作区是一个独立目录，包含 checkpoint/feedback/db_config/语义库/报告/中间数据。
- memory/、skills/、model_config.json 全局共享，位于共享资源根（`_SHARED_RESOURCES_DIR`）。
- **外部基础目录（2026-08-28）**：`.env` 配置 `AGENT_DATA_ROOT`（项目外目录）后，
  shared 与默认工作区统一放到 `<AGENT_DATA_ROOT>/{shared,workspace}`，代码根 src/agent/
  彻底退出 VFS（main_agent/nl2sql_agent 的 `/` 兜底路由改为指向 data_root）。
  首次运行若外部目录缺失，自动从仓库内置 `src/agent/{shared,workspace}` 原子拷贝种子。
  未配置时回退仓库内 `src/agent/{shared,workspace}`（兼容现有部署）。
- 注册表 `workspaces.json` 记录所有工作区及当前活跃工作区。
- 活跃工作区切换即时生效（DynamicFilesystemBackend 每次操作前重新解析 root_dir）。

隔离矩阵：
    按工作区隔离：db_config.json, semantic/, report/, tmp/,
                  nl2sql_process_data/, large_tool_results/
    全局共享：    memory/, skills/, model_config.json, checkpoint/,
                  trace/, feedback/, fts.sqlite

用法：
    from agent.workspace_manager import get_workspace_manager

    wm = get_workspace_manager()
    print(wm.active_workspace)       # 活跃工作区目录
    print(wm.checkpoint_dir)         # 该工作区的 checkpoint 目录
    print(wm.shared_memory_dir)      # 共享 memory 目录（固定）
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# 必须先加载 .env 再读 AGENT_DATA_ROOT / SHARED_RESOURCES_PATH 等模块级 env——
# 否则 import 顺序不同时（如 eval 脚本直连、Docker env 注入）外部基础目录会静默回退。
# 默认 override=False：Docker 已注入的 env（如 AGENT_DATA_ROOT=/app/data）不被 .env 覆盖。
load_dotenv()

_logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# 默认注册表路径：工作区注册表（可被 .env WORKSPACE_REGISTRY_PATH 覆盖）
_DEFAULT_REGISTRY_PATH = os.getenv(
    "WORKSPACE_REGISTRY_PATH",
    str(Path(__file__).resolve().parent / "workspaces.json"),
)

# 仓库内置种子目录锚点（随 git 分发）：manager.py 在 workspace_manager/ 子目录，
# 上两层到 src/agent/。shared 与 workspace 的仓库种子从这里拷贝到外部基础目录。
_REPO_AGENT_DIR = Path(__file__).resolve().parent.parent

# 外部基础目录（项目外，.env AGENT_DATA_ROOT 配置）——shared 与默认工作区都放在这里。
# 配置后：shared → <AGENT_DATA_ROOT>/shared，默认工作区 → <AGENT_DATA_ROOT>/workspace。
# 为空 = 未配置，回退仓库内 src/agent/（旧行为，兼容现有部署）。
_DATA_ROOT = os.getenv("AGENT_DATA_ROOT", "").strip()

# 默认工作区目录：AGENT_DATA_ROOT/workspace（配置时）→ src/agent/workspace（回退）
_DEFAULT_WORKSPACE_DIR = (
    Path(_DATA_ROOT) / "workspace" if _DATA_ROOT else _REPO_AGENT_DIR / "workspace"
)

# 共享资源根目录（memory/、skills/、model_config.json 的父目录）
# 优先级：SHARED_RESOURCES_PATH env → AGENT_DATA_ROOT/shared → src/agent/shared
_SHARED_RESOURCES_DIR = Path(
    os.getenv("SHARED_RESOURCES_PATH")
    or (Path(_DATA_ROOT) / "shared" if _DATA_ROOT else _REPO_AGENT_DIR / "shared")
)


class WorkspaceManager:
    """统一工作区路径解析器。

    所有组件通过 `get_workspace_manager()` 获取单例，按需解析路径。
    活跃工作区切换后，checkpoint/feedback/store 等路径即时反映变化。
    """

    def __init__(self, registry_path: Optional[str] = None) -> None:
        self._registry_path = Path(registry_path or _DEFAULT_REGISTRY_PATH)
        self._cache: Optional[dict] = None
        self._cache_valid = False
        self._seed_data_root_once()  # 首次运行：外部基础目录缺失时从仓库种子初始化

    # ── 注册表读写 ──────────────────────────────────────────

    def _read_registry(self) -> dict:
        """读取工作区注册表 JSON。文件不存在时返回空注册表。"""
        if not self._registry_path.exists():
            return {"version": 1, "active": "", "workspaces": {}}
        try:
            return json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            _logger.warning("[workspace] 读取注册表 %s 失败: %s，按空处理", self._registry_path, e)
            return {"version": 1, "active": "", "workspaces": {}}

    def _write_registry(self, data: dict) -> None:
        """原子写入注册表。"""
        import tempfile

        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._registry_path.parent, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._registry_path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _refresh_cache(self) -> None:
        """刷新内存缓存。"""
        with _LOCK:
            reg = self._read_registry()
            self._cache = reg
            self._cache_valid = True

    def invalidate(self) -> None:
        with _LOCK:
            self._cache_valid = False
            self._cache = None

    # ── 首次运行种子初始化 ───────────────────────────────────

    def _seed_data_root_once(self) -> None:
        """首次运行初始化：AGENT_DATA_ROOT 配置时，若外部 shared/workspace 缺失，
        从仓库内置 `src/agent/shared`、`src/agent/workspace` 原子拷贝种子。

        目的：.env 里只配 `AGENT_DATA_ROOT` 即可跑起来（本地全新克隆 / Docker 首启），
        无需手工拷贝。目录已存在则跳过（此后运行时数据以外部目录为准，仓库种子不再改动）。

        原子性：先拷到同名 `.seed_tmp`，成功后 `os.replace` 改名，避免拷贝中断留下
        半成品目录导致后续运行误判「已存在」。
        """
        import shutil

        if not _DATA_ROOT:
            return  # 未配置外部基础目录，沿用仓库内目录，无需种子
        for target, seed in (
            (_SHARED_RESOURCES_DIR, _REPO_AGENT_DIR / "shared"),
            (_DEFAULT_WORKSPACE_DIR, _REPO_AGENT_DIR / "workspace"),
        ):
            try:
                if target.is_dir() or not seed.is_dir():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.parent / f"{target.name}.seed_tmp"
                shutil.rmtree(tmp, ignore_errors=True)
                shutil.copytree(seed, tmp)
                os.replace(tmp, target)
                _logger.info("[workspace] 首次运行：从仓库种子初始化 %s → %s", seed, target)
            except OSError as e:
                _logger.warning(
                    "[workspace] 种子初始化 %s 失败: %s（继续使用仓库内目录）", target, e
                )

    # ── 活跃工作区 ──────────────────────────────────────────

    def _resolve_active_path(self) -> Path:
        """解析当前活跃工作区的实际目录。

        顺序：注册表 active 指向的路径 → 环境变量 WORKSPACE_PATH → 默认工作区。
        前两级都校验目录存在，不存在则回退下一级（并打 warning 便于排障）。
        这是活跃工作区的**唯一**路径来源；`active_name` 按它反查名称，
        保证「显示的名称」与「实际生效的目录」永远一致。
        """
        with _LOCK:
            if not self._cache_valid:
                self._refresh_cache()
            assert self._cache is not None

            active_name = self._cache.get("active", "") or ""
            if active_name:
                ws = self._cache.get("workspaces", {}).get(active_name)
                if ws and ws.get("path"):
                    p = Path(ws["path"])
                    if p.is_dir():
                        return p.resolve()
                    _logger.warning(
                        "[workspace] 活跃工作区 '%s' 目录不存在: %s，回退默认/环境变量",
                        active_name, ws["path"],
                    )

            # 环境变量覆盖
            env_path = os.getenv("WORKSPACE_PATH", "")
            if env_path:
                p = Path(env_path)
                if p.is_dir():
                    return p.resolve()

            # 默认工作区
            return _DEFAULT_WORKSPACE_DIR.resolve()

    @property
    def active_workspace(self) -> Path:
        """当前活跃工作区的根目录。

        解析顺序（见 _resolve_active_path）：
        1. 注册表 `workspaces.json` 中 `active` 字段指向的工作区路径
        2. 环境变量 `WORKSPACE_PATH` 覆盖
        3. 默认工作区 `src/agent/workspace/`（零配置回退）
        """
        return self._resolve_active_path()

    @property
    def active_name(self) -> str:
        """当前活跃工作区名称，与 `active_workspace` 实际解析到的目录严格一致。

        按实际目录反查注册表名称；查不到（目录缺失回退默认、或 WORKSPACE_PATH
        指向未注册目录）时返回 'default'——不再返回「注册表里但目录已不存在」的悬空名。
        """
        try:
            actual = self._resolve_active_path()
            with _LOCK:
                if not self._cache_valid:
                    self._refresh_cache()
                assert self._cache is not None
                workspaces = self._cache.get("workspaces") or {}
            for k, v in workspaces.items():
                if not v.get("path"):
                    continue
                try:
                    if Path(v["path"]).resolve() == actual:
                        return k
                except OSError:
                    continue
            return "default"
        except Exception:
            return "default"

    # ── 共享资源路径（固定，不随工作区切换）──────────────────

    @property
    def data_root(self) -> Path:
        """VFS 根后端目录 = 共享资源根的父目录（shared 的上一层）。

        - 配置 AGENT_DATA_ROOT 时 = 该外部基础目录（代码根 src/agent/ 彻底退出 VFS）；
        - 未配置（旧部署）时 = src/agent/（兼容旧 VFS，行为与 `shared_code_backend` 一致）。

        main_agent / nl2sql_agent 的 `vfs_root_backend` 以此为根；SkillsMiddleware 的
        `sources=["/shared/skills/main/"]` 也经它解析（shared 必须是 data_root 的子目录）。
        """
        return _SHARED_RESOURCES_DIR.parent

    @property
    def shared_memory_dir(self) -> Path:
        """共享 memory 目录（所有工作区共用）。"""
        return _SHARED_RESOURCES_DIR / "memory"

    @property
    def shared_skills_dir(self) -> Path:
        """共享 skills 目录（所有工作区共用）。"""
        return _SHARED_RESOURCES_DIR / "skills"

    @property
    def shared_model_config_path(self) -> Path:
        """共享 model_config.json（默认路径，可被工作区覆盖）。"""
        return _SHARED_RESOURCES_DIR / "model_config.json"

    # ── 全局共享数据路径（不随工作区切换）────────────────────
    # 2026-08-27 决策：checkpoint/trace/fts/feedback 全局共享，切换工作区不丢会话。
    # 数据锚点 = src/agent/shared/（_SHARED_RESOURCES_DIR，可被 .env
    # SHARED_RESOURCES_PATH 覆盖）。workspace/ 本身是默认工作区目录，不能作为
    # 共享锚点。隔离矩阵更新为：
    #   按工作区隔离：db_config.json, semantic/, report/, tmp/,
    #                 nl2sql_process_data/, large_tool_results/
    #   全局共享：    memory/, skills/, model_config.json, checkpoint/,
    #                 trace/, feedback/, fts.sqlite

    @property
    def shared_data_root(self) -> Path:
        """全局共享数据根目录（所有工作区共用，锚定 _SHARED_RESOURCES_DIR）。"""
        return _SHARED_RESOURCES_DIR

    @property
    def shared_checkpoint_dir(self) -> Path:
        """全局共享 checkpoint 目录（所有工作区共用）。"""
        return _SHARED_RESOURCES_DIR / "checkpoint"

    @property
    def shared_trace_db(self) -> Path:
        """全局共享 trace 库路径（所有工作区共用，独立子目录存放 .sqlite/-shm/-wal）。"""
        return _SHARED_RESOURCES_DIR / "trace" / "traces.sqlite"

    @property
    def shared_feedback_dir(self) -> Path:
        """全局共享 feedback 目录（所有工作区共用）。"""
        return _SHARED_RESOURCES_DIR / "feedback"

    # ── 工作区级路径（随活跃工作区变化）──────────────────────

    @property
    def checkpoint_dir(self) -> Path:
        """当前工作区的 checkpoint 目录（仅供展示，实际存储走 shared_checkpoint_dir）。"""
        return self.active_workspace / "checkpoint"

    @property
    def feedback_dir(self) -> Path:
        """当前工作区的 feedback 目录（仅供展示，实际存储走 shared_feedback_dir）。"""
        return self.active_workspace / "feedback"

    @property
    def db_config_path(self) -> Path:
        """当前工作区的 db_config.json 路径。"""
        return self.active_workspace / "db_config.json"

    @property
    def model_config_path(self) -> Path:
        """当前工作区的 model_config.json 路径（优先工作区非空配置，回退共享）。"""
        local = self.active_workspace / "model_config.json"
        if local.exists():
            try:
                data = json.loads(local.read_text(encoding="utf-8"))
                if data.get("providers"):
                    return local
            except Exception:
                pass
        return self.shared_model_config_path

    @property
    def report_dir(self) -> Path:
        """当前工作区的 report 目录。"""
        return self.active_workspace / "report"

    @property
    def tmp_dir(self) -> Path:
        """当前工作区的 tmp 目录。"""
        return self.active_workspace / "tmp"

    @property
    def process_data_dir(self) -> Path:
        """当前工作区的 nl2sql_process_data 目录。"""
        return self.active_workspace / "nl2sql_process_data"

    @property
    def large_tool_results_dir(self) -> Path:
        """当前工作区的 large_tool_results 目录。"""
        return self.active_workspace / "large_tool_results"

    @property
    def semantic_dir(self) -> Path:
        """当前工作区的语义库根目录（即工作区根，Wren 项目直接放这里）。"""
        return self.active_workspace

    # ── 工作区管理（CRUD）───────────────────────────────────

    def list_workspaces(self) -> list[dict]:
        """列出所有已注册工作区（含默认工作区）。"""
        with _LOCK:
            if not self._cache_valid:
                self._refresh_cache()
            assert self._cache is not None
            workspaces = dict(self._cache.get("workspaces", {}))
            # 确保默认工作区出现在列表中
            if "default" not in workspaces:
                workspaces["default"] = {
                    "path": str(_DEFAULT_WORKSPACE_DIR.resolve()),
                    "name": "默认工作区",
                    "created_at": "",
                }
            active = self.active_name
            return [
                {**v, "name_key": k, "active": k == active}
                for k, v in workspaces.items()
            ]

    def register_workspace(self, name: str, path: str, display_name: str = "") -> dict:
        """注册一个新工作区。

        - 路径不存在时自动创建（含父目录链）并初始化子目录结构
        - 路径已存在且非空，不覆盖已有文件，只初始化缺失的子目录
        - 路径已存在但是文件 → 拒绝（无法作为工作区目录）
        - name 用于注册表唯一标识（如 "project-a"）
        """
        p = Path(path).resolve()

        if p.exists() and not p.is_dir():
            raise ValueError(f"路径已存在但不是目录: {path}")

        if not p.is_dir():
            try:
                p.mkdir(parents=True, exist_ok=True)
                _logger.info("[workspace] 注册工作区时自动创建目录: %s", p)
            except OSError as e:
                raise ValueError(f"无法创建目录 {path}: {e}") from e

        # 初始化工作区子目录结构
        self._init_workspace_dirs(p)

        with _LOCK:
            reg = self._read_registry()
            workspaces = reg.get("workspaces", {})

            if name in workspaces:
                raise ValueError(f"工作区 '{name}' 已存在")

            from datetime import datetime, timezone

            workspaces[name] = {
                "path": str(p),
                "name": display_name or name,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            # 如果这是第一个非默认工作区，自动设为活跃
            non_default = [k for k in workspaces if k != "default"]
            if not reg.get("active") and len(non_default) == 1:
                reg["active"] = name

            reg["version"] = reg.get("version", 1)
            reg["workspaces"] = workspaces
            self._write_registry(reg)
            self._cache = reg
            self._cache_valid = True

            _logger.info("[workspace] 注册工作区 '%s' → %s", name, p)
            return {**workspaces[name], "name_key": name, "active": reg.get("active") == name}

    def activate_workspace(self, name: str) -> dict:
        """切换活跃工作区。即时生效，无需重启。"""
        with _LOCK:
            reg = self._read_registry()
            workspaces = reg.get("workspaces", {})

            if name != "default" and name not in workspaces:
                raise KeyError(f"工作区 '{name}' 不存在")
            if name == "default" and "default" not in workspaces:
                # 确保默认工作区存在
                workspaces["default"] = {
                    "path": str(_DEFAULT_WORKSPACE_DIR.resolve()),
                    "name": "默认工作区",
                    "created_at": "",
                }
                reg["workspaces"] = workspaces

            # 切换前校验目录存在，避免「激活成功但运行时回退默认目录」的悬空态
            if name != "default":
                p = Path(workspaces[name]["path"])
                if not p.is_dir():
                    raise ValueError(f"工作区 '{name}' 目录不存在: {p}")

            reg["active"] = name
            self._write_registry(reg)
            self._cache = reg
            self._cache_valid = True

            _logger.info("[workspace] 激活工作区 '%s'", name)
            return {
                "ok": True,
                "active": name,
                "workspace": workspaces.get(name) or {
                    "path": str(_DEFAULT_WORKSPACE_DIR.resolve()),
                    "name": "默认工作区",
                },
            }

    def unregister_workspace(self, name: str) -> bool:
        """取消注册工作区（不删除文件）。"""
        if name == "default":
            raise ValueError("不能删除默认工作区")

        with _LOCK:
            reg = self._read_registry()
            workspaces = reg.get("workspaces", {})

            if name not in workspaces:
                return False

            del workspaces[name]
            if reg.get("active") == name:
                reg["active"] = "default"  # 回退到默认工作区

            reg["workspaces"] = workspaces
            self._write_registry(reg)
            self._cache = reg
            self._cache_valid = True

            _logger.info("[workspace] 取消注册工作区 '%s'", name)
            return True

    def _init_workspace_dirs(self, root: Path) -> None:
        """初始化工作区子目录结构（如不存在则创建）。"""
        dirs = [
            "checkpoint",
            "feedback",
            "report",
            "tmp",
            "nl2sql_process_data",
            "large_tool_results",
        ]
        for d in dirs:
            (root / d).mkdir(parents=True, exist_ok=True)

        # 初始化空 db_config.json（如不存在）
        db_config = root / "db_config.json"
        if not db_config.exists():
            db_config.write_text(
                json.dumps({"version": 1, "databases": []}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # 初始化空 model_config.json（如不存在，可选覆盖全局配置）
        model_config = root / "model_config.json"
        if not model_config.exists():
            model_config.write_text(
                json.dumps({"version": 1, "active": "", "providers": []}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        _logger.info("[workspace] 初始化工作区目录结构: %s", root)


# 单例
_default_manager: Optional[WorkspaceManager] = None


def get_workspace_manager() -> WorkspaceManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = WorkspaceManager()
    return _default_manager