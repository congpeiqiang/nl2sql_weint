"""
智能对话 Agent 系统 - 多智能体架构。

主智能体负责：意图识别 -> 委派给子智能体 -> 汇总结果。
子智能体：nl2sql（数据查询）。
子智能体执行过程通过 ToolMessage.artifact 传递给前端渲染。
"""

from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend, CompositeBackend, LocalShellBackend
from deepagents.middleware import SkillsMiddleware

from agent.llms.model import deepseek_model
from agent.tools.mcp_tool import tools as mcp_tools
from agent.subagents.loader import load_subagent_configs
from agent.subagents.task_with_trace import build_subagent_graphs, create_streaming_task_tool

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

# Load subagent configs and build graphs
raw_configs = load_subagent_configs()
subagent_graphs = build_subagent_graphs(raw_configs, deepseek_model, mcp_tools)
print(f"[MainAgent] subagent graphs: {list(subagent_graphs.keys())}", flush=True)

# Create streaming task tool
task_tool = create_streaming_task_tool(subagent_graphs)

# Merge tools: MCP tools + custom task tool
all_tools = list(mcp_tools) + [task_tool]
print(f"[MainAgent] total tools: {len(all_tools)} (mcp={len(mcp_tools)} + task)", flush=True)

agent = create_deep_agent(
    model=deepseek_model,
    tools=all_tools,
    subagents=[],  # 禁用默认 general-purpose subagent
    middleware=[skills_middleware],
    backend=composite_backend,
    system_prompt=SYSTEM_PROMPT,
)

# 检查工具列表
tool_names = [getattr(t, 'name', str(t)) for t in all_tools]
task_names = [n for n in tool_names if n in ('task', 'delegate')]
print(f"[MainAgent] total tools: {len(all_tools)}, task-like: {task_names}", flush=True)
print(f"[MainAgent] agent created successfully", flush=True)
