"""主智能体 — 异步子智能体架构。支持取消长时间运行的查询。"""
"""主智能体 - 异步子智能体架构。"""
from pathlib import Path

# 增强 check_async_task 返回详细进度（必须在 create_deep_agent 之前导入）
import agent.subagents.check_progress  # noqa: F401

from deepagents import create_deep_agent, AsyncSubAgent
from deepagents.backends import FilesystemBackend, CompositeBackend, LocalShellBackend
from deepagents.middleware import SkillsMiddleware
from agent.llms.model import deepseek_model
from agent.tools.mcp_tool import tools as mcp_tools

base_dir = Path(r"D:\code_work_space\llm\nl2sql\src\agent").resolve()
_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompt" / "MAIN_AGENT_PROMPT.md"
SYSTEM_PROMPT = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

file_backend = FilesystemBackend(root_dir=base_dir, virtual_mode=True)
shell_backend = LocalShellBackend(root_dir=Path(base_dir) / "workspace", inherit_env=True, virtual_mode=True)
composite_backend = CompositeBackend(default=shell_backend, routes={"/": file_backend})
skills_middleware = SkillsMiddleware(
    backend=file_backend,
    sources=["/workspace/skills/main/main-agent/", "/workspace/skills/main/alibabacloud-find-skills/", "/workspace/skills/main/report-export/"]
)

nl2sql_async = AsyncSubAgent(
    name="nl2sql",
    description="NL2SQL查询专家",
    graph_id="nl2sql_agent",
)

agent = create_deep_agent(
    model=deepseek_model,
    tools=mcp_tools,
    subagents=[nl2sql_async],
    memory=[],  # AGENTS.md 改为按需加载，由主智能体在委派 nl2sql 时读取并拼入 prompt
    middleware=[skills_middleware],
    backend=composite_backend,
    system_prompt=SYSTEM_PROMPT,
).with_config({"recursion_limit": 500})

print("[MainAgent] ready", flush=True)
