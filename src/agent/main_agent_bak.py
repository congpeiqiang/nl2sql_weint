"""
智能对话 Agent 系统 - 多智能体架构。

主智能体负责：意图识别 -> 委派给子智能体 -> 汇总结果。
子智能体：nl2sql（数据查询）。
"""

from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend, CompositeBackend, LocalShellBackend
from deepagents.middleware import SkillsMiddleware

from agent.llms.model import deepseek_model
from agent.tools.mcp_tool import tools as mcp_tools
from agent.subagents.loader import load_subagent_configs, resolve_subagent_tools

base_dir = Path(r"D:\code_work_space\llm\nl2sql\src\agent").resolve()

_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompt" / "MAIN_AGENT_PROMPT.md"
SYSTEM_PROMPT = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

file_backend = FilesystemBackend(root_dir=base_dir, virtual_mode=True)
shell_backend = LocalShellBackend(
    root_dir=Path(base_dir) / "workspace",
    inherit_env=True,
    virtual_mode=True,
)
composite_backend = CompositeBackend(default=shell_backend, routes={"/": file_backend})

skills_middleware = SkillsMiddleware(
    backend=file_backend,
    sources=["/workspace/skills/main/", "/workspace/skills/nl2sql/"]
)

# Load subagents
_raw_configs = load_subagent_configs()
_subagents = resolve_subagent_tools(_raw_configs, mcp_tools)
print(f"[MainAgent] subagents: {[s['name'] for s in _subagents]}", flush=True)

agent = create_deep_agent(
    model=deepseek_model,
    tools=mcp_tools,
    subagents=_subagents,
    middleware=[skills_middleware],
    backend=composite_backend,
    system_prompt=SYSTEM_PROMPT,
)

print("[MainAgent] agent created successfully", flush=True)
