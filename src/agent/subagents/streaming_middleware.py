"""
StreamingDelegateMiddleware v2 — 多步 Command 实现实时子智能体显示。

原理：
1. 拦截 delegate 工具调用
2. 返回 Command(goto="_delegate_start") 进入自定义执行循环
3. _delegate_step 节点逐步执行，每一步通过 Command.update 更新 state
4. 前端 SSE values 流每步都可见
"""

import logging
from typing import Any, Dict, List
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
_logger = logging.getLogger(__name__)


class StreamingDelegateMiddleware(AgentMiddleware):
    def __init__(self, subagent_graphs: Dict[str, Any], delegate_tool_name: str = "delegate"):
        self.subagent_graphs = subagent_graphs
        self.delegate_tool_name = delegate_tool_name

    def wrap_model_call(self, request, handler):
        return handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler):
        response = await handler(request)
        msgs = response.result if hasattr(response, 'result') else response
        last_msg = msgs[-1] if isinstance(msgs, list) else msgs
        tool_calls = getattr(last_msg, "tool_calls", []) or []

        for tc in tool_calls:
            if tc.get("name") == self.delegate_tool_name:
                subagent_type = tc["args"].get("subagent_type", "")
                _logger.info(f"[StreamDelegate] intercepted: {subagent_type}")
                # Store delegate state and route to execution node
                return Command(
                    update={
                        "messages": [],
                        "_delegate": {
                            "subagent_type": subagent_type,
                            "description": tc["args"].get("description", ""),
                            "tool_call_id": tc["id"],
                            "steps": [],
                            "iterator": None,
                            "done": False,
                        }
                    },
                    goto="_delegate_step"
                )
        return response
