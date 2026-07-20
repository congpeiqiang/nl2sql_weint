"""
智能对话 Agent 系统 — 多智能体架构。

主智能体负责：意图识别 → 委派给子智能体 → 汇总结果。
子智能体：nl2sql（数据查询）、未来可扩展更多。

参考: ERP_OPENCLAW/src/agent/main_agent.py
"""

from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend, CompositeBackend
from deepagents.middleware import SkillsMiddleware

from agent.llms.model import deepseek_model
from agent.tools.mcp_tool import tools as mcp_tools
from agent.subagents.loader import load_subagent_configs, resolve_subagent_tools

# ── 路径配置 ──────────────────────────────────────────────
base_dir = Path(r"D:\code_work_space\llm\nl2sql\src\workspace").resolve()

_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompt" / "MAIN_AGENT_PROMPT.md"
SYSTEM_PROMPT = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

# ── 后端 ──────────────────────────────────────────────────
file_backend = FilesystemBackend(root_dir=base_dir, virtual_mode=True)
composite_backend = CompositeBackend(default=file_backend, routes={"/": file_backend})

# ── 技能中间件 ────────────────────────────────────────────
skills_middleware = SkillsMiddleware(
    backend=file_backend,
    sources=["/skills/main/", "/skills/sql-of-thought/"]
)

# ── 加载子智能体 ──────────────────────────────────────────
_raw_configs = load_subagent_configs()
_subagents = resolve_subagent_tools(_raw_configs, mcp_tools)
print(f"[MainAgent] 子智能体: {[s['name'] for s in _subagents]}")
print(f"[MainAgent] MCP 工具: {[t.name for t in mcp_tools]}")

# ── 创建 Agent ────────────────────────────────────────────
agent = create_deep_agent(
    model=deepseek_model,
    tools=mcp_tools,
    subagents=_subagents,
    memory=["/memory/AGENTS.md"],
    middleware=[skills_middleware],
    backend=composite_backend,
    system_prompt=SYSTEM_PROMPT,
)
