"""
语义搜索表结构（可搜索 entities/）

使用 MultiServerMCPClient 连接 llm-wiki-compiler MCP 服务器。
配置集中在 mcp_config.py 中，调用辅助在 mcp_helper.py 中。
"""

import asyncio
from mcp_helper import get_llmwiki_tool, print_result

TOOL_NAME = "search_pages"
TOOL_ARGS = {"question": "artist 表和 album 表的结构和列定义"}


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
