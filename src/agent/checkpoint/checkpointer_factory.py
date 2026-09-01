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

路径解析（SQLite 模式）：全局共享 checkpoint 目录（src/agent/shared/checkpoint，
可被 .env SHARED_RESOURCES_PATH 覆盖），不随工作区切换。2026-08-27 决策：
checkpoint 全局共享，切换工作区不丢历史会话。
"""
import os

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


def _resolve_checkpoint_path() -> str:
    """解析 checkpoint SQLite 数据库路径（全局共享，不随工作区切换）。"""
    from agent.workspace_manager import get_workspace_manager

    wm = get_workspace_manager()
    wm.shared_checkpoint_dir.mkdir(parents=True, exist_ok=True)
    return str(wm.shared_checkpoint_dir / "checkpoints.sqlite")


class _DynamicCheckpointer:
    """全局共享 checkpointer 包装器（SQLite 模式）。

    shared_checkpoint_dir 是固定路径（src/agent/shared/checkpoint），不随工作区
    切换；每次 __aenter__ 重新解析以反映配置变更。
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


class _PostgresCheckpointer:
    """PostgreSQL checkpointer 包装器（生产模式）。

    进入上下文时自动调用 ``setup()`` 建表——AsyncPostgresSaver 的表（checkpoints /
    checkpoint_writes / checkpoint_blobs / checkpoint_migrations）需显式 setup 创建，
    新部署的空库首次使用即自动建表，避免 ``relation "checkpoints" does not exist``。
    setup() 幂等（CREATE TABLE IF NOT EXISTS），每次会话打开执行开销可忽略。
    """

    def __init__(self, uri: str) -> None:
        self._uri = uri
        self._cm = None

    async def __aenter__(self):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        self._cm = AsyncPostgresSaver.from_conn_string(self._uri)
        saver = await self._cm.__aenter__()
        await saver.setup()  # 幂等建表（新库首次使用即自动初始化）
        return saver

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._cm is not None:
            return await self._cm.__aexit__(exc_type, exc_val, exc_tb)
        return None


# ── 选择 checkpointer 实现 ──
_PG_URI = os.environ.get("CHECKPOINT_DB_URI", "")

if _PG_URI.startswith("postgresql://"):
    # PostgreSQL 模式（生产环境）——首次使用自动建表
    checkpointer = _PostgresCheckpointer(_PG_URI)
    print(f"✅ Checkpointer: PostgreSQL ({_PG_URI.split('@')[-1] if '@' in _PG_URI else 'configured'})，首次使用自动建表")
else:
    # SQLite 模式（开发环境）
    checkpointer = _DynamicCheckpointer()
