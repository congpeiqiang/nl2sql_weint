"""
尝试读取 entities/ 页面——会失败

使用 MultiServerMCPClient 连接 llm-wiki-compiler MCP 服务器。
配置集中在 mcp_config.py 中，调用辅助在 mcp_helper.py 中。
"""

import asyncio
from mcp_helper import get_llmwiki_tool, print_result

TOOL_NAME = "read_page"
TOOL_ARGS = {"slug": "chinook-database"}


async def main():
    print("=" * 60)
    print(f"TOOL: {TOOL_NAME}")
    print(f"ARGS: {TOOL_ARGS}")
    print("=" * 60)
    print()

    tool = await get_llmwiki_tool(TOOL_NAME)
    result = await tool.ainvoke(TOOL_ARGS)
    print_result(result)


if __name__ == "__main__":
    asyncio.run(main())
