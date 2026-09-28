"""workspace_manager 包 — 工作区路径统一解析（单工作区，2026-09-25 起）。

重导出保持向后兼容：所有 `from agent.workspace_manager import ...` 无需修改。
（`WorkspaceBusyError` 已随多工作区切换机件一并删除。）
"""

from agent.workspace_manager.manager import (  # noqa: F401
    OFFLINE_EXPERIMENT_DIR_NAME,
    WorkspaceManager,
    get_workspace_manager,
)
