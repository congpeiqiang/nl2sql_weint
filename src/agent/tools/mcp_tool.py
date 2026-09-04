"""
MCP 工具管理模块 - 支持主智能体和子智能体工具独立加载
"""

import logging
import os
import sys
import asyncio
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
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
            try:
                print(f"  ⏳ [{server_type}] 连接 {name}...", flush=True)

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

                # Wren 语义层工具注入 project_path，供 wrap_tool 快速路径使用
                # （get_context / recall_queries 在主进程直接调用 wren API，
                #  绕过 MCP 子进程 + MemoryStore 420MB 嵌入模型加载）
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
                                    _injected += 1
                                    break
                        if _injected > 0:
                            _logger.info("[MCP] %s: 已注入 _wren_project_path 到 %d 个工具", name, _injected)
                        else:
                            _logger.warning("[MCP] %s: 未匹配到任何数据库，fast-path 将不可用", name)
                    except Exception as e:
                        _logger.warning("[MCP] %s: _wren_project_path 注入失败（%s: %s），fast-path 将不可用", name, type(e).__name__, e)

                wrapped = [wrap_tool(t) for t in tools]
                all_tools.extend(wrapped)
                _mcp_server_results[name] = "ok"
                print(f"  ✅ [{server_type}] {name}: {len(wrapped)} tools loaded", flush=True)

            except asyncio.TimeoutError:
                msg = f"连接超时 (60秒)"
                _mcp_server_results[name] = msg
                print(f"  ❌ [{server_type}] {name}: {msg}", flush=True)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                _mcp_server_results[name] = msg
                print(f"  ❌ [{server_type}] {name}: FAILED — {msg}", flush=True)
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


# ── 3. 获取子智能体配置 ──────────────────────────────────
def _get_sub_server_config() -> Dict[str, Any]:
    """获取子智能体的 MCP 服务器配置。

    两路通道（LLM 按工具描述选择）：
    - ``wrenai_<库名>``：语义层——每个已建模库（db_config 配了 wren_project，
      或默认 WREN_PROJECT_PATH 项目里建模的库）一个专属 server，工具前缀
      ``wrenai_<库名>_``（如 ``wrenai_imdb_run_sql``）。
    - ``dbmcp``：db_mcp_server 直连（按 db_name 路由到对应库的 runner），
      工具前缀 ``dbmcp_``。由 .env 的 ``NL2SQL_DBMCP_ENABLED`` 控制（默认开）。

    注意：server 在进程启动时按 db_config 构建（工具单例缓存），前端新增/修改
    wren_project 后需重启后端生效。
    """
    from agent.utils.semantic_db import get_detector, wrenai_server_name

    detector = get_detector()
    servers: Dict[str, Any] = {}
    for db_name in sorted(detector.discover()):
        project = detector.project_path_for(db_name)
        if not project:
            continue
        server_name = wrenai_server_name(db_name)
        # 冲突兜底：不同中文库折到同一 ASCII 骨架（去哈希后同骨架，极罕见）时
        # 告警跳过，避免 tool 前缀歧义。处置：给库名补 ASCII 区分段（如 WIT库A/WIT库B）
        # 或让后端仅建模其一，改完重启生效。
        if server_name in servers:
            _logger.warning(
                "[mcp] wrenai server 名冲突: %r 跳过 %r（去哈希后同骨架；"
                "请在 db_config 库名里补 ASCII 区分段）",
                server_name, db_name,
            )
            continue

        # 从 db_config 同步连接信息到 Wren profile，通过 --profile 直传。
        # 绕过 ~/.wren/profiles.yml 的全局 active profile 兜底——否则未在
        # wren_project.yml 中 pin profile 的项目会继承全局 active profile，
        # 导致连接到错误的数据源（如 active=chinook 指向 clickhouse）。
        # profile 命名 wren_mcp_<db_name>，add_profile 不切换 active（已有 active
        # 时保留），幂等覆写（连接信息变了自动更新）。
        profile_name = None
        args = ["serve", "mcp", "--project", project]
        try:
            from mcp_server.db_mcp_server.db.core.db_config_store import get_store
            from wren.profile import add_profile

            cfg = get_store().get(db_name)
            profile_dict: Dict[str, Any] = {
                "datasource": cfg.db_type,
                "host": cfg.host,
                "port": cfg.port,
                "database": cfg.database,
                "user": cfg.user,
            }
            if cfg.password:
                profile_dict["password"] = cfg.password
            profile_name = f"wren_mcp_{wrenai_server_name(db_name)}"
            add_profile(profile_name, profile_dict)
            args.extend(["--profile", profile_name])
        except Exception as e:  # noqa: BLE001
            _logger.warning(
                "[mcp] %s: 同步 Wren profile 失败 (%s)，回退全局 active profile",
                db_name, e,
            )

        servers[server_name] = {
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
            }
        }

    if settings.NL2SQL_DBMCP_ENABLED:
        servers["dbmcp"] = {
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

    return servers


# ── 4. 加载主智能体工具 ──────────────────────────────────
def load_main_tools() -> List:
    """加载主智能体专用的 MCP 工具（图表生成）"""
    global _main_tools

    if _main_tools is not None:
        return _main_tools

    print("⏳ 加载主智能体 MCP 工具 (mcp-server-chart)...", flush=True)

    servers = _get_main_server_config()
    _main_tools = _load_mcp_servers(servers, "main")

    print(f"✅ 主智能体 MCP 工具加载完成: {len(_main_tools)} 个工具", flush=True)
    return _main_tools


# ── 5. 加载子智能体工具 ──────────────────────────────────
def load_sub_tools() -> List:
    """加载子智能体专用的 MCP 工具（wrenai）"""
    global _sub_tools

    if _sub_tools is not None:
        return _sub_tools

    print("⏳ 加载子智能体 MCP 工具 (wrenai)...", flush=True)

    servers = _get_sub_server_config()
    _sub_tools = _load_mcp_servers(servers, "sub")

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


# ── 8. 获取工具状态 ──────────────────────────────────────
def get_mcp_status() -> Dict[str, Any]:
    """获取 MCP 工具加载状态（用于健康检查）"""
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
            "count": len(_sub_tools) if _sub_tools else 0,
            "tools": [t.name for t in _sub_tools] if _sub_tools else [],
            "servers": {
                k: v for k, v in _mcp_server_results.items()
                if k in _get_sub_server_config().keys()
            }
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