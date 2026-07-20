
async def get_mcp_tools():
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from agent.utils.path_resolver import wrap_tool
    import asyncio
    client = MultiServerMCPClient(
            {
                "mcp-db": {
                    "transport": "http",  # HTTP-based remote server
                    # Ensure you start your weather server on port 8000
                    "url": "http://localhost:8000/mcp",
                },
                # "mcp-db": {
                #     "transport": "stdio",
                #     "command": "python",
                #     "args": ["./app/db/server.py"],
                # },
                "mcp-server-chart": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-p", "semiotic", "semiotic-mcp"]
                },
                # "quick_chart": {
                #     "type": "sse",
                #     "url": "https://mcp.api-inference.modelscope.net/26470d550fdc49/sse"
                # },
                "llmwiki": {
                    "transport": "stdio",
                    "command": "npx",
                    # "args": ["llm-wiki-compiler", "serve", "--root", "D:\\code_work_space\\llm\\LLM-Wiki-Project\\chinook_autoIncrement"],
                    "args": ["llm-wiki-compiler", "serve", "--root", "D:\\code_work_space\\llm\\LLM-Wiki-Project\\aix_report"],
                    # "args": ["llm-wiki-compiler", "serve", "--root", "\\LLM-Wiki-Project\\Chinook_AutoIncrement"],
                    "env": {
                        "LLMWIKI_PROVIDER": "openai",
                        "LLMWIKI_MODEL": "deepseek-chat",
                        "OPENAI_API_KEY": "sk-97dc8af1edbb468f868ddad89cedda78",
                        "OPENAI_BASE_URL": "https://api.deepseek.com"
                    }
                }
            },

        )
    _tools = await client.get_tools()
    return [wrap_tool(t) for t in _tools]

import asyncio
tools = asyncio.run(get_mcp_tools())

if __name__ == "__main__":
    import asyncio
    async def get_wiki_status():
        tools = await get_mcp_tools()

        # 找到 wiki_status 工具
        wiki_status_tool = None
        for tool in tools:
            if tool.name == "search_pages":
                wiki_status_tool = tool
                break

        if wiki_status_tool:
            try:
                # 使用异步调用
                result = await wiki_status_tool.ainvoke({"question": "artist表 track表 album表的结构和列定义"})
                # result = await wiki_status_tool.ainvoke({"source": "D:\\code_work_space\\llm\\nl2sql\\docs\\Chinook_Sqlite_Sql.md"})
                # result = await wiki_status_tool.ainvoke({"slug": "艺术家内部流派不一致"})
                # result = await wiki_status_tool.ainvoke({})
                print("Wiki状态:")
                print("=" * 60)
                print(result)
                print("=" * 60)
            except Exception as e:
                print(f"调用失败: {e}")
        else:
            print("未找到 wiki_status 工具")
    # 运行异步函数
    asyncio.run(get_wiki_status())

    # async def exec_mcp_db():
    #     tools = await get_mcp_tools()
    #     db_tool = None
    #     for tool in tools:
    #         if tool.name == "run_sql":
    #             db_tool = tool
    #             break
    #     if db_tool:
    #         try:
    #             # 使用异步调用
    #             result = await db_tool.ainvoke({"sql": "SELECT * FROM Artist LIMIT 10"})
    #             print(result)
    #         except Exception as e:
    #             print(f"调用失败: {e}")
    #
    # asyncio.run(exec_mcp_db())