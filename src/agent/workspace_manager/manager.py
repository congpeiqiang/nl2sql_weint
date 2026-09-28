"""WorkspaceManager — 统一工作区路径解析，替换所有 `Path(__file__).resolve().parents[N]` 硬编码。

设计（2026-09-25 起：**单工作区**）：
- **只有一份工作区**，路径钉死在 `<AGENT_DATA_ROOT>/workspace`（未配 AGENT_DATA_ROOT
  的部署回退仓库内 `src/agent/workspace`）。
- 此前「多工作区 + `workspaces.json` 注册表 + 切换」的整套机件已删除：「工作区」是
  **部署级单值**（切一次全部署生效），**不提供任何用户隔离**（用户隔离靠
  `grants(db_name)` + `thread_owner` + `report_owner` 账本），却带来两个真问题 ——
  ① 切换时在跑的 run 后续每次路径解析都落到新目录而 run 自己不知道；② 前端入口对
  普通用户 403。多项目诉求由 `db_config.json`（一个文件装 N 个库）与语义库根
  （Wren 项目直接放工作区下）天然满足，不需要多工作区。
- 工作区目录含 db_config/语义库/报告/中间数据；memory/、skills/、model_config.json
  全局共享，位于共享资源根（`_SHARED_RESOURCES_DIR`）。
- **外部基础目录**：`.env` 配置 `AGENT_DATA_ROOT`（项目外目录）后，shared 与工作区
  统一放到 `<AGENT_DATA_ROOT>/{shared,workspace}`，代码根 src/agent/ 彻底退出 VFS
  （main_agent/nl2sql_agent 的 `/` 兜底路由改为指向 data_root）。
  首次运行若外部目录缺失，自动从仓库内置 `src/agent/{shared,workspace}` 原子拷贝种子
  （注意：`src/agent/workspace` 已被发版 tar 排除且仓库内通常不存在，所以工作区的
  初次落盘实际靠 `_init_workspace_dirs` 的启动初始化）。未配置时回退仓库内
  `src/agent/{shared,workspace}`（兼容现有部署）。

隔离矩阵：
    工作区内：    db_config.json, semantic/, report/, tmp/,
                  nl2sql_process_data/, large_tool_results/
    全局共享：    memory/, skills/, model_config.json, checkpoint/,
                  trace/, feedback/, fts.sqlite

用法：
    from agent.workspace_manager import get_workspace_manager

    wm = get_workspace_manager()
    print(wm.active_workspace)       # 工作区目录（= <AGENT_DATA_ROOT>/workspace）
    print(wm.checkpoint_dir)         # 该工作区的 checkpoint 目录（仅供展示）
    print(wm.shared_memory_dir)      # 共享 memory 目录（固定）
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# 必须先加载 .env 再读 AGENT_DATA_ROOT / SHARED_RESOURCES_PATH 等模块级 env——
# 否则 import 顺序不同时（如 eval 脚本直连、Docker env 注入）外部基础目录会静默回退。
# 默认 override=False：Docker 已注入的 env（如 AGENT_DATA_ROOT=/app/data）不被 .env 覆盖。
load_dotenv()

_logger = logging.getLogger(__name__)

# 仓库内置种子目录锚点（随 git 分发）：manager.py 在 workspace_manager/ 子目录，
# 上两层到 src/agent/。shared 与 workspace 的仓库种子从这里拷贝到外部基础目录。
_REPO_AGENT_DIR = Path(__file__).resolve().parent.parent

# 外部基础目录（项目外，.env AGENT_DATA_ROOT 配置）——shared 与工作区都放在这里。
# 配置后：shared → <AGENT_DATA_ROOT>/shared，工作区 → <AGENT_DATA_ROOT>/workspace。
# 为空 = 未配置，回退仓库内 src/agent/（旧行为，兼容现有部署）。
_DATA_ROOT = os.getenv("AGENT_DATA_ROOT", "").strip()

# 工作区目录（唯一）：AGENT_DATA_ROOT/workspace（配置时）→ src/agent/workspace（回退）。
# 双分支刻意保留：生产/开发都配了 AGENT_DATA_ROOT，而未配的老部署不需要迁移数据。
_DEFAULT_WORKSPACE_DIR = (
    Path(_DATA_ROOT) / "workspace" if _DATA_ROOT else _REPO_AGENT_DIR / "workspace"
)

# 共享资源根目录（memory/、skills/、model_config.json 的父目录）
# 优先级：SHARED_RESOURCES_PATH env → AGENT_DATA_ROOT/shared → src/agent/shared
_SHARED_RESOURCES_DIR = Path(
    os.getenv("SHARED_RESOURCES_PATH")
    or (Path(_DATA_ROOT) / "shared" if _DATA_ROOT else _REPO_AGENT_DIR / "shared")
)

# 离线实验产物根目录名（<data_root>/offline_experiment/，见 offline_experiment_dir）。
# 单一来源：skills_versioning / prompt_versioning 的物化落点与 VFS 路径都从这里取名，
# 避免字面量在四处漂移。2026-09-12 由 data_root 根下平铺迁入（旧目录不自动迁移）。
OFFLINE_EXPERIMENT_DIR_NAME = "offline_experiment"


class WorkspaceManager:
    """统一工作区路径解析器。

    所有组件通过 `get_workspace_manager()` 获取单例，按需解析路径。
    路径是**常量**（`active_workspace` 每次返回同一个目录），所以不存在"切换后
    各处路径不一致"的问题 —— 这也是删掉多工作区机件的主要收益。
    """

    def __init__(self) -> None:
        self._seed_data_root_once()  # 首次运行：外部基础目录缺失时从仓库种子初始化
        self._init_workspace_dirs(_DEFAULT_WORKSPACE_DIR)  # 缺什么补什么（幂等）

    # ── 首次运行种子初始化 ───────────────────────────────────

    def _seed_data_root_once(self) -> None:
        """首次运行初始化：AGENT_DATA_ROOT 配置时，若外部 shared/workspace 缺失，
        从仓库内置 `src/agent/shared`、`src/agent/workspace` 原子拷贝种子。

        目的：.env 里只配 `AGENT_DATA_ROOT` 即可跑起来（本地全新克隆 / Docker 首启），
        无需手工拷贝。目录已存在则跳过（此后运行时数据以外部目录为准，仓库种子不再改动）。

        原子性：先拷到同名 `.seed_tmp`，成功后 `os.replace` 改名，避免拷贝中断留下
        半成品目录导致后续运行误判「已存在」。
        """
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

    # ── 工作区（唯一）───────────────────────────────────────

    @property
    def active_workspace(self) -> Path:
        """工作区根目录 —— 常量，不再有任何解析优先级。

        历史：曾按「注册表 active → `WORKSPACE_PATH` env → 默认目录」三档解析。
        2026-09-25 起只保留默认工作区：`AGENT_DATA_ROOT/workspace`（未配 data root
        时 `src/agent/workspace`）。盘上残留的 `workspaces.json` 与 `WORKSPACE_PATH`
        env 一律**不再被读取**（见 `scripts/verify_workspace_pinned.py` 的负对照）。
        """
        return _DEFAULT_WORKSPACE_DIR.resolve()

    @property
    def active_name(self) -> str:
        """工作区名称。单工作区后恒为 `"default"`。

        保留这个属性是因为有读者把它写进 run 的 configurable / Langfuse metadata
        （`middlewares/deepagents_async_config_patch.py`、`api/langfuse_metadata.py`），
        它们只把它当"这本 run 属于哪个工作区"的标签用。
        """
        return "default"

    # ── 共享资源路径（固定，不随工作区变化）──────────────────

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
        """共享 memory 目录（唯一工作区也会变，但 memory 始终共享）。"""
        return _SHARED_RESOURCES_DIR / "memory"

    @property
    def shared_skills_dir(self) -> Path:
        """共享 skills 目录。"""
        return _SHARED_RESOURCES_DIR / "skills"

    @property
    def shared_model_config_path(self) -> Path:
        """共享 model_config.json（默认路径，可被工作区内的同名文件覆盖）。"""
        return _SHARED_RESOURCES_DIR / "model_config.json"

    # ── 全局共享数据路径 ─────────────────────────────────────
    # 2026-08-27 决策：checkpoint/trace/fts/feedback 全局共享（不按工作区切）。
    # 数据锚点 = _SHARED_RESOURCES_DIR（可被 .env SHARED_RESOURCES_PATH 覆盖）。
    # 注意不能拿工作区目录当共享锚点：它可能被 retention 清理。

    @property
    def offline_experiment_dir(self) -> Path:
        """离线实验产物根目录（`<data_root>/offline_experiment`）。

        收纳离线 A/B 实验的**版本物化缓存**：`skill_refs/`（SKILLS_REF → git archive /
        远程浅克隆）与 `prompt_refs/`（prompt label → 版本号 + 正文快照）。放 data_root
        下、而非 `shared/` 内：落 `FILE_PERMISSIONS` 的 `deny /**` 与
        `execute_guard` 的 allow 之外，在线 agent 的 read_file/ls/glob/grep 与
        execute 都摸不到（与 `eval_runs/` 同一封堵思路）。

        注意：实验的 run 产物（manifest/status/out/arms.json）**不在这里**，仍在
        `<active_workspace>/eval/experiment_runs/`。
        """
        return self.data_root / OFFLINE_EXPERIMENT_DIR_NAME

    @property
    def shared_data_root(self) -> Path:
        """全局共享数据根目录（锚定 _SHARED_RESOURCES_DIR）。"""
        return _SHARED_RESOURCES_DIR

    @property
    def shared_checkpoint_dir(self) -> Path:
        """全局共享 checkpoint 目录。"""
        return _SHARED_RESOURCES_DIR / "checkpoint"

    @property
    def shared_trace_db(self) -> Path:
        """全局共享 trace 库路径（独立子目录存放 .sqlite/-shm/-wal）。"""
        return _SHARED_RESOURCES_DIR / "trace" / "traces.sqlite"

    @property
    def shared_feedback_dir(self) -> Path:
        """全局共享 feedback 目录。"""
        return _SHARED_RESOURCES_DIR / "feedback"

    # ── 工作区级路径 ─────────────────────────────────────────

    @property
    def checkpoint_dir(self) -> Path:
        """工作区的 checkpoint 目录（仅供展示，实际存储走 shared_checkpoint_dir）。"""
        return self.active_workspace / "checkpoint"

    @property
    def feedback_dir(self) -> Path:
        """工作区的 feedback 目录（仅供展示，实际存储走 shared_feedback_dir）。"""
        return self.active_workspace / "feedback"

    @property
    def db_config_path(self) -> Path:
        """工作区的 db_config.json 路径（一个文件装 N 个库）。"""
        return self.active_workspace / "db_config.json"

    @property
    def model_config_path(self) -> Path:
        """工作区的 model_config.json 路径（优先工作区非空配置，回退共享）。"""
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
        """工作区的 report 目录。"""
        return self.active_workspace / "report"

    @property
    def tmp_dir(self) -> Path:
        """工作区的 tmp 目录。"""
        return self.active_workspace / "tmp"

    @property
    def process_data_dir(self) -> Path:
        """工作区的 nl2sql_process_data 目录。"""
        return self.active_workspace / "nl2sql_process_data"

    @property
    def large_tool_results_dir(self) -> Path:
        """工作区的 large_tool_results 目录。"""
        return self.active_workspace / "large_tool_results"

    @property
    def semantic_dir(self) -> Path:
        """语义库根目录（即工作区根，Wren 项目直接放这里）。"""
        return self.active_workspace

    def _init_workspace_dirs(self, root: Path) -> None:
        """初始化工作区子目录结构（缺什么补什么，幂等；不覆盖已有文件）。

        启动时由 `__init__` 调用 —— 单工作区后这是工作区落盘的**唯一**初始化点
        （老代码里只被 `register_workspace` 调用，而 CRUD 已删）。全新部署首启就靠它
        建出目录结构与空 `db_config.json`。
        """
        dirs = [
            "checkpoint",
            "feedback",
            "report",
            "tmp",
            "nl2sql_process_data",
            "large_tool_results",
        ]
        created: list[str] = []
        for d in dirs:
            p = root / d
            if not p.is_dir():
                p.mkdir(parents=True, exist_ok=True)
                created.append(d)

        # 初始化空 db_config.json（如不存在）
        db_config = root / "db_config.json"
        if not db_config.exists():
            db_config.write_text(
                json.dumps({"version": 1, "databases": []}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            created.append("db_config.json")

        # 初始化空 model_config.json（如不存在，可选覆盖全局配置）
        model_config = root / "model_config.json"
        if not model_config.exists():
            model_config.write_text(
                json.dumps({"version": 1, "active": "", "providers": []}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            created.append("model_config.json")

        if created:
            _logger.info("[workspace] 初始化工作区目录结构 %s（新建: %s）", root, created)


# 单例
_default_manager: Optional[WorkspaceManager] = None


def get_workspace_manager() -> WorkspaceManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = WorkspaceManager()
    return _default_manager
