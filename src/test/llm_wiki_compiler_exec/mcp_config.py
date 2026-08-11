"""
llm-wiki-compiler MCP 连接配置（统一入口）

所有测试脚本从此模块导入配置，修改一处即可影响全部脚本。
"""

import os

# Wiki 项目根目录
WIKI_ROOT = r"D:\code_work_space\llm\LLM-Wiki-Project\chinook_autoIncrement"

# LLM 配置（用于 compile_wiki / query_wiki / search_pages）
LLM_CONFIG = {
    "LLMWIKI_PROVIDER": "openai",
    "LLMWIKI_MODEL": "deepseek-chat",
    "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "sk-xxx"),
    "OPENAI_BASE_URL": "https://api.deepseek.com",
}

# MultiServerMCPClient 服务器配置
SERVER_CONFIG = {
    "llmwiki": {
        "transport": "stdio",
        "command": "npx",
        "args": [
            "llm-wiki-compiler", "serve",
            "--root", WIKI_ROOT
        ],
        "env": LLM_CONFIG,
    }
}
