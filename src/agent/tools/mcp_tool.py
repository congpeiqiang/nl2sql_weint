
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
                
            },

        )
    _tools = await client.get_tools()
    return [wrap_tool(t) for t in _tools]

import asyncio
tools = asyncio.run(get_mcp_tools())

