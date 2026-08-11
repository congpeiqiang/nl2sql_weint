"""
MCP 工具管理模块 - 支持主智能体和子智能体工具独立加载
"""

import logging
import os
import sys
import asyncio
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
    """获取主智能体的 MCP 服务器配置（根据 CHART_ENGINE 二选一）"""
    engine = settings.CHART_ENGINE.lower()

    if engine == "echarts":
        # ECharts MCP（本地全局安装，bin 命令 mcp-echarts，避免 npx 下载最新版）
        return {
            "mcp-server-echarts": {
                "transport": "stdio",
                "command": "mcp-echarts",
                "args": [],
                "env": {
                    **_UTF8_ENV,
                },
            },
        }

    # 默认 Semiotic MCP
    return {
        "mcp-server-chart": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-p", "semiotic", "semiotic-mcp"],
            "env": {
                **_UTF8_ENV,
            },
        },
    }


# ── 3. 获取子智能体配置 ──────────────────────────────────
def _get_sub_server_config() -> Dict[str, Any]:
    """获取子智能体的 MCP 服务器配置（wrenai）"""
    return {
        "wrenai": {
            "transport": "stdio",
            "command": settings.WREN_BIN_PATH,
            "args": [
                "serve", "mcp",
                "--project", settings.WREN_PROJECT_PATH
            ],
            "env": {
                **_UTF8_ENV,
                "WREN_LOG_LEVEL": "ERROR",
                "PYTHONUNBUFFERED": "1",
            }
        },
    }


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