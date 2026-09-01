"""WorkspaceManager — 统一工作区路径解析，替换所有 `Path(__file__).resolve().parents[N]` 硬编码。

设计：
- 每个工作区是一个独立目录，包含 checkpoint/feedback/db_config/语义库/报告/中间数据。
- memory/、skills/、model_config.json 全局共享，位于后端项目的 `src/agent/shared/`。
- 默认工作区 = `src/agent/workspace/`（零配置回退，兼容现有部署）。
- 注册表 `workspaces.json` 记录所有工作区及当前活跃工作区。
- 活跃工作区切换即时生效（DynamicFilesystemBackend 每次操作前重新解析 root_dir）。

隔离矩阵：
    按工作区隔离：db_config.json, checkpoint/, feedback/, semantic/, report/, tmp/,
                  nl2sql_process_data/, large_tool_results/
    全局共享：    memory/, skills/, model_config.json（可选工作区覆盖）

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

_logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# 默认注册表路径：工作区注册表（可被 .env WORKSPACE_REGISTRY_PATH 覆盖）
_DEFAULT_REGISTRY_PATH = os.getenv(
    "WORKSPACE_REGISTRY_PATH",
    str(Path(__file__).resolve().parent / "workspaces.json"),
)

# 默认工作区目录（零配置回退，即现有 src/agent/workspace/）
# manager.py 在 workspace_manager/ 子目录下，需要上两层到 src/agent/
_DEFAULT_WORKSPACE_DIR = Path(__file__).resolve().parent.parent / "workspace"

# 共享资源根目录（memory/、skills/、model_config.json 的父目录）
# 独立于默认工作区：src/agent/shared/（可被 .env SHARED_RESOURCES_PATH 覆盖）
_SHARED_RESOURCES_DIR = Path(
    os.getenv("SHARED_RESOURCES_PATH")
    or Path(__file__).resolve().parent.parent / "shared"
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

    # ── 活跃工作区 ──────────────────────────────────────────

    @property
    def active_workspace(self) -> Path:
        """当前活跃工作区的根目录。

        解析顺序：
        1. 注册表 `workspaces.json` 中 `active` 字段指向的工作区路径
        2. 环境变量 `WORKSPACE_PATH` 覆盖
        3. 默认工作区 `src/agent/workspace/`（零配置回退）
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

            # 环境变量覆盖
            env_path = os.getenv("WORKSPACE_PATH", "")
            if env_path:
                p = Path(env_path)
                if p.is_dir():
                    return p.resolve()

            # 默认工作区
            return _DEFAULT_WORKSPACE_DIR.resolve()

    @property
    def active_name(self) -> str:
        """当前活跃工作区的名称。默认工作区返回 'default'。"""
        try:
            with _LOCK:
                if not self._cache_valid:
                    self._refresh_cache()
                assert self._cache is not None
                return self._cache.get("active") or "default"
        except Exception:
            return "default"

    # ── 共享资源路径（固定，不随工作区切换）──────────────────

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

    # ── 工作区级路径（随活跃工作区变化）──────────────────────

    @property
    def checkpoint_dir(self) -> Path:
        """当前工作区的 checkpoint 目录。"""
        return self.active_workspace / "checkpoint"

    @property
    def feedback_dir(self) -> Path:
        """当前工作区的 feedback 目录。"""
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
            active = self._cache.get("active") or "default"
            return [
                {**v, "name_key": k, "active": k == active}
                for k, v in workspaces.items()
            ]

    def register_workspace(self, name: str, path: str, display_name: str = "") -> dict:
        """注册一个新工作区。

        - 如果目录不存在，自动创建并初始化子目录结构
        - 如果目录已存在且非空，不覆盖已有文件，只初始化缺失的子目录
        - name 用于注册表唯一标识（如 "project-a"）
        """
        p = Path(path).resolve()
        if not p.is_dir():
            raise ValueError(f"目录不存在: {path}")

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