"""
MCP 工具管理模块 - 支持主智能体和子智能体工具独立加载
"""

import json
import logging
import os
import sys
import threading
import time
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Dict, Any, Optional, Tuple
from contextlib import redirect_stdout, redirect_stderr
import io
import warnings

# 设置编码
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"

# 禁用警告和日志
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)
for name in ["langchain_mcp_adapters", "mcp", "wren", "httpx", "urllib3"]:
    logging.getLogger(name).setLevel(logging.WARNING)

from langchain_mcp_adapters.client import MultiServerMCPClient
from agent.utils.path_resolver import wrap_tool
from agent.settings.setting import settings

_logger = logging.getLogger(__name__)

# 仓库根目录（src/agent/tools/mcp_tool.py → parents[3] 即仓库根）
# 用于 db_mcp_server 子进程的 PYTHONPATH，保证子进程能 import mcp_server 包
_REPO_ROOT = Path(__file__).resolve().parents[3]

# ── 全局状态 ────────────────────────────────────────────────────
_main_tools: Optional[List] = None  # 主智能体工具 (mcp-server-chart)
_sub_tools: Optional[List] = None  # 子智能体工具 (wrenai)
_all_tools: Optional[List] = None  # 所有工具（向后兼容）
_tools_loaded = False
_mcp_server_results: Dict[str, str] = {}  # server_name → "ok" | error message
_mcp_server_tool_counts: Dict[str, int] = {}  # server_name → 成功加载的工具数（失败=0）
_skipped_servers: set = set()  # 被有意跳过的 server（无配置 → 不加载，区别于「有配置但加载失败」）

# 关键 server 前缀：语义层（wrenai_*）与 SQL 直连执行（dbmcp）——缺任何一个，
# 建模库查询整体失效（2026-09-08 事故：wrenai_WIT 加载失败被总数"✅ 预检通过: 20"
# 掩盖，子 agent 0 语义工具，查询退化成 10 分钟撞墙+僵尸续跑）。
# 关键 server 失败 → 启动预检拒绝启动（MCP_ALLOW_DEGRADED=1 可显式降级）。
_CRITICAL_SERVER_PREFIXES = ("wrenai_", "dbmcp")


class MCPToolsLoadError(RuntimeError):
    """MCP 工具加载失败"""
    pass


# ── 子进程 UTF-8 环境变量 ──────────────────────────────────
_UTF8_ENV = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
}


# ── 1. 核心加载函数 ────────────────────────────────────────
def _load_mcp_servers(servers: Dict[str, Any], server_type: str = "unknown") -> List:
    """
    通用的 MCP 服务器加载函数

    Args:
        servers: MCP 服务器配置字典
        server_type: 日志标识 ("main" | "sub" | "unknown")

    Returns:
        加载的工具列表
    """
    all_tools = []

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        for name, config in servers.items():
            # 每 server 两次尝试（子进程偶发起步慢/瞬断，一次重试消掉大部分误报；
            # 持久性故障如 target/mdl.json 缺失重试无济于事，照常记 FAILED）
            wrapped = None
            last_msg = ""
            for attempt in (1, 2):
                try:
                    suffix = "" if attempt == 1 else f"（重试 {attempt}/2）"
                    print(f"  ⏳ [{server_type}] 连接 {name}{suffix}...", flush=True)

                    # 捕获 MCP 客户端的冗余输出
                    f = io.StringIO()
                    with redirect_stdout(f), redirect_stderr(f):
                        client = MultiServerMCPClient({name: config}, tool_name_prefix=True)
                        tools = loop.run_until_complete(
                            asyncio.wait_for(
                                client.get_tools(),
                                timeout=60.0
                            )
                        )

                    # Wren 语义层工具注入 project_path / db_name，供 wrap_tool 快速路径使用
                    # （get_context / recall_queries 在主进程直接调用 wren API，
                    #  绕过 MCP 子进程 + MemoryStore 420MB 嵌入模型加载）
                    # db_name 与 project_path **同源**（同一个探测器的 discover 结果，也就是
                    # db_config.json 里的 name）—— 检索层据此给条目分桶，绝不许按目录名拼。
                    if name.startswith("wrenai_"):
                        try:
                            from agent.utils.semantic_db import get_detector, wrenai_server_name
                            _det = get_detector()
                            _injected = 0
                            for _t in tools:
                                for _db in _det.discover():
                                    _proj = _det.project_path_for(_db)
                                    if _proj and name == wrenai_server_name(_db):
                                        _t._wren_project_path = str(_proj)
                                        _t._wren_db_name = str(_db)
                                        _injected += 1
                                        break
                            if _injected > 0:
                                _logger.info("[MCP] %s: 已注入 _wren_project_path 到 %d 个工具", name, _injected)
                            else:
                                _logger.warning("[MCP] %s: 未匹配到任何数据库，fast-path 将不可用", name)
                        except Exception as e:
                            _logger.warning("[MCP] %s: _wren_project_path 注入失败（%s: %s），fast-path 将不可用", name, type(e).__name__, e)

                    wrapped = [wrap_tool(t) for t in tools]
                    break
                except asyncio.TimeoutError:
                    last_msg = "连接超时 (60秒)"
                except Exception as e:
                    last_msg = f"{type(e).__name__}: {e}"
                if attempt == 1:
                    print(f"  ⚠️ [{server_type}] {name}: {last_msg} —— 3 秒后重试", flush=True)
                    time.sleep(3)

            if wrapped is None:
                _mcp_server_results[name] = last_msg
                _mcp_server_tool_counts[name] = 0
                print(f"  ❌ [{server_type}] {name}: FAILED — {last_msg}（含重试共 2 次尝试）", flush=True)
            else:
                all_tools.extend(wrapped)
                _mcp_server_results[name] = "ok"
                _mcp_server_tool_counts[name] = len(wrapped)
                print(f"  ✅ [{server_type}] {name}: {len(wrapped)} tools loaded", flush=True)
    finally:
        loop.close()

    return all_tools


# ── 2. 获取主智能体配置 ──────────────────────────────────
def _get_main_server_config() -> Dict[str, Any]:
    """获取主智能体的 MCP 服务器配置（ECharts MCP）"""
    # ECharts MCP（本地全局安装，bin 命令 mcp-echarts，避免 npx 下载最新版）
    return {
        "mcp-server-echarts": {
            "transport": "stdio",
            "command": "mcp-echarts",
            "args": [],
            "env": {
                **os.environ, **_UTF8_ENV,
            },
        },
    }


def wren_conn_dict(db_name: str) -> Optional[Dict[str, Any]]:
    """db_config → wren 连接字典（``{"datasource", "host", ...}`` 扁平形态）。

    **唯一真源**：这里给 Wren profile 用的字典，与 ``agent/utils/wren_plan`` 复算
    物理 SQL 时建的引擎用的是同一份 —— 否则报告里复算出的方言/连接可能与线上
    实际执行的不是一回事（不带连接时 wren 会退化成 ``DATE_DIFF()`` 这类目标库
    没有的函数，见 docs/SQL通道-SQL生成实现思路方案.md §五坑 1）。

    拿不到配置时抛异常（连接信息是本地配置读取，不是网络调用），由调用方决定
    是「回退全局 active profile」还是「不出物理 SQL 节」。
    """
    from mcp_server.db_mcp_server.db.core.db_config_store import get_store

    cfg = get_store().get(db_name)
    conn: Dict[str, Any] = {
        "datasource": cfg.db_type,
        "host": cfg.host,
        "port": cfg.port,
        "database": cfg.database,
        "user": cfg.user,
    }
    if cfg.password:
        conn["password"] = cfg.password
    return conn


# ── 3. 子智能体 server 条目与热更新注册表 ────────────────
@dataclass
class _SubEntry:
    """一个 wrenai_<库> / dbmcp server 的注册表条目。

    ``fingerprint`` 只覆盖「变了必须重连 server 才生效」的东西（server 名 +
    语义库路径 + wren 可执行文件路径）；**连接信息不在其中**——它进的是 Wren
    profile 文件，每次工具调用新起的 MCP 子进程会重新读，所以改密码/换 host
    天然即时生效，不需要重载工具。
    """

    server_name: str
    config: Dict[str, Any]
    fingerprint: str = ""
    db_name: Optional[str] = None  # dbmcp 无归属库（按工具入参 db_name 路由）
    tools: List = field(default_factory=list)
    status: str = "pending"  # "ok" 或失败原因
    loaded_at: float = 0.0


def _has_any_database() -> bool:
    """检查是否有任何数据库配置（db_config.json 或 .env DB_* 变量）。

    零配置部署时 db_config_store 和 .env 都没有数据库 → 跳过 dbmcp（不启动
    子进程），避免 ValueError 被当成 critical 失败阻断服务。
    """
    # 1. db_config_store（前端 UI 写入的运行时配置）
    try:
        from mcp_server.db_mcp_server.db.core.db_config_store import get_store
        store = get_store()
        if store.get_all_decrypted():
            return True
    except Exception:  # noqa: BLE001
        pass
    # 2. .env 回退（历史配置方式）
    try:
        return bool(settings.get_databases())
    except Exception:  # noqa: BLE001
        return False


def _dbmcp_config() -> Dict[str, Any]:
    """db_mcp_server 直连通道的 server 配置（NL2SQL_DBMCP_ENABLED 控制）。"""
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [
            "-m", "mcp_server.db_mcp_server.db.db_server",
            "--transport", "stdio",
        ],
        "env": {
            **os.environ, **_UTF8_ENV,
            # 保证子进程能 import mcp_server 包（父进程 src 不一定在 PYTHONPATH）
            "PYTHONPATH": os.pathsep.join(
                filter(None, [str(_REPO_ROOT / "src"), os.environ.get("PYTHONPATH", "")])
            ),
        },
    }


def _build_sub_entry(db_name: str) -> Optional[_SubEntry]:
    """为一个已建模库构造 wrenai server 条目；未关联语义库则返回 None。

    副作用：把 db_config 的连接信息同步进 Wren profile（幂等覆写，不切换 active）
    —— 见下文 profile 注释。每次对账都会重跑，所以连接信息变更即时生效。
    """
    from agent.utils.semantic_db import get_detector, wrenai_server_name

    project = get_detector().project_path_for(db_name)
    if not project:
        return None
    server_name = wrenai_server_name(db_name)

    # 从 db_config 同步连接信息到 Wren profile，通过 --profile 直传。
    # 绕过 ~/.wren/profiles.yml 的全局 active profile 兜底——否则未在
    # wren_project.yml 中 pin profile 的项目会继承全局 active profile，
    # 导致连接到错误的数据源（如 active=chinook 指向 clickhouse）。
    # profile 命名 wren_mcp_<db_name>，add_profile 不切换 active（已有 active
    # 时保留），幂等覆写（连接信息变了自动更新）。
    args = ["serve", "mcp", "--project", project]
    try:
        from wren.profile import add_profile

        add_profile(f"wren_mcp_{server_name}", wren_conn_dict(db_name))
        args.extend(["--profile", f"wren_mcp_{server_name}"])
    except Exception as e:  # noqa: BLE001
        _logger.warning(
            "[mcp] %s: 同步 Wren profile 失败 (%s)，回退全局 active profile",
            db_name, e,
        )

    return _SubEntry(
        server_name=server_name,
        db_name=db_name,
        fingerprint=f"{server_name}|{project}|{settings.WREN_BIN_PATH}",
        config={
            "transport": "stdio",
            "command": settings.WREN_BIN_PATH,
            "args": args,
            "env": {
                **os.environ, **_UTF8_ENV,
                "WREN_LOG_LEVEL": "ERROR",
                "PYTHONUNBUFFERED": "1",
                # Wren recall_queries 检索后端（grep / lancedb），由 settings 控制。
                # 默认 grep（token-overlap，毫秒级），避免 LanceDBIndex 每次重建
                # MemoryStore 加载 420MB 嵌入模型的开销，以及 knowledge/sql/ 为空时
                # LanceDB 空表 search hang 至超时的问题。
                "WREN_MEMORY_BACKEND": settings.WREN_MEMORY_BACKEND,
            },
        },
    )


def _get_sub_server_config() -> Dict[str, Any]:
    """获取子智能体的 MCP 服务器配置（按当前 db_config + 语义库实时构建）。

    两路通道（LLM 按工具描述选择）：
    - ``wrenai_<库名>``：语义层——每个已建模库（db_config 配了 wren_project，
      或默认 WREN_PROJECT_PATH 项目里建模的库）一个专属 server，工具前缀
      ``wrenai_<库名>_``（如 ``wrenai_imdb_run_sql``）。
    - ``dbmcp``：db_mcp_server 直连（按 db_name 路由到对应库的 runner），
      工具前缀 ``dbmcp_``。由 .env 的 ``NL2SQL_DBMCP_ENABLED`` 控制（默认开）。

    **本函数是"配置视图"，不是"已加载的工具视图"**：只按 db_config + 语义库
    现算 server 配置，不碰工具单例。运行期增删库即时生效靠 ``_sub_entries``
    注册表（见下方 §3.1 与 DynamicMCPToolsMiddleware）。

    冲突兜底：不同中文库折到同一 ASCII 骨架（去哈希后同骨架，极罕见）时告警
    跳过，避免 tool 前缀歧义。处置：给库名补 ASCII 区分段（如 WIT库A/WIT库B）。
    """
    from agent.utils.semantic_db import get_detector

    servers: Dict[str, Any] = {}
    for db_name in sorted(get_detector().discover()):
        entry = _build_sub_entry(db_name)
        if entry is None:
            continue
        if entry.server_name in servers:
            _logger.warning(
                "[mcp] wrenai server 名冲突: %r 跳过 %r（去哈希后同骨架；"
                "请在 db_config 库名里补 ASCII 区分段）",
                entry.server_name, db_name,
            )
            continue
        servers[entry.server_name] = entry.config

    if settings.NL2SQL_DBMCP_ENABLED:
        servers["dbmcp"] = _dbmcp_config()

    return servers


# ── 3.1 工具注册表（运行期新增/删除库免重启的枢纽） ──────
# 为什么需要它：``ToolNode.tools_by_name`` 在 graph 编译时固化（create_deep_agent
# 在 import 期拿到 sub_tools 列表），进程起来后新库的工具进不了那条通道。官方
# 的动态工具通道是「middleware 的 wrap_tool_call 里 request.override(tool=...)」
# + 「wrap_model_call 里 request.override(tools=...)」（langchain agents/factory.py
# 的 DYNAMIC_TOOL_ERROR_TEMPLATE；本仓已有 8 个 wrap_tool_call 中间件，未知工具的
# 校验早已关掉，见 factory.py 的 has_wrap_tool_call 判定）。本注册表就是那条通道
# 的数据源：**进程内唯一权威的工具清单**，快照给模型、索引给执行侧。
#
# 读方（每次模型调用 / 每次工具调用）走无锁快照；写方（启动加载、后台对账、
# 写路径同步加载）持 ``_sub_entries_lock``（可重入：对账里会嵌 ensure）。
_sub_entries: Dict[str, _SubEntry] = {}
_sub_entries_lock = threading.RLock()
_sub_snapshot: List = []  # 已过滤的工具对象（模型可见 + 可执行）
_sub_lookup: Dict[str, Any] = {}  # tool_name → 工具对象（执行侧解析用）
_sub_registry_warmed = False  # 是否完成过至少一次全量对账（未预热前执行侧 fail-open）
_sub_tool_filter: Optional[Callable[[str], bool]] = None  # graph 注册的 YAML 工具名过滤器
_sub_reload_thread: Optional[threading.Thread] = None
_reload_thread_lock = threading.Lock()


def _apply_sub_tool_filter(tools: List) -> List:
    """按 graph 注册的过滤器（YAML ``tools:`` 前缀模式）裁剪工具名单。

    过滤器由 ``nl2sql_agent`` 在建图时用 ``set_sub_tool_filter`` 注册——否则
    运行期新加载的工具会绕过 YAML 的 ``tools:`` 白名单（建图那条路是
    ``if pattern in tool_name``）。过滤器异常 → fail-open 保留（宁可多给工具，
    不可把一个健康的库弄成无工具）。
    """
    fn = _sub_tool_filter
    if fn is None:
        return list(tools)
    kept = []
    for tool in tools:
        try:
            keep = fn(getattr(tool, "name", "") or "")
        except Exception as e:  # noqa: BLE001
            _logger.warning("[mcp] 工具名过滤器异常，放行 %r: %s", getattr(tool, "name", "?"), e)
            keep = True
        if keep:
            kept.append(tool)
    return kept


def _rebuild_sub_snapshot() -> None:
    """在锁内调用：由注册表重建只读快照与名字索引。"""
    global _sub_snapshot, _sub_lookup
    tools: List = []
    for entry in _sub_entries.values():
        tools.extend(entry.tools)
    _sub_snapshot = _apply_sub_tool_filter(tools)
    _sub_lookup = {getattr(t, "name", ""): t for t in _sub_snapshot}


def set_sub_tool_filter(fn: Optional[Callable[[str], bool]]) -> None:
    """注册「工具名 → 是否启用」过滤器（建图时由 nl2sql_agent 调用）。"""
    global _sub_tool_filter
    _sub_tool_filter = fn
    with _sub_entries_lock:
        _rebuild_sub_snapshot()


def sub_tools_snapshot() -> List:
    """当前生效的子智能体工具快照（无锁，返回浅拷贝）。"""
    return list(_sub_snapshot)


def sub_lookup_snapshot() -> Dict[str, Any]:
    """当前生效的工具名索引快照（无锁，返回浅拷贝）。"""
    return dict(_sub_lookup)


def lookup_sub_tool(name: str) -> Optional[Any]:
    """按工具名取当前注册表里的工具实例；不存在返回 None。"""
    return _sub_lookup.get(name)


def sub_registry_warmed() -> bool:
    """注册表是否完成过全量对账。

    未预热（如离线单测只 import 了本模块、或启动加载尚未跑完）时执行侧
    **fail-open**：不因"注册表里没有"而判定工具已下线，避免把健康工具误杀。
    """
    return _sub_registry_warmed


def _load_entry(entry: _SubEntry) -> str:
    """按条目 config 加载工具（**阻塞**，秒级；不抛异常）。返回状态字符串。

    必须在没有事件循环的线程里调用：``_load_mcp_servers`` 内部
    ``asyncio.new_event_loop()``，在运行中的 loop 里会直接炸。
    """
    try:
        tools = _load_mcp_servers({entry.server_name: entry.config}, "sub")
    except Exception as e:  # noqa: BLE001
        tools = []
        _mcp_server_results[entry.server_name] = f"加载异常: {e}"
        _logger.warning("[mcp] %s 加载异常: %s", entry.server_name, e)
    entry.tools = list(tools)
    entry.loaded_at = time.time()
    entry.status = _mcp_server_results.get(entry.server_name, "ok")
    return entry.status


def _prune_orphan_entries() -> List[str]:
    """删掉已从 db_config / 语义库里消失的 wrenai 条目（改名、删库的余孽）。"""
    from agent.utils.semantic_db import get_detector

    try:
        known = set(get_detector().discover())
    except Exception as e:  # noqa: BLE001
        _logger.warning("[mcp] 对账跳过（discover 失败）: %s", e)
        return []
    dropped = [
        n for n, e in _sub_entries.items()
        if e.db_name is not None and e.db_name not in known
    ]
    for n in dropped:
        del _sub_entries[n]
    return dropped


def refresh_sub_entries(load: bool = True) -> Dict[str, Any]:
    """全量对账：把注册表对齐到当前 db_config + 语义库（新增/改名/删除/重指向）。

    重新加载只发生在两种情形：
    - ``fingerprint`` 变了（server 名 / 语义库路径 / wren 可执行路径）；
    - **上次加载失败**（``status != "ok"``）——必须重试，见下方注释。

    语义库**内容**更新（git pull + 重新构建）自身不触发重载：每次工具调用新起的
    MCP 子进程会重新读 ``target/mdl.json``，本来就即时生效。

    ``load=False`` 时只做增删对齐、不加载（供离线测试与"先摘掉再补上"场景）。

    Returns:
        {"added": [...], "changed": [...], "retried": [...], "removed": [...],
         "loaded": {name: status}, "counts": {name: 工具数}}
    """
    from agent.utils.semantic_db import get_detector

    global _sub_registry_warmed
    with _sub_entries_lock:
        expected: Dict[str, _SubEntry] = {}
        for db_name in sorted(get_detector().discover()):
            entry = _build_sub_entry(db_name)
            if entry is None:
                continue
            if entry.server_name in expected:
                _logger.warning(
                    "[mcp] wrenai server 名冲突: %r 跳过 %r（去哈希后同骨架；"
                    "请在 db_config 库名里补 ASCII 区分段）",
                    entry.server_name, db_name,
                )
                continue
            expected[entry.server_name] = entry

        _skipped_servers.discard("dbmcp")  # 每次对账重新判定
        if settings.NL2SQL_DBMCP_ENABLED:
            if _has_any_database():
                dbmcp = _SubEntry(
                    server_name="dbmcp", config=_dbmcp_config(), fingerprint="dbmcp",
                )
                current = _sub_entries.get("dbmcp")
                if current is not None:  # 静态 server：沿用已加载的工具，不重复起进程
                    dbmcp.tools = current.tools
                    dbmcp.status = current.status
                    dbmcp.loaded_at = current.loaded_at
                expected["dbmcp"] = dbmcp
            else:
                _skipped_servers.add("dbmcp")
                _logger.info("[mcp] 未配置任何数据库，跳过 dbmcp 加载")

        removed = [n for n in list(_sub_entries) if n not in expected]
        for n in removed:
            del _sub_entries[n]

        added: List[str] = []
        changed: List[str] = []
        retried: List[str] = []
        to_load: List[_SubEntry] = []
        for name, entry in expected.items():
            current = _sub_entries.get(name)
            if current is None:
                added.append(name)
                to_load.append(entry)
            elif current.fingerprint != entry.fingerprint:
                changed.append(name)
                to_load.append(entry)
            elif current.status != "ok":
                # 上次加载失败的条目**必须重试**：指纹不含构建产物（也不含连接信息，
                # 那是有意的），所以「先建空语义库（加载失败）→ 后构建」这条路径下
                # 指纹一字不变，若照旧按指纹跳过就会永久停在 0 工具（2026-09-20
                # 生产实测：新建 aliyun-chinook_semantic → 后台对账如期尝试 → 失败 →
                # 此后每次对账都跳过它）。任何一次瞬时失败（wren 崩溃、启动超时）
                # 同理会让该库一直死到人工 build / 保存库配置 / 重启，与「不许静默
                # 降级」相悖。代价可控：本函数只在启动与写路径的后台对账（单飞线程）
                # 里跑，不在请求路径上，且健康条目依旧不重载。
                retried.append(name)
                to_load.append(entry)

        loaded: Dict[str, str] = {}
        for entry in to_load:
            if load:
                loaded[entry.server_name] = _load_entry(entry)
            # 加载完成才换上：加载期间旧条目的工具仍在服务，调用方无空窗
            _sub_entries[entry.server_name] = entry

        _sub_registry_warmed = True
        _rebuild_sub_snapshot()
        return {
            "added": added,
            "changed": changed,
            "retried": retried,
            "removed": removed,
            "loaded": loaded,
            "counts": {n: len(e.tools) for n, e in _sub_entries.items()},
        }


def ensure_sub_loaded(db_name: str, force: bool = False) -> Dict[str, Any]:
    """把某个库对应的 wrenai server 立即加载进注册表（写路径用）。

    管理员新增/改库后的同步通道：返回体里带回工具数与失败原因，让"配好了但
    工具没起来"当场可见（2026-09-08 的教训是不许静默降级）。慢（起 MCP 子进程，
    秒级）→ 调用方须放到线程里（``asyncio.to_thread``），别阻塞事件循环。

    Args:
        db_name: 库名（db_config 的 name）。
        force: 即使指纹未变也重新加载（管理员刚改过配置时用）。
    """
    with _sub_entries_lock:
        _prune_orphan_entries()
        entry = _build_sub_entry(db_name)
        if entry is None:
            removed = [n for n, e in _sub_entries.items() if e.db_name == db_name]
            for n in removed:
                del _sub_entries[n]
            if removed:
                _rebuild_sub_snapshot()
            return {
                "db_name": db_name, "server": None, "status": "skipped",
                "reason": "该库未关联 Wren 语义库（未建模），走 dbmcp 直连",
                "tools_loaded": 0, "reloaded": False, "removed": removed,
            }

        current = _sub_entries.get(entry.server_name)
        if (
            not force
            and current is not None
            and current.fingerprint == entry.fingerprint
            and current.status == "ok"
        ):
            return {
                "db_name": db_name, "server": entry.server_name, "status": "ok",
                "tools_loaded": len(current.tools), "reloaded": False, "removed": [],
            }

        status = _load_entry(entry)
        _sub_entries[entry.server_name] = entry
        _sub_registry_warmed = True
        _rebuild_sub_snapshot()
        return {
            "db_name": db_name, "server": entry.server_name, "status": status,
            "tools_loaded": len(entry.tools), "reloaded": True, "removed": [],
            "error": None if status == "ok" else status,
        }


def invalidate_sub_entries(db_name: Optional[str] = None) -> List[str]:
    """摘掉注册表条目（不加载）。默认摘掉全部 wrenai 条目，dbmcp 保留。

    摘掉后该库的工具立刻不再出现在模型可见清单里，执行侧对已发出的调用回错误
    ToolMessage（见 DynamicMCPToolsMiddleware），**不会执行**陈旧实例。

    ⚠️ 语义库内容变更**不要**用它（会有一段空窗）：``refresh_sub_entries`` 自己
    会按指纹判断该不该重载，直接调 ``reload_sub_entries_in_background()`` 即可。
    """
    with _sub_entries_lock:
        if db_name is None:
            dropped = [n for n, e in _sub_entries.items() if e.db_name is not None]
        else:
            dropped = [n for n, e in _sub_entries.items() if e.db_name == db_name]
        for n in dropped:
            del _sub_entries[n]
        if dropped:
            _rebuild_sub_snapshot()
        return dropped


def reload_sub_entries_in_background() -> bool:
    """把全量对账丢到守护线程（语义库增删改后调用，免阻塞请求线程）。

    已在跑则直接返回 False（不排队：晚到的变更会被下一轮对账看到——写路径已经
    把 detector 缓存失效掉，对账读到的必然是最新配置）。
    """
    global _sub_reload_thread
    with _reload_thread_lock:
        if _sub_reload_thread is not None and _sub_reload_thread.is_alive():
            return False
        thread = threading.Thread(
            target=_bg_refresh_sub_entries, name="mcp-sub-reload", daemon=True,
        )
        _sub_reload_thread = thread
        thread.start()
        return True


def _bg_refresh_sub_entries() -> None:
    try:
        result = refresh_sub_entries()
        # !%s = 上次失败被重试的条目（排查「某库工具一直没起来」时看这一项）
        _logger.info(
            "[mcp] 后台对账完成: +%s ~%s !%s -%s counts=%s",
            result["added"], result["changed"], result["retried"], result["removed"],
            result["counts"],
        )
    except Exception as e:  # noqa: BLE001
        _logger.warning("[mcp] 后台对账失败: %s", e)


# ── 4. 加载主智能体工具 ──────────────────────────────────
def load_main_tools() -> List:
    """加载主智能体专用的 MCP 工具（图表生成）"""
    global _main_tools

    if _main_tools is not None:
        return _main_tools

    print("⏳ 加载主智能体 MCP 工具 (mcp-server-chart)...", flush=True)

    servers = _get_main_server_config()
    _main_tools = _load_mcp_servers(servers, "main")

    # 组内任一 server 失败 → 汇总行不许再打 ✅（2026-09-08：wrenai 挂了
    # 汇总仍是"✅ ...加载完成"，把失败盖住）
    failed_here = [n for n in servers if _mcp_server_results.get(n) != "ok"]
    if failed_here:
        print(f"⚠️ 主智能体 MCP 工具加载完成: {len(_main_tools)} 个工具"
              f"（失败 server: {', '.join(failed_here)}，详见上方 ❌）", flush=True)
    else:
        print(f"✅ 主智能体 MCP 工具加载完成: {len(_main_tools)} 个工具", flush=True)
    return _main_tools


# ── 5. 加载子智能体工具 ──────────────────────────────────
def load_sub_tools() -> List:
    """加载子智能体专用的 MCP 工具（wrenai + dbmcp），并建立运行期注册表"""
    global _sub_tools

    if _sub_tools is not None:
        return _sub_tools

    print("⏳ 加载子智能体 MCP 工具 (wrenai)...", flush=True)

    # 启动加载顺带建注册表：此后运行期增删库由 ensure_sub_loaded /
    # refresh_sub_entries 增量维护，不再需要重启进程（见 §3.1）。
    result = refresh_sub_entries()
    _sub_tools = list(_sub_snapshot)

    # 同 load_main_tools：组内有失败 server 时汇总行降级为 ⚠️ 并点名
    failed_here = [n for n in result["counts"] if _mcp_server_results.get(n) != "ok"]
    if failed_here:
        print(f"⚠️ 子智能体 MCP 工具加载完成: {len(_sub_tools)} 个工具"
              f"（失败 server: {', '.join(failed_here)}，详见上方 ❌）", flush=True)
    else:
        print(f"✅ 子智能体 MCP 工具加载完成: {len(_sub_tools)} 个工具", flush=True)
    return _sub_tools


# ── 6. 加载所有工具（向后兼容） ──────────────────────────
def load_all_tools() -> List:
    """加载所有 MCP 工具（向后兼容旧代码）"""
    global _all_tools

    if _all_tools is not None:
        return _all_tools

    main_tools = load_main_tools()
    sub_tools = load_sub_tools()
    _all_tools = main_tools + sub_tools

    return _all_tools


# ── 7. 就绪门控检查 ──────────────────────────────────────
def check_mcp_readiness():
    """检查 MCP 工具加载状态，必要时阻止启动"""
    main_ok = _main_tools is not None and len(_main_tools) > 0
    sub_ok = _sub_tools is not None and len(_sub_tools) > 0

    # 至少需要一组工具
    if not main_ok and not sub_ok:
        details = "\n".join(
            f"  - {name}: {status}"
            for name, status in _mcp_server_results.items()
        )
        raise MCPToolsLoadError(
            f"所有 MCP 服务器均连接失败，服务无法启动:\n{details}\n"
            f"请检查:\n"
            f"  1. WrenAI 是否已安装且 WREN_BIN_PATH 指向正确的可执行文件\n"
            f"  2. Node.js/npx 是否可用（当前图表引擎: {settings.CHART_ENGINE}）\n"
            f"  3. WREN_PROJECT_PATH 是否指向有效的 WrenAI 项目目录"
        )

    # 记录降级状态
    if not main_ok:
        print("⚠️  主智能体工具加载失败，图表生成功能不可用", flush=True)
    if not sub_ok:
        print("⚠️  子智能体工具加载失败，NL2SQL 功能不可用", flush=True)


def mcp_allow_degraded() -> bool:
    """MCP_ALLOW_DEGRADED=1/true/yes/on 时允许关键 server 缺失仍降级启动。

    默认 False：wrenai_*/dbmcp 加载失败 → 启动预检拒绝启动（响亮失败）。
    降级启动仅用于明确知道语义层/直连不可用仍要保住其余功能（echarts/报告/闲聊）
    的运维场景——建模库查询会整体失效，勿常态使用。
    """
    return (os.getenv("MCP_ALLOW_DEGRADED", "") or "").strip().lower() in ("1", "true", "yes", "on")


def evaluate_mcp_preflight() -> Dict[str, Any]:
    """按 server 的启动预检判决（纯函数，读模块级加载结果；可离线单测）。

    2026-09-08 教训：wrenai_WIT 加载失败被"✅ 预检通过: 20 个 MCP 工具就绪"
    的总数掩盖（18 echarts + 2 dbmcp），语义层静默缺失 → 建模库查询全线退化。
    本函数把判决粒度落到每个 server：

    Returns:
        {"ok": 全部 server 成功,
         "failed": {name: 失败原因},
         "critical_failed": [命中 _CRITICAL_SERVER_PREFIXES 的失败 server 名],
         "counts": {name: 工具数},
         "allow_degraded": MCP_ALLOW_DEGRADED 开关,
         "block_startup": 关键 server 失败且未允许降级 → 调用方应 sys.exit(1)}
    """
    failed = {n: s for n, s in _mcp_server_results.items() if s != "ok"}
    critical = [n for n in failed if n.startswith(_CRITICAL_SERVER_PREFIXES)]
    allow = mcp_allow_degraded()
    return {
        "ok": not failed,
        "failed": failed,
        "critical_failed": critical,
        "counts": dict(_mcp_server_tool_counts),
        "skipped": sorted(_skipped_servers),  # 被有意跳过的 server（无配置）
        "allow_degraded": allow,
        "block_startup": bool(critical) and not allow,
    }


# ── 8. 获取工具状态 ──────────────────────────────────────
def get_mcp_status() -> Dict[str, Any]:
    """获取 MCP 工具加载状态（用于健康检查）。

    子智能体部分读**运行期注册表**（而非启动时的工具单例）：运行期新加的库、
    刚加载失败的原因都能在这里看到。``registry[server]`` 的 ``db_name``/
    ``loaded_at``/``fingerprint`` 是排查"到底哪个库没起来"的直接抓手。
    """
    entries = dict(_sub_entries)
    return {
        "main_agent": {
            "loaded": _main_tools is not None,
            "count": len(_main_tools) if _main_tools else 0,
            "tools": [t.name for t in _main_tools] if _main_tools else [],
            "servers": {
                k: v for k, v in _mcp_server_results.items()
                if k in _get_main_server_config().keys()
            }
        },
        "sub_agent": {
            "loaded": _sub_tools is not None,
            "count": len(_sub_snapshot),
            "tools": [getattr(t, "name", "") for t in _sub_snapshot],
            "servers": {n: e.status for n, e in entries.items()},
            "registry": {
                n: {
                    "db_name": e.db_name,
                    "status": e.status,
                    "tools": len(e.tools),
                    "loaded_at": e.loaded_at,
                    "fingerprint": e.fingerprint,
                }
                for n, e in entries.items()
            },
        },
        "total_tools": len(_all_tools) if _all_tools else 0
    }


# ── 9. 模块加载时初始化（懒加载模式） ──────────────────
# 注意：不立即加载，而是由调用方按需加载
# 这样可以避免在导入时就加载所有工具
print("📦 MCP 工具模块已加载（工具将在首次调用时初始化）", flush=True)


# ── 10. 导出 ──────────────────────────────────────────────
# 延迟加载：首次访问时才加载
class _LazyTools:
    """延迟加载工具代理"""

    @property
    def main_tools(self) -> List:
        if _main_tools is None:
            load_main_tools()
        return _main_tools

    @property
    def sub_tools(self) -> List:
        if _sub_tools is None:
            load_sub_tools()
        return _sub_tools

    @property
    def tools(self) -> List:
        """所有工具（向后兼容）"""
        if _all_tools is None:
            load_all_tools()
        return _all_tools


# 创建延迟加载实例
lazy = _LazyTools()

# 导出
__all__ = [
    'lazy',  # 延迟加载代理
    'main_tools',  # 直接访问（会触发加载）
    'sub_tools',  # 直接访问（会触发加载）
    'tools',  # 所有工具（向后兼容）
    'load_main_tools',
    'load_sub_tools',
    'load_all_tools',
    'get_mcp_status',
    'MCPToolsLoadError',
    '_mcp_server_results',
    # 运行期工具注册表（新增/删除库免重启）
    'set_sub_tool_filter',
    'sub_tools_snapshot',
    'sub_lookup_snapshot',
    'lookup_sub_tool',
    'sub_registry_warmed',
    'refresh_sub_entries',
    'ensure_sub_loaded',
    'invalidate_sub_entries',
    'reload_sub_entries_in_background',
]

# 为了方便，也可以直接导出属性
main_tools = lazy.main_tools
sub_tools = lazy.sub_tools
tools = lazy.tools

if __name__ == "__main__":
        import asyncio
        async def test_tool():
            # 找到指定工具
            tool_invoke = None
            for tool in tools:
                if tool.name == "recall_queries":
                    tool_invoke = tool
                    break

            if tool_invoke:
                try:
                    # 使用异步调用
                    result = await tool_invoke.ainvoke({"question": "对比导演+编剧双重身份 vs 纯导演身份的作品平均评分，按类型分组"})
                    print("=" * 60)
                    print(result)
                    print("=" * 60)
                except Exception as e:
                    print(f"调用失败: {e}")
            else:
                print("未找到 指定 工具")

        # 运行异步函数
        asyncio.run(test_tool())