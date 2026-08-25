"""独立的 checkpointer 模块，供 langgraph_api 通过 LANGGRAPH_CHECKPOINTER 加载。

在 graph.json / langgraph.json 中配置:
{
  "checkpointer": {
    "backend": "custom",
    "path": "./src/agent/checkpointer_factory.py:checkpointer"
  }
}

双模式支持：
- PostgreSQL（生产）：设置 CHECKPOINT_DB_URI 环境变量（postgresql://...），
  使用 AsyncPostgresSaver，事务保障 + 多实例并发安全。
- SQLite（开发）：不设置 CHECKPOINT_DB_URI 时，使用 AsyncSqliteSaver，
  通过 from_conn_string() 异步上下文管理器创建，确保事件循环可用。

路径解析（SQLite 模式）：优先 .env CHECKPOINT_DB_PATH，否则由 WorkspaceManager 动态解析。

注意：checkpointer 是动态工作区感知的包装器，每次 __aenter__ 时重新解析路径，
切换工作区后自动连接新数据库，无需重启进程。
"""
import os
import os.path
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from agent.settings.setting import settings


def _resolve_checkpoint_path() -> str:
    """解析 checkpoint SQLite 数据库路径。"""
    base = settings.CHECKPOINT_DB_PATH
    if base:
        return os.path.join(base, "checkpoints.sqlite")
    # 由 WorkspaceManager 动态解析
    from agent.workspace_manager import get_workspace_manager
    wm = get_workspace_manager()
    wm.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    return str(wm.checkpoint_dir / "checkpoints.sqlite")


class _DynamicCheckpointer:
    """动态工作区感知的 checkpointer 包装器（SQLite 模式）。

    每次 __aenter__ 时按当前工作区解析路径，切换工作区后自动连接新数据库。
    langgraph_api 的 _yield_checkpointer() 每次调用 async with 都会触发重新解析。
    """

    def __init__(self) -> None:
        self._cm: AsyncSqliteSaver | None = None

    async def __aenter__(self) -> AsyncSqliteSaver:
        current_path = _resolve_checkpoint_path()
        self._cm = AsyncSqliteSaver.from_conn_string(current_path)
        return await self._cm.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._cm is not None:
            return await self._cm.__aexit__(exc_type, exc_val, exc_tb)
        return None


# ── 选择 checkpointer 实现 ──
_PG_URI = os.environ.get("CHECKPOINT_DB_URI", "")

if _PG_URI.startswith("postgresql://"):
    # PostgreSQL 模式（生产环境）
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    checkpointer = AsyncPostgresSaver.from_conn_string(_PG_URI)
    print(f"✅ Checkpointer: PostgreSQL ({_PG_URI.split('@')[-1] if '@' in _PG_URI else 'configured'})")
else:
    # SQLite 模式（开发环境）
    checkpointer = _DynamicCheckpointer()
