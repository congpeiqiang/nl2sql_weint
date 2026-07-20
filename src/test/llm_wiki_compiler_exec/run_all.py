"""
一次性演示全部 7 个 llm-wiki-compiler MCP 工具。

使用 MultiServerMCPClient 连接 llm-wiki-compiler MCP 服务器。
配置集中在 mcp_config.py 中。
"""

import asyncio
from mcp_helper import get_llmwiki_tool, print_result


async def main():
    # 只连一次，复用
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from mcp_config import SERVER_CONFIG

    client = MultiServerMCPClient(SERVER_CONFIG)
    tools = await client.get_tools()
    tool_map = {t.name: t for t in tools}

    async def run(name, args=None):
        print(f"\n{{'='*60}}")
        print(f"TOOL: {{name}}")
        print(f"ARGS: {{args or {{}}}}")
        print(f"{{'='*60}}")
        try:
            result = await tool_map[name].ainvoke(args or {})
            print_result(result)
        except Exception as e:
            print(f"ERROR: {{e}}")

    await run("wiki_status")
    await run("read_page", {"slug": "发票定价模式"})
    await run("read_page", {"slug": "chinook-database"})
    await run("search_pages", {"question": "artist 表和 album 表的结构和列定义"})
    await run("query_wiki", {"question": "Chinook 数据库中 employee 表和其他表有什么关系？", "save": False})
    await run("lint_wiki")
    await run("get_context_pack", {"prompt": "Chinook 数据库中有哪些表？关系是什么？", "budget": 4000, "topPages": 5})


if __name__ == "__main__":
    asyncio.run(main())
