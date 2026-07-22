"""delegate 工具 — 子智能体执行过程可视化。"""
import json, logging
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)


def build_subagent_graphs(
    configs: List[Dict],
    model,
    available_tools: List,
) -> Dict[str, CompiledStateGraph]:
    from deepagents import create_deep_agent
    tool_index = {}
    for t in available_tools:
        name = getattr(t, "name", None)
        if name:
            tool_index[name] = t
    graphs = {}
    for cfg in configs:
        name = cfg["name"]
        resolved_tools = []
        for pattern in cfg.get("tools", []):
            for tn, tobj in tool_index.items():
                if pattern in tn and tobj not in resolved_tools:
                    resolved_tools.append(tobj)
        try:
            sub = create_deep_agent(
                model=model, tools=resolved_tools,
                system_prompt=cfg["system_prompt"],
            )
            graphs[name] = sub
            _logger.info(f"[StreamTask] built: {name} ({len(resolved_tools)} tools)")
        except Exception as e:
            _logger.error(f"[StreamTask] build failed {name}: {e}")
    return graphs


class TaskInput(BaseModel):
    description: str = Field(description="任务描述")
    subagent_type: str = Field(description="子智能体类型，如 nl2sql")


def create_streaming_task_tool(
    subagent_graphs: Dict[str, CompiledStateGraph],
) -> Any:
    def _extract_steps(messages: List) -> List[Dict]:
        steps = []
        for msg in messages:
            if isinstance(msg, AIMessage):
                if msg.content and msg.content.strip():
                    steps.append({"step": len(steps) + 1, "type": "ai", "content": msg.content[:300]})
                for tc in getattr(msg, "tool_calls", []) or []:
                    steps.append({"step": len(steps) + 1, "type": "tool_call", "name": tc.get("name", "?"), "args":
str(tc.get("args", {}))[:200]})
        return steps

    def _build_content(messages: List, description: str) -> str:
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content and msg.content.strip():
                return msg.content
        return f"无文本返回: {description}"

    def _call(subagent_type: str, description: str) -> tuple:
        _logger.info(f"[StreamTask] ENTER: {subagent_type} desc={description[:50]}")
        if subagent_type not in subagent_graphs:
            return (f"{subagent_type} 不存在", {"steps": [], "total_steps": 0})
        graph = subagent_graphs[subagent_type]
        config = {"configurable": {"ls_agent_type": "subagent", "subagent_name": subagent_type}}
        try:
            result = graph.invoke({"messages": [HumanMessage(content=description)]}, config)
        except Exception as e:
            _logger.error(f"[StreamTask] invoke failed: {e}")
            return (f"执行失败: {e}", {"steps": [], "total_steps": 0})
        msgs = result.get("messages", [])
        content = _build_content(msgs, description)
        steps = _extract_steps(msgs)
        artifact = {"subagent_type": subagent_type, "steps": steps, "total_steps": len(steps)}
        _logger.info(f"[StreamTask] DONE: {len(steps)} steps")
        return (content, artifact)

    async def _acall(subagent_type: str, description: str) -> tuple:
        _logger.info(f"[StreamTask] ENTER async: {subagent_type}")
        if subagent_type not in subagent_graphs:
            return (f"{subagent_type} 不存在", {"steps": [], "total_steps": 0})
        graph = subagent_graphs[subagent_type]
        config = {"configurable": {"ls_agent_type": "subagent", "subagent_name": subagent_type}}
        try:
            result = await graph.ainvoke({"messages": [HumanMessage(content=description)]}, config)
        except Exception as e:
            _logger.error(f"[StreamTask] async invoke failed: {e}")
            return (f"执行失败: {e}", {"steps": [], "total_steps": 0})
        msgs = result.get("messages", [])
        content = _build_content(msgs, description)
        steps = _extract_steps(msgs)
        artifact = {"subagent_type": subagent_type, "steps": steps, "total_steps": len(steps)}
        _logger.info(f"[StreamTask] DONE async: {len(steps)} steps")
        return (content, artifact)

    return StructuredTool.from_function(
        name="delegate",
        func=_call,
        coroutine=_acall,
        description="将任务委派给子智能体 nl2sql。返回完整执行步骤。",
        args_schema=TaskInput,
        response_format="content_and_artifact",
    )