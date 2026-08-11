"""
nl2sql 子智能体 — AsyncSubAgent 独立 graph。
配置来源: subagents/configs/nl2sql.yaml。
"""
from dataclasses import dataclass
from typing import Any, Dict

import yaml
from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware import SkillsMiddleware
from langchain.agents.middleware import ModelRequest, dynamic_prompt

from agent.llms.model import deepseek_model
from agent.middlewares.skill_data import SkillDataMiddleware
from agent.tools.mcp_tool import sub_tools as mcp_tools
from agent.subagents.track_progress import ProgressTrackerMiddleware

base_dir = Path(__file__).resolve().parent

# ── 加载 YAML 配置 ────────────────────────────────────
cfg_path = base_dir / "subagents" / "configs" / "nl2sql.yaml"
with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

# 系统提示词
prompt_file = cfg.get("system_prompt_file", "")
if prompt_file:
    prompt_path = base_dir / prompt_file
    system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else cfg.get("system_prompt", "")
else:
    system_prompt = cfg.get("system_prompt", "")

# 工具匹配
tool_names = cfg.get("tools", [])
tool_index = {getattr(t, "name", None): t for t in mcp_tools if getattr(t, "name", None)}
resolved_tools = []
for pattern in tool_names:
    for tn, t in tool_index.items():
        if pattern in tn and t not in resolved_tools:
            resolved_tools.append(t)

# 技能
skills = cfg.get("skills", ["/workspace/skills/nl2sql/"])

file_backend = FilesystemBackend(root_dir=base_dir, virtual_mode=True)

# ── Skills Middleware ──────────────────────────────────────────
skills_middleware = SkillsMiddleware(backend=file_backend, sources=skills)

# ── Skill Data Middleware ──────────────────────────────────────
skill_data_middleware = SkillDataMiddleware(
    backend=file_backend,
    data_dir="/workspace/nl2sql_process_data"  # 使用指定目录
)

@dynamic_prompt
def dynamic_prompt(request: ModelRequest) -> str:
    execution_info = getattr(request.runtime, 'execution_info', None)
    thread_id = getattr(execution_info, 'thread_id', '') if execution_info else ''
    if len(request.system_message.content)>0:
        base_prompt = request.system_message.content[-1]["text"]
        prompt = base_prompt + f"\n## \nthread_id\nthread_id是 {thread_id}"
    else:
        return ""

    return prompt

@dataclass
class Context:
    thread_id: str = ""

# ── 构建 Agent ─────────────────────────────────────────
agent = create_deep_agent(
    model=deepseek_model,
    tools=resolved_tools,
    middleware=[skills_middleware, skill_data_middleware, ProgressTrackerMiddleware(), dynamic_prompt],
    backend=file_backend,
    context_schema=Context,
    system_prompt=system_prompt + "\n\n## 数据库\n默认 imdb。调用 run_sql 时传 db_name='imdb'。",
).with_config({"recursion_limit": 500})



# ── 调用包装器 ──────────────────────────────────────────────────
async def invoke_with_thread_id(state: Dict[str, Any], thread_id: str):
    """
    带 thread_id 的调用包装器
    """
    # 设置 thread_id
    skill_data_middleware.set_thread_id(thread_id)

    # 执行 Agent
    result = await agent.ainvoke(
        state,
        config={"configurable": {"thread_id": thread_id}},
        context=Context(thread_id=thread_id)
    )
    return result

print(f"[nl2sql_agent] loaded: {len(resolved_tools)} tools", flush=True)
