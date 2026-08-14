"""
nl2sql 子智能体 — AsyncSubAgent 独立 graph。
配置来源: subagents/configs/nl2sql.yaml。
"""
import yaml
from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware import SkillsMiddleware
from langchain.agents.middleware import ModelRequest, dynamic_prompt

from agent.llms.model import deepseek_model
from agent.middlewares.skill_data import SkillDataMiddleware
from agent.middlewares.write_todos import WriteTodosProtocolMiddleware
from agent.tools.mcp_tool import sub_tools as mcp_tools
from agent.subagents.track_progress import ProgressTrackerMiddleware
from agent.settings.file_permissions import FILE_PERMISSIONS

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

    # ── 双通道路由（D2 硬需求）────────────────────────────
    # 按当前 db_name 动态注入「语义层 / 直连」通道指引。
    # 用 SemanticDbDetector 实时判断（新增 wren 语义库后自动感知，无需改代码）。
    # 语义层工具前缀由库名推导（wrenai_<库名>_*），与 mcp_tool 启动的 server 一致。
    try:
        from langgraph.config import get_config as _cfg
        from agent.utils.semantic_db import get_detector, wrenai_server_name

        db_name = ""
        try:
            db_name = _cfg().get("configurable", {}).get("db_name", "") if _cfg is not None else ""
        except Exception:
            db_name = ""
        if not db_name:
            db_name = "imdb"  # 默认库
        modeled = get_detector().is_modeled(db_name)

        if modeled:
            prefix = wrenai_server_name(db_name)
            routing = (
                "\n\n## 查询通道路由（重要）\n"
                f"当前数据库: `{db_name}` —— **已在 Wren 语义层建模**。\n"
                f"使用语义层工具链：`{prefix}_get_mdl` / `{prefix}_describe_schema` / "
                f"`{prefix}_recall_queries` 等取 schema，最终用语义层 "
                f"`{prefix}_run_sql(sql, limit?)` 执行。\n"
            )
        else:
            routing = (
                "\n\n## 查询通道路由（重要）\n"
                f"当前数据库: `{db_name}` —— **未在语义层建模**。\n"
                "不要用 wrenai 语义层工具（get_mdl/describe_schema/run_sql 等）查询该库，"
                "wrenai 语义层工具按库绑定专属 server，当前库没有对应 server，"
                "会报 `table not found`/`INVALID_SQL`。\n"
                "改用直连工具：\n"
                f"- `dbmcp_get_db_info(db_name='{db_name}')` 获取表清单\n"
                f"- `dbmcp_run_sql(sql=..., db_name='{db_name}')` 直接执行 SQL\n"
                "- 若误用语义层 run_sql 且报 `not found`，立即改用 `dbmcp_run_sql`，不要进入纠错循环\n"
            )
        prompt += routing
    except Exception:  # noqa: BLE001  路由注入失败不影响正常流程
        pass

    return prompt

# ── 构建 Agent ─────────────────────────────────────────
agent = create_deep_agent(
    model=deepseek_model,
    tools=resolved_tools,
    # 注意：WriteTodosProtocolMiddleware 必须放在用户 middleware 列表末尾——
    # 内层后追加的 system 文本落在最终 system prompt 末尾，才能压过
    # TodoListMiddleware 默认的"简单任务可跳过 write_todos"指引（见 write_todos.py 模块 docstring）。
    middleware=[
        skills_middleware,
        skill_data_middleware,
        ProgressTrackerMiddleware(),
        dynamic_prompt,
        WriteTodosProtocolMiddleware(),
    ],
    backend=file_backend,
    permissions=FILE_PERMISSIONS,  # 文件读写安全控制：只读根，仅 workspace/{report,tmp,nl2sql_process_data} 可写（独立 graph，须单独传）
    # 注意：不在此处硬编码 imdb——当前数据库由下方 dynamic_prompt 从 configurable
    # 动态注入（「查询通道路由」段），此处的"默认 imdb"会与注入冲突，
    # 导致切库后子 agent 仍按 imdb 执行（2026-08-11 实证）。
    system_prompt=system_prompt + "\n\n## 数据库\n当前数据库以 dynamic_prompt 注入的「查询通道路由」段为准，调用 run_sql / get_db_info 时按注入的 db_name 传参。",
).with_config({"recursion_limit": 500})

print(f"[nl2sql_agent] loaded: {len(resolved_tools)} tools", flush=True)
