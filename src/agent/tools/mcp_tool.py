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

_logger = logging.getLogger(__name__)

# 全局缓存
_tools = None
_tools_loaded = False


def _get_mcp_tools_sync() -> List:
    """同步获取 MCP 工具（在模块加载时调用）"""
    global _tools, _tools_loaded

    if _tools_loaded:
        return _tools

    print("⏳ 正在加载 MCP 工具...", flush=True)

    all_tools = []
    servers = {
        "mcp-server-chart": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-p", "semiotic", "semiotic-mcp"]
        },
        # "antv-chart": {
        #     "transport": "stdio",
        #     "command": "npx",
        #     "args": ["-y", "@antv/mcp-server-chart"]
        # },
        "wrenai": {
            "transport": "stdio",
            "command": r"D:\code_work_space\llm\nl2sql\.venv\Scripts\wren.EXE",
            "args": [
                "serve", "mcp",
                "--project", r"D:\code_work_space\llm\nl2sql\src\agent\workspace\imdb_project"
            ],
            "env": {
                "WREN_LOG_LEVEL": "ERROR",  # 如果 Wren 支持日志级别控制
                "PYTHONUNBUFFERED": "1"
            }
        },
    }

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
                print(f"  ✅ MCP [{name}]: {len(wrapped)} tools loaded", flush=True)

            except asyncio.TimeoutError:
                print(f"  ❌ MCP [{name}]: 连接超时 (60秒)", flush=True)
            except Exception as e:
                print(f"  ❌ MCP [{name}]: FAILED — {type(e).__name__}: {e}", flush=True)

    finally:
        loop.close()

    _tools = all_tools
    _tools_loaded = True
    print(f"✅ MCP 工具加载完成: {len(all_tools)} 个工具", flush=True)
    return all_tools


# 在模块加载时初始化工具
try:
    tools = _get_mcp_tools_sync()
except Exception as e:
    print(f"❌ MCP 工具加载失败: {e}", flush=True)
    tools = []

# 导出 tools
__all__ = ['tools']

if __name__ == "__main__":
    print(f"\nTotal tools: {len(tools)}")
    for tool in tools:
        print(f"  - {tool.name}")
        if tool.name == "generate_line_chart":
            print(f"Description: {tool.description}")
            print(f"Args: {tool.args}")
            # ✅ 正确调用方式：只传 arguments 内容
            try:
                result = asyncio.run(asyncio.wait_for(
                    tool.ainvoke({
                        "data": [{"time": "2015", "value": 23}, {"time": "2016", "value": 32}],
                        "title": "测试", "axisXTitle": "X", "axisYTitle": "Y"
                    }),
                    timeout=30.0
                ))
                print("✅ 成功生成图表:")
                print(result)
                # result = asyncio.wait_for(
                #     tool.ainvoke({
                #         "data": [
                #             {"time": "2015", "value": 23},
                #             {"time": "2016", "value": 32},
                #             {"time": "2017", "value": 40},
                #             {"time": "2018", "value": 55}
                #         ],
                #         "title": "随时间变化的趋势",
                #         "axisXTitle": "时间",
                #         "axisYTitle": "数值",
                #         "width": 600,
                #         "height": 400
                #     }),
                #     timeout=30.0
                # )
                # print("✅ 成功生成图表:")
                # print(result)
            except asyncio.TimeoutError:
                print("❌ 请求超时，请检查本地服务是否响应缓慢")
            except Exception as e:
                print(f"❌ 调用异常: {type(e).__name__}: {e}")
            break

# if __name__ == "__main__":
#     print(f"\nTotal tools: {len(tools)}")
#     for tool in tools:
#         print(f"  - {tool.name}")
#         if tool.name == "generate_line_chart":
#             print(f"description:    {tool.description}")
#             print(f"args:    {tool.args}")
#             result = asyncio.run(tool.ainvoke({
#   "data": [
#     { "time": "2015", "value": 23 },
#     { "time": "2016", "value": 32 },
#     { "time": "2017", "value": 40 },
#     { "time": "2018", "value": 55 }
#   ],
#   "title": "随时间变化的趋势",
#   "axisXTitle": "时间",
#   "axisYTitle": "数值",
#   "width": 600,
#   "height": 400
# }))
#             print(result)
#             break