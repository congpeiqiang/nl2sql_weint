"""nl2sql 子智能体 — AsyncSubAgent 独立 graph。
配置来源: subagents/configs/nl2sql.yaml。
"""
import os
import yaml
from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from agent.backends.dynamic_workspace import DynamicFilesystemBackend

# 修复 Windows/Py3.13 下 _resolve_path 的 `\\?\` 前缀误报越界（必须早于实例化导入）
import agent.utils.filesystem_backend_patch  # noqa: F401
from deepagents.middleware import SkillsMiddleware
from langchain.agents.middleware import ModelRequest, dynamic_prompt

from agent.llms.model import deepseek_model
from agent.middlewares.thinking_toggle import ThinkingToggleMiddleware
from agent.middlewares.sql_approval import build_sql_approval_middleware
from agent.middlewares.tool_filter import ToolFilterMiddleware
from agent.middlewares.langfuse_span import LangfuseSpanMiddleware
from agent.middlewares.write_todos import WriteTodosProtocolMiddleware
from agent.tools.mcp_tool import sub_tools as mcp_tools
from agent.subagents.track_progress import ProgressTrackerMiddleware
from agent.settings.file_permissions import FILE_PERMISSIONS
from agent.middlewares.trace_recorder import TraceRecorderMiddleware
from agent.trace.langfuse_client import get_langfuse_callbacks

base_dir = Path(__file__).resolve().parent.parent  # graphs/ 子目录，上两层到 src/agent/

# ── 工作区管理器 ──────────────────────────────────────────
from agent.workspace_manager import get_workspace_manager
_wm = get_workspace_manager()
_shared_skills_dir = _wm.shared_skills_dir

# ── 加载 YAML 配置 ────────────────────────────────────
cfg_path = base_dir / "subagents" / "configs" / "nl2sql.yaml"
with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

# 系统提示词（M4 版本管理：优先 Langfuse `nl2sql_system_prompt`(production)，
# 失败回退本地文件；Langfuse 里存含 {{VAR}} 占位符的原始正文，后续替换不受影响）
from agent.trace.langfuse_client import get_prompt_text

prompt_file = cfg.get("system_prompt_file", "")
if prompt_file:
    prompt_path = base_dir / prompt_file
    local_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else cfg.get("system_prompt", "")
else:
    local_prompt = cfg.get("system_prompt", "")
system_prompt = get_prompt_text(
    "nl2sql_system_prompt",
    fallback=local_prompt,
    min_chars=100,  # 内容校验：Langfuse 正文过短（残缺）时回退本地，防残缺 prompt 上线
)

# 工具匹配
tool_names = cfg.get("tools", [])
tool_index = {getattr(t, "name", None): t for t in mcp_tools if getattr(t, "name", None)}
resolved_tools = []
for pattern in tool_names:
    for tn, t in tool_index.items():
        if pattern in tn and t not in resolved_tools:
            resolved_tools.append(t)

# 技能
skills = cfg.get("skills", ["/shared/skills/nl2sql/"])

# VFS 后端设计（与 main_agent.py 同构，多工作区隔离）
shared_code_backend = FilesystemBackend(root_dir=base_dir, virtual_mode=True)
shared_skills_backend = FilesystemBackend(root_dir=_shared_skills_dir, virtual_mode=True)
# 动态工作区：每次操作前从 WorkspaceManager 重新解析 root_dir，切换工作区即时生效
workspace_data_backend = DynamicFilesystemBackend(get_root_dir=lambda: _wm.active_workspace)

# nl2sql_agent 不需要 shell backend 和 memory backend，
# 但需要 CompositeBackend 来路由 /shared/skills/ → 共享、/workspace/ → 当前工作区
from deepagents.backends import CompositeBackend
composite_backend = CompositeBackend(
    default=workspace_data_backend,
    routes={
        "/shared/skills/": shared_skills_backend,
        "/": shared_code_backend,
    },
)

# ── Skills Middleware ──────────────────────────────────────────
skills_middleware = SkillsMiddleware(backend=shared_code_backend, sources=skills)

# ── SQL 审批闸门（P1-3）──────────────────────────────────────
# 只对 run_sql 类工具生效；只读查询直接放行（清晰查询零打扰），
# 写/DDL 与疑似全表拉取触发 interrupt，前端弹审批卡。
sql_approval_middleware = build_sql_approval_middleware(resolved_tools)

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
            # ── Cube 摘要注入 ──────────────────────────────
            # 列出可用 Cube，让 LLM 在策略选择时就知道能否走 Cube 快速通道
            try:
                project_path = get_detector().project_path_for(db_name)
                if project_path:
                    from pathlib import Path as _Path
                    cubes_dir = _Path(project_path) / "cubes"
                    if cubes_dir.is_dir():
                        cube_names = sorted(
                            d.name for d in cubes_dir.iterdir()
                            if d.is_dir() and (d / "metadata.yml").exists()
                        )
                        if cube_names:
                            routing += (
                                f"\n可用 Cube（优先使用 Strategy C 快速通道）：{', '.join(f'`{c}`' for c in cube_names)}\n"
                                f"调用 `{prefix}_list_cubes()` 查看详情，匹配则用 "
                                f"`{prefix}_query_cube(cube, measures, dimensions)` 直接查询，跳过完整流水线。\n"
                            )
            except Exception:  # noqa: BLE001
                pass
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
# 注意：WriteTodosProtocolMiddleware 必须放在用户 middleware 列表末尾——
# 内层后追加的 system 文本落在最终 system prompt 末尾，才能压过
# TodoListMiddleware 默认的"简单任务可跳过 write_todos"指引（见 write_todos.py 模块 docstring）。
_middleware = [
    TraceRecorderMiddleware(
        db_path=os.path.join(str(_wm.active_workspace), "traces.sqlite"),
        agent_type="nl2sql_agent",
    ),
    skills_middleware,
    ProgressTrackerMiddleware(),
    # 与主 agent 同构：按 configurable.llm_route/llm_model 重建模型。
    # 否则子 agent 用模块级 deepseek_model（import 时按 active provider 创建），
    # 前端切模型（如 DeepSeek 官方 API）只对主 agent 生效，子 agent 仍打旧 provider。
    ThinkingToggleMiddleware(),
    ToolFilterMiddleware(),
    # M2 ② 结构化层：关键工具边界包 skill 级 span（skill 名作 metadata + vfs_path 定位）
    LangfuseSpanMiddleware(),
    dynamic_prompt,
]
if sql_approval_middleware is not None:
    _middleware.append(sql_approval_middleware)
_middleware.append(WriteTodosProtocolMiddleware())

agent = create_deep_agent(
    model=deepseek_model,
    tools=resolved_tools,
    middleware=_middleware,
    backend=composite_backend,
    permissions=FILE_PERMISSIONS,  # 文件读写安全控制：只读根，仅 workspace/{report,tmp,nl2sql_process_data} 可写（独立 graph，须单独传）
    # 注意：不在此处硬编码 imdb——当前数据库由下方 dynamic_prompt 从 configurable
    # 动态注入（「查询通道路由」段），此处的"默认 imdb"会与注入冲突，
    # 导致切库后子 agent 仍按 imdb 执行（2026-08-11 实证）。
    system_prompt=system_prompt + "\n\n## 数据库\n当前数据库以 dynamic_prompt 注入的「查询通道路由」段为准，调用 run_sql / get_db_info 时按注入的 db_name 传参。",
).with_config({
    "recursion_limit": 500,
    "callbacks": get_langfuse_callbacks(),  # Langfuse 全链路埋点（M1 监控上线；LANGFUSE_ENABLE=false 时空列表）
})

print(f"[nl2sql_agent] loaded: {len(resolved_tools)} tools", flush=True)
