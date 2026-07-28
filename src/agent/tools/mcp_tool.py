import logging
import os
import sys
import asyncio
from typing import List
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

# ── 就绪门控状态 ────────────────────────────────────────────────────
_tools = None
_tools_loaded = False
_mcp_server_results: dict[str, str] = {}  # server_name → "ok" | error message


class MCPToolsLoadError(RuntimeError):
    """所有 MCP 服务器均连接失败，服务不应启动。"""


def _get_mcp_tools_sync() -> List:
    """同步获取 MCP 工具（在模块加载时调用）。

    就绪门控策略:
    - 任一 MCP 服务器连接成功 → 正常启动（部分降级可接受）
    - 全部 MCP 服务器连接失败 → 抛出 MCPToolsLoadError，阻止服务启动
    """
    global _tools, _tools_loaded

    if _tools_loaded:
        return _tools

    print("⏳ 正在加载 MCP 工具...", flush=True)

    # 子进程通用 UTF-8 环境变量（修复 Windows GBK 解码错误）
    _utf8_env = {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }

    all_tools = []
    _mcp_server_results.clear()
    servers = {
        "mcp-server-chart": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-p", "semiotic", "semiotic-mcp"],
            "env": {
                **_utf8_env,
            },
        },
        "wrenai": {
            "transport": "stdio",
            "command": settings.WREN_BIN_PATH,
            "args": [
                "serve", "mcp",
                "--project", settings.WREN_PROJECT_PATH
            ],
            "env": {
                **_utf8_env,
                "WREN_LOG_LEVEL": "ERROR",
                "PYTHONUNBUFFERED": "1",
            }
        },
    }

    total_servers = len(servers)

    # 创建新的事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        for name, config in servers.items():
            try:
                print(f"  ⏳ 连接 {name}...", flush=True)
                # 使用 StringIO 捕获输出
                f = io.StringIO()
                with redirect_stdout(f), redirect_stderr(f):
                    client = MultiServerMCPClient({name: config})
                    # 增加超时时间
                    tools = loop.run_until_complete(
                        asyncio.wait_for(
                            client.get_tools(),
                            timeout=60.0
                        )
                    )

                wrapped = [wrap_tool(t) for t in tools]
                all_tools.extend(wrapped)
                _mcp_server_results[name] = "ok"
                print(f"  ✅ MCP [{name}]: {len(wrapped)} tools loaded", flush=True)

            except asyncio.TimeoutError:
                msg = f"连接超时 (60秒)"
                _mcp_server_results[name] = msg
                print(f"  ❌ MCP [{name}]: {msg}", flush=True)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                _mcp_server_results[name] = msg
                print(f"  ❌ MCP [{name}]: FAILED — {msg}", flush=True)

    finally:
        loop.close()

    _tools = all_tools
    _tools_loaded = True

    # ── 就绪门控：全部 MCP 服务器失败时拒绝启动 ──
    failed_count = sum(1 for v in _mcp_server_results.values() if v != "ok")
    if failed_count == total_servers:
        details = "\n".join(
            f"  - {name}: {status}"
            for name, status in _mcp_server_results.items()
        )
        raise MCPToolsLoadError(
            f"所有 {total_servers} 个 MCP 服务器均连接失败，服务无法启动:\n{details}\n"
            f"请检查:\n"
            f"  1. WrenAI 是否已安装且 WREN_BIN_PATH 指向正确的可执行文件\n"
            f"  2. Node.js/npx 是否可用（用于 Semiotic MCP）\n"
            f"  3. WREN_PROJECT_PATH 是否指向有效的 WrenAI 项目目录"
        )

    if failed_count > 0:
        failed_names = [n for n, s in _mcp_server_results.items() if s != "ok"]
        print(
            f"⚠️  部分 MCP 服务器加载失败 ({failed_count}/{total_servers}): "
            f"{', '.join(failed_names)}。服务将以降级模式运行。",
            flush=True,
        )

    print(f"✅ MCP 工具加载完成: {len(all_tools)} 个工具 "
          f"({total_servers - failed_count}/{total_servers} 服务器可用)", flush=True)
    return all_tools


# ── 模块加载时初始化 + 就绪门控 ─────────────────────────────────────
try:
    tools = _get_mcp_tools_sync()
except MCPToolsLoadError:
    # 致命错误：向上传播，阻止 graph 注册和服务启动
    raise
except Exception as e:
    # 未预期的异常也视为致命
    raise MCPToolsLoadError(f"MCP 工具加载过程中发生未预期错误: {e}") from e

# 导出 tools
__all__ = ['tools', 'MCPToolsLoadError', '_mcp_server_results']

if __name__ == "__main__":
    print(f"\nTotal tools: {len(tools)}")
    for tool in tools:
        print(f"  - {tool.name}")