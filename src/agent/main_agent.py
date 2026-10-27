"""主智能体 — 异步子智能体架构。支持取消长时间运行的查询。"""
"""主智能体 - 异步子智能体架构。"""
import logging
from pathlib import Path

_logger = logging.getLogger(__name__)

# 运行时猴子补丁集中入口（Phase 2-3）。原先这 4 个补丁靠下方分散的 `# noqa: F401`
# import 触发；现在集中到 bootstrap.apply_patches()，并在 start_server.py 早期显式调用。
# 此处保留一次兜底调用：走 `langgraph dev` / 直接 import main_agent 的入口不经过
# start_server.py，少了这一行补丁就静默不生效（幂等，重复调用无副作用）。
from agent.bootstrap import apply_patches

apply_patches()

from deepagents import create_deep_agent, AsyncSubAgent, DeepAgentState
from deepagents.backends import FilesystemBackend, CompositeBackend
from agent.backends.dynamic_workspace import DynamicFilesystemBackend, DynamicLocalShellBackend
from deepagents.middleware import SkillsMiddleware
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
    ModelRequest,
    dynamic_prompt,
)
from agent.llms.model import deepseek_model
from agent.tools.mcp_tool import main_tools as mcp_tools
from agent.tools.report_builder import build_report_tool
# 归因分解（S3）：与 build_report 同层 —— 指纹在主 state（读指纹是一次取值，
# 无跨图协议），cube 元信息有进程内 reader，追问归因不必再起子 run（方案 §4.4）。
from agent.tools.attribution_analyze import attribution_analyze_tool
# 定时任务（阶段3）：单工具多 action，只调 agent.scheduler.service（与 REST 同一实现层）
from agent.tools.manage_scheduled_task import manage_scheduled_task_tool
from agent.settings.file_permissions import FILE_PERMISSIONS
from agent.middlewares.query_keywords import QueryKeywordsMiddleware
from agent.middlewares.thinking_toggle import ThinkingToggleMiddleware
from agent.middlewares.message_slimmer import MessageSlimmerMiddleware
from agent.middlewares.knowledge_trim import KnowledgeTrimMiddleware
from agent.middlewares.current_db_context import CurrentDbContextMiddleware
from agent.middlewares.dangling_tool_calls import DanglingToolCallsMiddleware
from agent.middlewares.token_meter import TokenMeterMiddleware, _accumulate_token_stats
from agent.middlewares.trace_recorder import TraceRecorderMiddleware
from agent.middlewares.langfuse_span import LangfuseSpanMiddleware
from agent.middlewares.vfs_path_resolver import VfsPathResolverMiddleware
from agent.middlewares.execute_guard import ExecuteGuardMiddleware
from agent.middlewares.chart_artifact_owner import ChartArtifactOwnerMiddleware
from agent.middlewares.quota_error import QuotaErrorMiddleware
from agent.middlewares.model_timeout import ModelTimeoutMiddleware
from agent.middlewares.pinned_replay import PINNED_REPLAY_SECTION, PinnedReplayMiddleware
# 账号级模型门禁（2026-09-28）：新账号不继承共享模型配置，没配就明确报错、绝不回落。
# 必须最外层（列表首元素）：命中时连模型实例都不构造。
from agent.middlewares.model_required import ModelRequiredMiddleware
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
        # 注意这行在**模块导入期**执行（L85 `SYSTEM_PROMPT = _build_system_prompt()`），
        # 所以这里当年若名字解析不了（`_log` 未定义）不是「少打一条日志」，而是
        # **整个 agent 起不来**。测试文件缺失才会走到这一支，属于潜伏炸弹。
        _logger.warning("[main_agent] chart_specs/echarts.md 不存在，图表规范将缺失")

    # 替换占位符
    prompt = base.replace("{{CHART_SPEC}}", chart_spec)
    prompt = prompt.replace("{{CHART_ENGINE_NAME}}", "ECharts")
    # prompt = prompt.replace("{{CHART_OUTPUT_FORMAT}}", "PNG")
    prompt = prompt.replace("{{CHART_OUTPUT_FORMAT}}", "html")

    return prompt


SYSTEM_PROMPT = _build_system_prompt()


def _get_configurable() -> dict:
    """当前 run 的 configurable（``request.runtime.config`` 恒空，必须走这里）。

    单独成函数是为了测试 monkeypatch——与 ``middlewares/pinned_replay._cfg``
    各自独立实现，不互相 import（一个读 db_name、一个读 pinned_replay，
    合在一起会让两个模块平白耦合）。
    """
    try:
        from langgraph.config import get_config as _cfg

        if _cfg is not None:
            return _cfg().get("configurable", {}) or {}
    except Exception:  # noqa: BLE001  读取失败则走默认
        pass
    return {}


@dynamic_prompt
def dynamic_prompt(request: ModelRequest) -> str:
    """从 configurable 读当前数据库名并注入系统提示词。

    主 agent 的 system prompt 是静态文件（MAIN_AGENT_PROMPT.md），不含 db_name。
    前端切库后 configurable.db_name 是最新的，但主 agent LLM 读不到——委派子 agent 时
    【数据库名称】会写错（默认 imdb）。本函数把当前库名动态注入，
    保证 start_async_task 的 description 写对库名（与子 agent 的 dynamic_prompt 同构）。

    固化回放 run（``pinned_replay``）额外在最前面拼 ``PINNED_REPLAY_SECTION``：
    数据已经在上下文里了，必须先讲清楚「不要再派子任务」。
    """
    # 用 .text 属性取纯文本（content 可能是 str 也可能是 block 列表，
    # 子 agent 是列表故用 content[-1]["text"]，主 agent 是 str 会炸）。
    prompt = getattr(request.system_message, "text", None) or ""
    if not prompt:
        return ""
    cfg = _get_configurable()
    db_name = cfg.get("db_name", "") or ""
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
    if cfg.get("pinned_replay"):
        # 回放段再前置一次：它比「当前数据库」还硬——本轮连委派都不许，
        # 而 db_section 的通篇都在讲「委派时怎么报库名」。
        prompt = PINNED_REPLAY_SECTION + prompt
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
# skill 版本化：SKILLS_REF 物化目录（<data_root>/offline_experiment/skill_refs/），
# skill 脚本经 VFS 也读物化版
skills_ref_backend = FilesystemBackend(
    root_dir=_wm.offline_experiment_dir / "skill_refs", virtual_mode=True
)
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
        "/offline_experiment/skill_refs/": skills_ref_backend,
        "/workspace/": workspace_data_backend,
        "/": vfs_root_backend,
    },
)
# SKILLS_REF 实验注入时换成物化版 skill，默认仍读共享磁盘 skill
# 2026-09-25：skills/ 按组分挂——main/ 为主智能体技能，nl2sql/ 现为 wren-* 语义层 SOP
# 编排集（原自研八步流水线已归档 /shared/skills_bak/）；主智能体不加载 wren-*。
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
# 知识料按问题裁剪（P1-2）。⚠️ 主 agent 的 MCP 工具只有 echarts，**没有 wren 知识工具**
# ⇒ 这里当前是 no-op；挂上只为与 nl2sql 组合根同构（将来主 agent 拿到 wren 工具即自动生效）。
knowledge_trim = KnowledgeTrimMiddleware()
# 把当前库名（configurable.db_name）注入最新用户消息，作为当轮最高优先级信号，
# 防止 LLM 被对话历史/总结里过时的库名误导（切库后仍按旧库委派）。
db_context_middleware = CurrentDbContextMiddleware()
# 模型输出后处理：把最终 AIMessage 里的 VFS 路径（/workspace/report/...）改写为真实
# 磁盘路径（D:/workspace1/report/...），让聊天显示可打开、含工作区名的路径。
vfs_path_resolver = VfsPathResolverMiddleware()
# execute（shell）护栏：文件权限管不住 execute（LocalShellBackend 直接跑宿主 shell），
# 这里拦截破坏性命令与工作区外路径引用（rm /tmp/x 等），见 execute_guard.py。
execute_guard = ExecuteGuardMiddleware()
# P1-17：execute 产出的图表（chart-saver skill 的 .svg/.png）登记归属。挂在这里是唯一
# 能做对的地方——子进程拿不到请求身份（见 chart_artifact_owner.py 文件头）。
chart_owner = ChartArtifactOwnerMiddleware()
# 定时任务审批闸门（2026-10-22）：manage_scheduled_task 的变更类动作会让 agent 安排
# 无人值守的未来运行，执行前必须经用户确认；list/runs 只读放行，避免每问一句都弹卡。
# 允许 edit：模型把「每天九点」解析成 cron 有偏差时，用户可在卡上直接改参数再批准
# （edit 后的 args 仍过 pydantic + service 校验，坏参数只会返回机读错误，不会静默
#  写坏任务）。action 缺失/非法时交给工具自身校验报错，不弹无意义的审批卡。
_SCHEDULED_MUTATING_ACTIONS = frozenset(
    {"create", "pause", "resume", "remove", "run_now"}
)


def _current_configurable() -> dict:
    """读当前 run 的 configurable。

    项目定式：必须走 ``langgraph.config.get_config()``（thinking_toggle.py
    2026-08-14 实证；直接读 runtime 拿不到）。读不到时返回空 dict。
    """
    try:
        from langgraph.config import get_config as _get_config

        cfg = _get_config() or {}
    except Exception:  # noqa: BLE001 —— 无 run 上下文（测试/离线调用）
        return {}
    return cfg.get("configurable") or {}


def _scheduled_hitl_when(request) -> bool:
    """manage_scheduled_task 是否需要人工审批（True=弹卡）。

    - 只读 action（list/runs）→ 不审批；
    - 定时触发的后台 run（configurable.scheduled_run，worker._fire 写入）→ 不审批：
      无人值守，interrupt 会被 langgraph 顶层图抑制（工具不执行、也没有卡片可点），
      这里直接放行（副作用仍过 service 校验 + owner 权限）；
    - 其余交互式 run → 审批；读不到 config 也按审批兜底（fail-closed 到人）。
    """
    action = (request.tool_call.get("args") or {}).get("action")
    if action not in _SCHEDULED_MUTATING_ACTIONS:
        return False
    return not bool(_current_configurable().get("scheduled_run"))


scheduled_hitl = HumanInTheLoopMiddleware(
    interrupt_on={
        "manage_scheduled_task": InterruptOnConfig(
            allowed_decisions=["approve", "reject", "edit"],
            description="该操作会创建或修改定时任务，请确认参数后批准。",
            when=_scheduled_hitl_when,
        )
    },
)

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
    # ── S8「答案指纹」：任务 id -> 这次算的是哪个库/哪个指标/哪条 SQL/哪两期 ──
    # 由 check_progress 在图内随 check 结果一起写回（见 _build_answer_fingerprint）；
    # 追问归因（attribution_analyze）靠它拿到上一轮的绝对两期区间与 db_name。
    answer_fingerprints: Annotated[NotRequired[dict[str, dict]], _merge_dict_by_key]


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

# 主 Agent 中间件链（外→内）——提取为模块级变量供测试断言顺序。
# vfs_path_resolver 放列表末尾（最内层、紧贴模型）：后处理在 langfuse_span /
# trace_recorder 等外层记录之前完成，保证 trace、checkpoint、前端看到同一份真实路径。
MAIN_AGENT_MIDDLEWARE: list = [
    ModelRequiredMiddleware(),
    QuotaErrorMiddleware(),
    ModelTimeoutMiddleware(),
    # 定时任务审批闸门：紧跟三道模型门禁之后。after_model 钩子按列表**逆序**执行
    # （langchain factory 从列表尾节点起链），放这里等于「trace/langfuse/瘦身等
    # 都处理完这轮模型输出之后才拦」，被拦那轮的记录不会丢。
    scheduled_hitl,
    # 固化回放：紧跟审批闸门（同属「拦截模型可见能力」这一层）。回放 run 已有数据，
    # 摘掉 start_async_task 让模型根本看不到委派能力。
    PinnedReplayMiddleware(),
    execute_guard,
    chart_owner,
    skills_middleware,
    query_keywords_middleware,
    thinking_toggle_middleware,
    message_slimmer,
    knowledge_trim,
    db_context_middleware,
    DanglingToolCallsMiddleware(),
    dynamic_prompt,
    TokenMeterMiddleware(),
    trace_recorder,
    LangfuseSpanMiddleware(agent_name="chat_agent"),
    vfs_path_resolver,
]

agent = create_deep_agent(
    model=_agent_model,
    tools=[*mcp_tools, build_report_tool, attribution_analyze_tool, manage_scheduled_task_tool],
    subagents=[nl2sql_async],
    memory=["/shared/memory/ORCHESTRATOR.md"],  # AGENTS.md 改为按需加载，由主智能体在委派 nl2sql 时读取并拼入 prompt
    middleware=MAIN_AGENT_MIDDLEWARE,
    backend=composite_backend,
    permissions=FILE_PERMISSIONS,  # 文件读写安全控制：只读根，仅 workspace/{report,tmp,nl2sql_process_data} 可写
    system_prompt=SYSTEM_PROMPT,
    state_schema=MainAgentState,
).with_config({
    "recursion_limit": 500,
    "callbacks": get_langfuse_callbacks(),  # Langfuse 全链路埋点（M1 监控上线；LANGFUSE_ENABLE=false 时空列表）
})


print(f"[MainAgent] ready", flush=True)