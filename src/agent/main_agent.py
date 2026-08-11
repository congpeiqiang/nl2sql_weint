"""主智能体 — 异步子智能体架构。支持取消长时间运行的查询。"""
import asyncio
from typing import Dict, Any

from langchain_core.messages import HumanMessage

"""主智能体 - 异步子智能体架构。"""
from pathlib import Path

# 增强 check_async_task 返回详细进度（必须在 create_deep_agent 之前导入）
import agent.subagents.check_progress  # noqa: F401
# 自动同步子智能体 todos 到主智能体 state（零前端改动方案）
import agent.subagents.sync_launcher  # noqa: F401
# 透传父 run 的 configurable 到异步子 agent run（前端选库 db_name 才能到达子 agent）
import agent.middlewares.deepagents_async_config_patch  # noqa: F401

from deepagents import create_deep_agent, AsyncSubAgent, DeepAgentState
from deepagents.backends import FilesystemBackend, CompositeBackend, LocalShellBackend
from deepagents.middleware import SkillsMiddleware
from agent.llms.model import deepseek_model
from agent.tools.mcp_tool import main_tools as mcp_tools
from agent.settings.setting import settings
from agent.middlewares.query_keywords import QueryKeywordsMiddleware
from agent.middlewares.message_slimmer import MessageSlimmerMiddleware
from typing import Annotated
from typing_extensions import NotRequired

# base_dir = Path(r"D:\code_work_space\llm\nl2sql\src\agent").resolve()
base_dir = Path(__file__).parent.resolve()
_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompt" / "MAIN_AGENT_PROMPT.md"


def _build_system_prompt() -> str:
    """根据 CHART_ENGINE 动态构建系统提示词，注入对应图表引擎规范"""
    base = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    engine = settings.CHART_ENGINE.lower()

    # 读取对应引擎的图表规范
    chart_spec_path = Path(__file__).parent / "prompt" / "chart_specs" / f"{engine}.md"
    if chart_spec_path.exists():
        chart_spec = chart_spec_path.read_text(encoding="utf-8")
    else:
        # 回退到 semiotic
        chart_spec_path = Path(__file__).parent / "prompt" / "chart_specs" / "semiotic.md"
        chart_spec = chart_spec_path.read_text(encoding="utf-8")
        engine = "semiotic"

    # 替换占位符
    prompt = base.replace("{{CHART_SPEC}}", chart_spec)
    prompt = prompt.replace("{{CHART_ENGINE_NAME}}", "Semiotic" if engine == "semiotic" else "ECharts")
    # prompt = prompt.replace("{{CHART_OUTPUT_FORMAT}}", "SVG" if engine == "semiotic" else "PNG")
    prompt = prompt.replace("{{CHART_OUTPUT_FORMAT}}", "html")

    return prompt


SYSTEM_PROMPT = _build_system_prompt()

file_backend = FilesystemBackend(root_dir=base_dir, virtual_mode=True)
shell_backend = LocalShellBackend(root_dir=Path(base_dir) / "workspace", inherit_env=True, virtual_mode=True)
composite_backend = CompositeBackend(default=shell_backend, routes={"/": file_backend})
skills_middleware = SkillsMiddleware(
    backend=file_backend,
    sources=["/workspace/skills/main/"]
)
# 从运行时 context 读取前端传入的查询关键词，注入系统提示词，
# 使 LLM 委派判断与前端拦截判断使用同一份关键词。
query_keywords_middleware = QueryKeywordsMiddleware()
# 方案 B L1：工具结果进入 checkpoint 前瘦身——超大结果落盘截断（head+tail 预览 + 路径指针）、
# 完全重复结果去重为小占位。阈值/开关见 MessageSlimmerMiddleware 构造参数。
message_slimmer = MessageSlimmerMiddleware(backend=composite_backend)

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


# ── Checkpointer ────────────────────────────────────────────────────
# 注意：checkpointer 不再在 graph 层设置，而是通过 LANGGRAPH_CHECKPOINTER 环境变量
# 在 langgraph_api 层配置。参见 checkpointer_factory.py 和 .env 文件。
# 如需在纯 Python（非 langgraph dev）模式下运行，取消下面的注释：
# import sqlite3
# _CHECKPOINT_DB = str(base_dir / "workspace" / "checkpoints.sqlite")
# _checkpoint_conn = sqlite3.connect(_CHECKPOINT_DB, check_same_thread=False)
# _checkpointer = SqliteSaver(_checkpoint_conn)

agent = create_deep_agent(
    model=deepseek_model,
    tools=mcp_tools,
    subagents=[nl2sql_async],
    memory=["/workspace/memory/ORCHESTRATOR.md"],  # AGENTS.md 改为按需加载，由主智能体在委派 nl2sql 时读取并拼入 prompt
    middleware=[skills_middleware, query_keywords_middleware, message_slimmer],
    backend=composite_backend,
    system_prompt=SYSTEM_PROMPT,
    state_schema=MainAgentState,
).with_config({"recursion_limit": 500})


# ── 调用包装器 ──────────────────────────────────────────────────
async def invoke_with_thread_id(state: Dict[str, Any], thread_id: str):
    """
    带 thread_id 的调用包装器
    确保每个会话使用独立的数据存储
    """
    # 执行 Agent
    result = await agent.ainvoke(
        state,
        config={"configurable": {"thread_id": thread_id}},
    )

    return result
print(f"[MainAgent] ready", flush=True)

# result = invoke_with_thread_id({"messages": HumanMessage("你好")}, "test")
# print(asyncio.run(result))