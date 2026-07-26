"""delegate 工具 — 子智能体实时执行过程可视化。

使用 graph.astream_events() 实现逐步骤推送。
"""

import json, logging
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)


def build_subagent_graphs(configs, model, available_tools):
    from deepagents import create_deep_agent
    tool_index = {getattr(t, "name", None): t for t in available_tools if getattr(t, "name", None)}
    graphs = {}
    for cfg in configs:
        name = cfg["name"]
        resolved = []
        for p in cfg.get("tools", []):
            for tn, t in tool_index.items():
                if p in tn and t not in resolved:
                    resolved.append(t)
        try:
            graphs[name] = create_deep_agent(model=model, tools=resolved, system_prompt=cfg["system_prompt"])
            _logger.info(f"[StreamTask] built: {name} ({len(resolved)} tools)")
        except Exception as e:
            _logger.error(f"[StreamTask] build failed {name}: {e}")
    return graphs


class TaskInput(BaseModel):
    description: str = Field(description="任务描述")
    subagent_type: str = Field(description="子智能体类型，如 nl2sql")


def create_streaming_task_tool(subagent_graphs: Dict[str, CompiledStateGraph]):
    def _call(subagent_type: str, description: str, run_manager=None) -> tuple:
        _logger.info(f"[StreamTask] SYNC: {subagent_type}")
        if subagent_type not in subagent_graphs:
            return (f"子智能体 {subagent_type} 不存在", {"steps": [], "total_steps": 0})
        
        graph = subagent_graphs[subagent_type]
        config = {"configurable": {"ls_agent_type": "subagent"}}
        
        msgs = []
        try:
            for event in graph.stream(
                {"messages": [HumanMessage(content=description)]},
                config,
                stream_mode="updates",
            ):
                for _, node_output in event.items():
                    if "messages" in node_output:
                        msgs.extend(node_output["messages"])
        except Exception as e:
            _logger.error(f"[StreamTask] SYNC failed: {e}")
            return (f"执行失败: {e}", {"steps": [], "total_steps": 0})

        return _build_result(msgs, subagent_type, description)

    async def _acall(subagent_type: str, description: str, run_manager=None) -> tuple:
        _logger.info(f"[StreamTask] ASYNC start: {subagent_type}")
        if subagent_type not in subagent_graphs:
            return (f"子智能体 {subagent_type} 不存在", {"steps": [], "total_steps": 0})
        
        graph = subagent_graphs[subagent_type]
        config = {"configurable": {"ls_agent_type": "subagent", "subagent_name": subagent_type}}
        
        msgs = []
        tool_count = 0
        try:
            async for event in graph.astream_events(
                {"messages": [HumanMessage(content=description)]},
                config,
                version="v2",
            ):
                kind = event.get("event", "")
                if kind == "on_tool_start":
                    name = event.get("name", "?")
                    tool_count += 1
                    _logger.info(f"[StreamTask] TOOL_START: {name}")
                    # Try to notify frontend via run_manager
                    if run_manager:
                        try:
                            run_manager.on_text(f"\n[子智能体] 步骤{tool_count}: 调用 {name}...", end="\n")
                        except:
                            pass
                elif kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk", {})
                    if hasattr(chunk, "content"):
                        _logger.info(f"[StreamTask] CHUNK: {str(chunk.content)[:80]}")
                elif kind == "on_tool_end":
                    output = event.get("data", {}).get("output", "")
                    _logger.info(f"[StreamTask] TOOL_END: result={str(output)[:100]}")
        except Exception as e:
            _logger.error(f"[StreamTask] ASYNC failed: {e}")
            return (f"执行失败: {e}", {"steps": [], "total_steps": 0})

        return _build_result(msgs, subagent_type, description)

    return StructuredTool.from_function(
        name="delegate",
        func=_call,
        coroutine=_acall,
        description="将任务委派给子智能体 nl2sql。实时显示子智能体执行步骤。",
        args_schema=TaskInput,
        response_format="content_and_artifact",
    )


def _build_result(messages: List, subagent_type: str, description: str) -> tuple:
    steps = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            if msg.content and msg.content.strip():
                steps.append({"step": len(steps)+1, "type": "ai", "content": msg.content[:300]})
            for tc in getattr(msg, "tool_calls", []) or []:
                steps.append({"step": len(steps)+1, "type": "tool_call", "name": tc.get("name","?"), "args": str(tc.get("args",{}))[:200]})

    content = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content and msg.content.strip():
            content = msg.content
            break
    if not content:
        content = f"子智能体未返回文本"

    _logger.info(f"[StreamTask] DONE: {len(steps)} steps")
    return (content, {"subagent_type": subagent_type, "description": description[:200], "steps": steps, "total_steps": len(steps)})
