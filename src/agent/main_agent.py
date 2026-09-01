"""主智能体 — 异步子智能体架构。支持取消长时间运行的查询。"""
"""主智能体 - 异步子智能体架构。"""
from pathlib import Path

# 增强 check_async_task 返回详细进度（必须在 create_deep_agent 之前导入）
import agent.subagents.check_progress  # noqa: F401
# 自动同步子智能体 todos 到主智能体 state（零前端改动方案）
import agent.subagents.sync_launcher  # noqa: F401
# 透传父 run 的 configurable 到异步子 agent run（前端选库 db_name 才能到达子 agent）
import agent.middlewares.deepagents_async_config_patch
# 修复 Windows/Py3.13 下 _resolve_path 的 `\\?\` 前缀误报越界（必须早于实例化导入）
import agent.utils.filesystem_backend_patch  # noqa: F401

from deepagents import create_deep_agent, AsyncSubAgent, DeepAgentState
from deepagents.backends import FilesystemBackend, CompositeBackend
from agent.backends.dynamic_workspace import DynamicFilesystemBackend, DynamicLocalShellBackend
from deepagents.middleware import SkillsMiddleware
from langchain.agents.middleware import ModelRequest, dynamic_prompt
from agent.llms.model import deepseek_model
from agent.tools.mcp_tool import main_tools as mcp_tools
from agent.tools.report_builder import build_report_tool
from agent.settings.file_permissions import FILE_PERMISSIONS
from agent.middlewares.query_keywords import QueryKeywordsMiddleware
from agent.middlewares.thinking_toggle import ThinkingToggleMiddleware
from agent.middlewares.message_slimmer import MessageSlimmerMiddleware
from agent.middlewares.current_db_context import CurrentDbContextMiddleware
from agent.middlewares.token_meter import TokenMeterMiddleware, _accumulate_token_stats
from agent.middlewares.trace_recorder import TraceRecorderMiddleware
from agent.middlewares.langfuse_span import LangfuseSpanMiddleware
from agent.middlewares.vfs_path_resolver import VfsPathResolverMiddleware
from agent.middlewares.execute_guard import ExecuteGuardMiddleware
from agent.middlewares.quota_error import QuotaErrorMiddleware
from agent.middlewares.model_timeout import ModelTimeoutMiddleware
from agent.trace.langfuse_client import get_langfuse_callbacks, get_prompt_text
from agent.utils.skills_versioning import effective_skills_sources
from typing import Annotated
from typing_extensions import NotRequired

base_dir = Path(__file__).parent.resolve()
_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompt" / "MAIN_AGENT_PROMPT.md"

# ── 工作区管理器 ──────────────────────────────────────────
from agent.workspace_manager import get_workspace_manager
_wm = get_workspace_manager()
_shared_memory_dir = _wm.shared_memory_dir
_shared_skills_dir = _wm.shared_skills_dir


def _build_system_prompt() -> str:
    """根据 CHART_ENGINE 动态构建系统提示词，注入对应图表引擎规范。

    M4 版本管理：base prompt 优先从 Langfuse `main_system_prompt`(production) 拉取，
    失败回退本地文件（get_prompt_text 内部兜底）。{{CHART_SPEC}} 等占位符在拉回的
    正文里原样保留，下面的替换逻辑不区分来源——Langfuse UI 编辑 → 打 production →
    重启服务即生效；回滚 = 标签切旧版 或 LANGFUSE_PROMPT_ENABLED=0 强制本地。
    """
    # 本地兜底文件存在性防护（M4 评估后新增）：文件缺失时给空串而非抛 FileNotFoundError，
    # 由 get_prompt_text 的 fallback 语义兜住；required_markers 校验 Langfuse 正文没把
    # 图表占位符弄丢（否则下方 .replace() 静默 no-op，带着残缺 prompt 上线）。
    local_fallback = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8") if _SYSTEM_PROMPT_PATH.is_file() else ""
    base = get_prompt_text(
        "main_system_prompt",
        fallback=local_fallback,
        required_markers=["{{CHART_SPEC}}", "{{CHART_ENGINE_NAME}}", "{{CHART_OUTPUT_FORMAT}}"],
        min_chars=100,
    )
    # 读取 ECharts 图表规范
    chart_spec_path = Path(__file__).parent / "prompt" / "chart_specs" / "echarts.md"
    if chart_spec_path.exists():
        chart_spec = chart_spec_path.read_text(encoding="utf-8")
    else:
        chart_spec = ""
        _log.warning("[main_agent] chart_specs/echarts.md 不存在，图表规范将缺失")

    # 替换占位符
    prompt = base.replace("{{CHART_SPEC}}", chart_spec)
    prompt = prompt.replace("{{CHART_ENGINE_NAME}}", "ECharts")
    # prompt = prompt.replace("{{CHART_OUTPUT_FORMAT}}", "PNG")
    prompt = prompt.replace("{{CHART_OUTPUT_FORMAT}}", "html")

    return prompt


SYSTEM_PROMPT = _build_system_prompt()


@dynamic_prompt
def dynamic_prompt(request: ModelRequest) -> str:
    """从 configurable 读当前数据库名并注入系统提示词。

    主 agent 的 system prompt 是静态文件（MAIN_AGENT_PROMPT.md），不含 db_name。
    前端切库后 configurable.db_name 是最新的，但主 agent LLM 读不到——委派子 agent 时
    【数据库名称】会写错（默认 imdb）。本函数把当前库名动态注入，
    保证 start_async_task 的 description 写对库名（与子 agent 的 dynamic_prompt 同构）。
    """
    # 用 .text 属性取纯文本（content 可能是 str 也可能是 block 列表，
    # 子 agent 是列表故用 content[-1]["text"]，主 agent 是 str 会炸）。
    prompt = getattr(request.system_message, "text", None) or ""
    if not prompt:
        return ""
    db_name = ""
    try:
        from langgraph.config import get_config as _cfg
        if _cfg is not None:
            db_name = (_cfg().get("configurable", {}) or {}).get("db_name", "") or ""
    except Exception:  # noqa: BLE001  读取失败则走默认
        db_name = ""
    if db_name:
        # 前置到提示词最顶部（实证 2026-08-11：追加在末尾时，模型对对话历史/总结里
        # 自己上轮说过的库名信任度更高，切库后仍按旧库委派——clickhouse→imdb 回归）。
        # 本段给出最高优先级、可覆盖历史陈旧库名的陈述。
        db_section = (
            f"## 当前数据库（configurable 动态注入）— 最高优先级\n"
            f"当前用户选中的数据库是 `{db_name}`。\n"
            "无论对话历史或总结中如何描述数据库，一律以本段为准；"
            "历史中提到的其他库名已过时，忽略。\n"
            "委派 nl2sql 时，start_async_task 的 description 中【数据库名称】必须写 "
            f"`{db_name}`，禁止使用其他库名或默认值（尤其禁止写 imdb）。\n\n"
        )
        prompt = db_section + prompt
    return prompt


# VFS 后端设计（多工作区隔离）：
# - "/shared/memory/" → shared_memory_backend（共享 memory）
# - "/shared/skills/" → shared_skills_backend（共享 skills）
# - "/workspace/"        → workspace_data_backend（当前工作区：report/tmp/process_data 等）
# - "/"                  → vfs_root_backend（VFS 根：AGENT_DATA_ROOT 配置时指向项目外
#                          基础目录，代码根 src/agent/ 彻底退出 VFS；未配置时回退 src/agent/）
vfs_root_backend = FilesystemBackend(root_dir=_wm.data_root, virtual_mode=True)
shared_memory_backend = FilesystemBackend(root_dir=_shared_memory_dir, virtual_mode=True)
shared_skills_backend = FilesystemBackend(root_dir=_shared_skills_dir, virtual_mode=True)
# skill 版本化：SKILLS_REF 物化目录（<data_root>/skill_refs/），skill 脚本经 VFS 也读物化版
skills_ref_backend = FilesystemBackend(root_dir=_wm.data_root / "skill_refs", virtual_mode=True)
# 动态工作区：每次操作前从 WorkspaceManager 重新解析 root_dir，切换工作区即时生效
workspace_data_backend = DynamicFilesystemBackend(get_root_dir=lambda: _wm.active_workspace)
shell_backend = DynamicLocalShellBackend(get_root_dir=lambda: _wm.active_workspace, inherit_env=True)

composite_backend = CompositeBackend(
    default=shell_backend,
    # 中间产物（自动压缩 conversation_history / 溢出 large_tool_results）统一落到
    # 前端选择的工作区（{active_workspace}/conversation_history、.../large_tool_results）。
    # 默认 "/" 会把它们路由到 vfs_root_backend（项目外数据根），造成跨工作区混放。
    artifacts_root="/workspace/",
    routes={
        "/shared/memory/": shared_memory_backend,
        "/shared/skills/": shared_skills_backend,
        "/skill_refs/": skills_ref_backend,
        "/workspace/": workspace_data_backend,
        "/": vfs_root_backend,
    },
)
# SKILLS_REF 实验注入时换成物化版 skill，默认仍读共享磁盘 skill
skills_middleware = SkillsMiddleware(
    backend=vfs_root_backend,
    sources=effective_skills_sources(["/shared/skills/main/"], group="main"),
)
# 从运行时 context 读取前端传入的查询关键词，注入系统提示词，
# 使 LLM 委派判断与前端拦截判断使用同一份关键词。
query_keywords_middleware = QueryKeywordsMiddleware()
# 前端「开启思考过程」开关 → 每次模型调用按 configurable.enable_thinking 重建模型
thinking_toggle_middleware = ThinkingToggleMiddleware()
# 方案 B L1：工具结果进入 checkpoint 前瘦身——超大结果落盘截断（head+tail 预览 + 路径指针）、
# 完全重复结果去重为小占位。阈值由 MessageSlimmerMiddleware 从环境变量
# LARGE_RESULT_TRUNCATE_CHARS 读取（默认 8000；传 None 显式关闭截断）。
message_slimmer = MessageSlimmerMiddleware(backend=composite_backend)
# 把当前库名（configurable.db_name）注入最新用户消息，作为当轮最高优先级信号，
# 防止 LLM 被对话历史/总结里过时的库名误导（切库后仍按旧库委派）。
db_context_middleware = CurrentDbContextMiddleware()
# 模型输出后处理：把最终 AIMessage 里的 VFS 路径（/workspace/report/...）改写为真实
# 磁盘路径（D:/workspace1/report/...），让聊天显示可打开、含工作区名的路径。
vfs_path_resolver = VfsPathResolverMiddleware()
# execute（shell）护栏：文件权限管不住 execute（LocalShellBackend 直接跑宿主 shell），
# 这里拦截破坏性命令与工作区外路径引用（rm /tmp/x 等），见 execute_guard.py。
execute_guard = ExecuteGuardMiddleware()

nl2sql_async = AsyncSubAgent(
    name="nl2sql",
    description="NL2SQL查询专家",
    graph_id="nl2sql_agent",
)

# ── 自定义 State Schema（添加 todos 字段供前端任务进度条使用）──
def _merge_dict_by_key(existing: dict | None, update: dict) -> dict:
    """按 key 合并字典（与 deepagents._tasks_reducer 同语义）。

    用于并发多查询：各 sync 线程只写自己 task_id 的 key，
    LangGraph 在 update_state 时对 reducer 注解字段做合并，而非整值替换，
    从而避免多线程互相覆盖。
    """
    merged = dict(existing or {})
    merged.update(update)
    return merged


class MainAgentState(DeepAgentState):
    todos: NotRequired[list]
    # ── 并发多查询：按 task_id 键控的合并字段 ──
    query_headers:       Annotated[NotRequired[dict[str, dict]],  _merge_dict_by_key]  # task_id -> {id,content,status,task_id}
    subagent_steps_map:  Annotated[NotRequired[dict[str, list]],  _merge_dict_by_key]  # task_id -> [steps]
    active_queries:      Annotated[NotRequired[dict[str, bool]], _merge_dict_by_key]   # task_id -> True(运行)/False(完成)
    # ── 旧单值字段（保留供旧前端回退，仅单任务时镜像写入）──
    query_header: NotRequired[dict]        # 查询标题（同步进程写入，不被 write_todos 覆盖）
    subagent_steps: NotRequired[list]      # 子智能体步骤（同步进程写入，不被 write_todos 覆盖）
    query_active: NotRequired[bool]        # 查询进行中标记（前端拦截二次查询依据）
    # ── Token 计量（累积型：LLM 耗时、输入/输出/缓存/推理 token、步数）──
    token_stats: Annotated[NotRequired[dict], _accumulate_token_stats]


# ── Checkpointer ────────────────────────────────────────────────────
# 注意：checkpointer 不再在 graph 层设置，而是通过 LANGGRAPH_CHECKPOINTER 环境变量
# 在 langgraph_api 层配置。参见 checkpointer_factory.py 和 .env 文件。
# 如需在纯 Python（非 langgraph dev）模式下运行，取消下面的注释：
# import sqlite3
# _CHECKPOINT_DB = str(base_dir / "workspace" / "checkpoints.sqlite")
# _checkpoint_conn = sqlite3.connect(_CHECKPOINT_DB, check_same_thread=False)
# _checkpointer = SqliteSaver(_checkpoint_conn)

# 模型占位：无前端配置时 deepseek_model 为 None，用占位模型避免 create_deep_agent
# 走废弃默认模型（claude-sonnet-4-6）。真实模型由 ThinkingToggleMiddleware 每次请求
# 按 configurable 重建；未配置模型时前端已拦截，此占位不会被真正调用。
_agent_model = deepseek_model
if _agent_model is None:
    from langchain_openai import ChatOpenAI

    _agent_model = ChatOpenAI(
        api_key="__unconfigured__",
        base_url="http://127.0.0.1:0",
        model="__unconfigured__",
        temperature=0,
    )

trace_recorder = TraceRecorderMiddleware(
    db_path=str(_wm.shared_trace_db),
    agent_type="chat_agent",
)

agent = create_deep_agent(
    model=_agent_model,
    tools=[*mcp_tools, build_report_tool],
    subagents=[nl2sql_async],
    memory=["/shared/memory/ORCHESTRATOR.md"],  # AGENTS.md 改为按需加载，由主智能体在委派 nl2sql 时读取并拼入 prompt
    # vfs_path_resolver 放列表末尾（最内层、紧贴模型）：后处理在 langfuse_span /
    # trace_recorder 等外层记录之前完成，保证 trace、checkpoint、前端看到同一份真实路径。
    middleware=[QuotaErrorMiddleware(), ModelTimeoutMiddleware(), execute_guard, skills_middleware, query_keywords_middleware, thinking_toggle_middleware, message_slimmer, db_context_middleware, dynamic_prompt, TokenMeterMiddleware(), trace_recorder, LangfuseSpanMiddleware(agent_name="chat_agent"), vfs_path_resolver],
    backend=composite_backend,
    permissions=FILE_PERMISSIONS,  # 文件读写安全控制：只读根，仅 workspace/{report,tmp,nl2sql_process_data} 可写
    system_prompt=SYSTEM_PROMPT,
    state_schema=MainAgentState,
).with_config({
    "recursion_limit": 500,
    "callbacks": get_langfuse_callbacks(),  # Langfuse 全链路埋点（M1 监控上线；LANGFUSE_ENABLE=false 时空列表）
})


print(f"[MainAgent] ready", flush=True)