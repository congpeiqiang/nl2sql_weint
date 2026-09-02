"""workspace_manager 包 — 工作区管理。

重导出保持向后兼容：所有 `from agent.workspace_manager import ...` 无需修改。
"""

from agent.workspace_manager.manager import (  # noqa: F401
    WorkspaceManager,
    get_workspace_manager,
)