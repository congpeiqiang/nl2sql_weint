"""
nl2sql 子智能体 — AsyncSubAgent 独立 graph。
配置来源: subagents/configs/nl2sql.yaml。
"""

import yaml
from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware import SkillsMiddleware
from agent.llms.model import deepseek_model
from agent.tools.mcp_tool import tools as mcp_tools
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

# ── 构建 Agent ─────────────────────────────────────────
file_backend = FilesystemBackend(root_dir=base_dir, virtual_mode=True)
skills_middleware = SkillsMiddleware(backend=file_backend, sources=skills)

agent = create_deep_agent(
    model=deepseek_model,
    tools=resolved_tools,
    middleware=[skills_middleware, ProgressTrackerMiddleware()],
    backend=file_backend,
    system_prompt=system_prompt + "\n\n## 数据库\n默认 imdb。调用 run_sql 时传 db_name='imdb'。",
).with_config({"recursion_limit": 500})
print(f"[nl2sql_agent] loaded: {len(resolved_tools)} tools", flush=True)
