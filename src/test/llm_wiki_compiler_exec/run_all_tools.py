"""
一次性演示全部 7 个 llm-wiki-compiler MCP 工具。

使用 MultiServerMCPClient 连接 llm-wiki-compiler MCP 服务器。
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from langchain_mcp_adapters.client import MultiServerMCPClient

WIKI_ROOT = 'D:\\\\code_work_space\\\\llm\\\\LLM-Wiki-Project\\\\chinook_autoIncrement'


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
    tool_map = {t.name: t for t in tools}

    async def run(name, args=None):
        print(f"\n{'='*60}")
        print(f"TOOL: {name}")
        print(f"ARGS: {json.dumps(args or {}, ensure_ascii=False)}")
        print(f"{'='*60}")
        try:
            result = await tool_map[name].ainvoke(args or {})
            print_result(result)
        except Exception as e:
            print(f"ERROR: {e}")

    await run("wiki_status")
    await run("read_page", {"slug": "发票定价模式"})
    await run("read_page", {"slug": "chinook-database"})
    await run("search_pages", {"question": "artist 表和 album 表的结构和列定义"})
    await run("query_wiki", {"question": "Chinook 数据库中 employee 表和其他表有什么关系？", "save": False})
    await run("lint_wiki")
    await run("get_context_pack", {"prompt": "Chinook 数据库中有哪些表？关系是什么？", "budget": 4000, "topPages": 5})


if __name__ == "__main__":
    asyncio.run(main())
