"""
@File    :  main_agent.py.py
@Author  :  CongPeiQiang
@Time    :  2026/7/16 17:19
@Desc    :  
"""
"""
智能对话Agent系统
"""
from pathlib import Path
from deepagents import create_deep_agent as create_agent
from deepagents.backends import FilesystemBackend, LocalShellBackend, CompositeBackend
from deepagents.middleware import SkillsMiddleware
from langchain_core.messages import HumanMessage

from agent.llms.model import deepseek_model
from agent.tools.mcp_tool import tools

base_dir = Path(r"D:\code_work_space\llm\nl2sql\src\agent\workspace").resolve()

# 从文件加载系统提示词，与 memory 参考手册分离以优化上下文窗口
_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompt" / "SYSTEM_PROMPT.md"
SYSTEM_PROMPT = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

file_backend = FilesystemBackend(root_dir=base_dir, virtual_mode=True)
shell_backend = LocalShellBackend(
    root_dir=base_dir,
    inherit_env=True,
    virtual_mode=True,
    env={"PATH": r"C:\Windows\system32;C:\Windows;C:\Windows\System32\Wbem;C:\Windows\System32\WindowsPowerShell\v1.0\;C:\Windows\System32\OpenSSH\;C:\JDK\jdk11.0.14_10\bin;C:\MAVEN\apache-maven-3.6.3\bin;c:\program files\esafenet\cobra docguard client;C:\Program Files\CorpLink\current\module\mdm\x64\policy\bin;C:\Program Files\Pandoc\;C:\Program Files\Git\cmd;D:\software\miniconda3;D:\software\miniconda3\Scripts;D:\software\miniconda3\Library\bin;D:\software\nodejs\;D:\software\PowerShell\7\;C:\Users\congpeiqiang\AppData\Local\hermes\hermes-agent\venv\Scripts;C:\Users\congpeiqiang\AppData\Local\hermes\bin;D:\code_work_space\llm\huice\008\harness-agent-system\.venv\Scripts;C:\Program Files\nodejs\;C:\Users\congpeiqiang\AppData\Local\Programs\Python\Launcher\;C:\Users\congpeiqiang\AppData\Local\Microsoft\WindowsApps;D:\software\PyCharm Community Edition 2024.3.6\bin;D:\software\PyCharm 2024.3.6\bin;C:\Users\congpeiqiang\AppData\Roaming\npm;D:\software\Microsoft VS Code\bin;C:\Users\congpeiqiang\.local\bin"}

    # env={"PATH": r"C:\Program Files\nodejs;C:\Users\65132\AppData\Roaming\npm;C:\Windows\System32;C:\Windows;C:\Users\65132\Desktop\workspace\harness-agent-system\.venv\Scripts;C:\Users\65132\AppData\Local\Programs\Python\Python313\Scripts"}
)
skills_middleware = SkillsMiddleware(backend=file_backend, sources=["/skills/"])

composite_backend = CompositeBackend(
    default=shell_backend,
    routes={
        "/": file_backend,
    },
)
agent = create_agent(
    model=deepseek_model,
    tools=tools,
    memory=["/memory/AGENTS.md"],
    middleware=[
        skills_middleware,
    ],
    # skills=["/skills/"],
    backend=composite_backend,
    system_prompt=SYSTEM_PROMPT,
)

# import asyncio

# async def main():
#     hm = HumanMessage("音乐家有哪些?")
#     response = await agent.ainvoke({"message": hm})
#     print(response)
#     for r in response["messages"][-1]:
#         r.pretty_print()
#
# if __name__ == '__main__':
#     asyncio.run(main())