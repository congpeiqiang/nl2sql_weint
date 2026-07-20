"""
llm-wiki-compiler MCP 工具调用辅助模块

提供统一的 get_llmwiki_tool() 和 print_result()，所有演示脚本共用。
配置集中在 mcp_config.py 中。
"""

import json
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp_config import SERVER_CONFIG


async def get_llmwiki_tool(name: str):
    """连接 llmwiki MCP 服务器并获取指定工具"""
    client = MultiServerMCPClient(SERVER_CONFIG)
    tools = await client.get_tools()
    for tool in tools:
        if tool.name == name:
            return tool
    available = [t.name for t in tools]
    raise ValueError(f"Tool not found: {name}. Available: {available}")


def print_result(result):
    """格式化打印工具返回结果"""
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
        except (json.JSONDecodeError, TypeError):
            print(result[:3000])
    elif isinstance(result, (list, dict)):
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(str(result)[:3000])
