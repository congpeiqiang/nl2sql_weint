"""
Wiki 质量检查

使用 MultiServerMCPClient 连接 llm-wiki-compiler MCP 服务器。
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from langchain_mcp_adapters.client import MultiServerMCPClient

WIKI_ROOT = 'D:\\code_work_space\\llm\\LLM-Wiki-Project\\chinook_autoIncrement'


async def get_llmwiki_tool(name):
    client = MultiServerMCPClient({
        "llmwiki": {
            "transport": "stdio",
            "command": "npx",
            "args": [
                "llm-wiki-compiler", "serve",
                "--root", WIKI_ROOT
            ],
            "env": {
                "LLMWIKI_PROVIDER": "openai",
                "LLMWIKI_MODEL": "deepseek-chat",
                "OPENAI_API_KEY": "sk-97dc8af1edbb468f868ddad89cedda78",
                "OPENAI_BASE_URL": "https://api.deepseek.com",
            },
        }
    })
    tools = await client.get_tools()
    for tool in tools:
        if tool.name == name:
            return tool
    raise ValueError(f"Tool not found: {name}")


def print_result(result):
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


async def main():
    args = {}
    print("=" * 60)
    print(f"TOOL: lint_wiki")
    print(f"ARGS: {json.dumps(args, ensure_ascii=False)}")
    print("=" * 60)
    print()

    tool = await get_llmwiki_tool("lint_wiki")
    result = await tool.ainvoke(args)
    print_result(result)


if __name__ == "__main__":
    asyncio.run(main())
