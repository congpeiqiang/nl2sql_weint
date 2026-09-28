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

⚠ 运维注意（2026-09-08 教训）：langgraph_api 的 _checkpointer/_adapter.py 会
**跨请求复用**已建立的 PG 连接（并非每次 run 新建）。重建/重启 postgres 容器
会掐断旧连接 → 之后所有 run 与 GET /threads/{id}/state 全部报
psycopg.OperationalError "the connection is closed"。
**postgres 容器 recreate 后必须重启 langgraph-api**（v1 compose 用 stop/rm/up
三步，直接 up -d 会炸 KeyError ContainerConfig）。

⚠⚠ 运维注意（2026-09-24 生产事故，本文件已修）：上面那条「重启 langgraph-api 就好」
**不成立**，因为 langgraph 那侧的缓存是**只建不改**的：`_adapter.py:65`
`CHECKPOINTER_STACK = threading.local()` + `_adapter.py:274`
`if not hasattr(CHECKPOINTER_STACK, "inner")` —— 每个线程第一次拿 checkpointer 时建一条
连接并缓存，**此后永不重建**。所以任何一次「连接被关」都让那个线程**永久报废**
（不是死一次）：表现为一条问数约 6s 就 error、`POST /threads/{id}/state` 瞬时 500，
重启只能重置一次、**第一波 run 就再次中毒**。

而关连接的正是本文件的老写法：`checkpointer` 是**模块级单例**，`self._cm` 是**一个共享
可变槽位**，但 `get_checkpointer()` 是**每线程各调一次**（`_adapter.py:274-280`）。于是
多线程各自 `__aenter__` 互相覆盖 `self._cm`，任何一次 `__aexit__`（`_adapter.py:363
exit_checkpointer` → `stack.aclose()`，唯一调用点在 `langgraph_runtime_inmem/lifespan.py:163`
的 finally）关掉的是**最后一个** cm，即**别的线程正在用的活连接**，自己那条反而泄漏。

本文件的两层修法（缺一不可）：
  ① `_cm` 改为 `threading.local()`：各线程各管各的，绝不闭别人的连接。
  ② `_SelfHealingPostgresSaver`：用连接前先看 `conn.closed`，已死就重连 + `setup()`。
     —— ②是①的兜底：**不依赖我们对「谁关了连接」的判断正确**，postgres 重启、连接被
     GC、服务端掐断等任何来源的关闭都能自愈（代价是那一条操作多一次重连）。
自愈会打 WARNING 日志并累计计数，用来观测「到底谁在关我们的连接」。
"""
import logging
import os
import threading
from contextlib import asynccontextmanager

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

_logger = logging.getLogger(__name__)

# 自愈次数（观测用：日志里每自愈一次 +1，反推连接被谁关掉）
_heal_count = 0
_heal_lock = threading.Lock()


def _note_heal(where: str) -> None:
    """记录一次自愈（线程安全）。"""
    global _heal_count
    with _heal_lock:
        _heal_count += 1
        n = _heal_count
    _logger.warning(
        "[checkpointer] PG 连接已死，重连成功（第 %d 次自愈，触发点=%s）", n, where
    )


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

    连接上下文按线程存放（threading.local）：langgraph_api 每线程各持一个
    checkpointer，共用槽位会让 A 线程退栈时关掉 B 线程正在用的连接（见模块
    docstring 2026-09-24 事故）。
    """

    def __init__(self) -> None:
        self._local = threading.local()

    async def __aenter__(self) -> AsyncSqliteSaver:
        current_path = _resolve_checkpoint_path()
        cm = AsyncSqliteSaver.from_conn_string(current_path)
        saver = await cm.__aenter__()
        self._local.cm = cm
        return saver

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        cm = getattr(self._local, "cm", None)
        self._local.cm = None
        if cm is not None:
            return await cm.__aexit__(exc_type, exc_val, exc_tb)
        return None


_PG_SAVER_CLS = None


def _pg_saver_cls():
    """延迟构造「连接自愈」版 AsyncPostgresSaver 子类（非 PG 环境不 import psycopg）。"""
    global _PG_SAVER_CLS
    if _PG_SAVER_CLS is not None:
        return _PG_SAVER_CLS

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    # 基类实现的未绑定引用：本类里不用 zero-arg super()（避免闭包/名字解析坑）
    _base_cursor = AsyncPostgresSaver._cursor  # 已是 @asynccontextmanager 包装后的函数
    _base_setup = AsyncPostgresSaver.setup

    class _SelfHealingPostgresSaver(AsyncPostgresSaver):
        """连接被外部关掉后能自愈的 AsyncPostgresSaver。

        为什么必须自愈：langgraph_api 用线程本地缓存 saver 且**永不重建**
        （`_adapter.py:274` 的 `hasattr` 守卫），所以「连接被关」= 该线程永久报废。
        在 `_cursor` 里做一次健康检查，就把永久报废降级成「这一次操作多一次重连」。
        """

        _pg_uri: str = ""  # 由 _PostgresCheckpointer.__aenter__ 注入

        async def _ensure_conn(self, where: str) -> None:
            conn = getattr(self, "conn", None)
            if conn is not None and not getattr(conn, "closed", True):
                return  # 连接还活着，零成本快路径
            if conn is not None and hasattr(conn, "getconn"):
                # 连接池（本模块不用，但别把池换成单连接）
                _logger.warning("[checkpointer] conn 是连接池，跳过自愈（%s）", where)
                return
            uri = self._pg_uri
            if not uri:
                return  # 拿不到 URI 就维持原行为（让调用方看到原始报错）
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
            self.conn = await AsyncConnection.connect(
                uri, autocommit=True, prepare_threshold=0, row_factory=dict_row
            )
            await _base_setup(self)  # 幂等建表（重连后同样需要）
            _note_heal(where)

        @asynccontextmanager
        async def _cursor(self, *, pipeline: bool = False):
            await self._ensure_conn("_cursor")  # 在拿锁之前做，避免与 self.lock 互锁
            async with _base_cursor(self, pipeline=pipeline) as cur:
                yield cur

    _PG_SAVER_CLS = _SelfHealingPostgresSaver
    return _PG_SAVER_CLS


class _PostgresCheckpointer:
    """PostgreSQL checkpointer 包装器（生产模式）。

    进入上下文时自动调用 ``setup()`` 建表——AsyncPostgresSaver 的表（checkpoints /
    checkpoint_writes / checkpoint_blobs / checkpoint_migrations）需显式 setup 创建，
    新部署的空库首次使用即自动建表，避免 ``relation "checkpoints" does not exist``。
    setup() 幂等（CREATE TABLE IF NOT EXISTS），每次会话打开执行开销可忽略。

    上下文管理器**按线程存放**（`threading.local`），因为 langgraph_api 是每线程各
    建一个 checkpointer；共用一个槽位会让一个线程的 `__aexit__` 关掉另一个线程正在
    用的活连接（2026-09-24 生产事故根因，详见模块 docstring）。
    """

    def __init__(self, uri: str) -> None:
        self._uri = uri
        self._local = threading.local()

    async def __aenter__(self):
        cm = _pg_saver_cls().from_conn_string(self._uri)
        saver = await cm.__aenter__()
        saver._pg_uri = self._uri  # 供自愈重连
        # 先登记再 setup：setup 失败也不留半开的上下文
        self._local.cm = cm
        try:
            await saver.setup()  # 幂等建表（新库首次使用即自动初始化）
        except Exception:
            self._local.cm = None
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass
            raise
        return saver

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        cm = getattr(self._local, "cm", None)
        self._local.cm = None
        if cm is not None:
            return await cm.__aexit__(exc_type, exc_val, exc_tb)
        return None


# ── 选择 checkpointer 实现 ──
_PG_URI = os.environ.get("CHECKPOINT_DB_URI", "")

if _PG_URI.startswith("postgresql://"):
    # PostgreSQL 模式（生产环境）——首次使用自动建表
    checkpointer = _PostgresCheckpointer(_PG_URI)
    # 注意：不用 emoji——Windows GBK 控制台 print 会 UnicodeEncodeError 炸 import
    # （生产容器 LANG=C.UTF-8 无碍，但宿主机 GBK 终端设 URI 测 PG 模式会中招）。
    # 只打 @ 后半段，不泄密码。
    print(f"[OK] Checkpointer: PostgreSQL ({_PG_URI.split('@')[-1] if '@' in _PG_URI else 'configured'})，首次使用自动建表，连接自愈已启用")
else:
    # SQLite 模式（开发环境）
    checkpointer = _DynamicCheckpointer()
