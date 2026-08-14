"""WriteTodosProtocolMiddleware — 追加 write_todos 强制协议。

背景：deepagents 基础栈的 `TodoListMiddleware` 会给最终 system prompt 追加默认指引
`WRITE_TODOS_SYSTEM_PROMPT`，其中明确写着 *"simple/few-step 请求不要用 write_todos"*。
它追加在项目 system_prompt（「原则 9 进度追踪」的强要求）**之后**，而简单查询
（如"查表数量"）恰好命中"可跳过"规则 → LLM 判定跳过 → 子线程 todos 为空 →
前端进度条只能靠 `deriveStepsFromSubMessages` 兜底显示原始工具名（"保存中间结果"/"edit_file"）。

本中间件必须放在子智能体 `middleware` 列表**末尾**：langchain 组合中间件时
list 第一个是最外层（[factory.py:240](langchain/agents/factory.py) "first in list becomes
outermost layer"），最内层后追加的 system 文本落在最终 system prompt 的**末尾**。
故本协议文本成为最后一条指令，直接压过默认"可跳过"指引。

本文件由 nl2sql_agent.py 引入；主智能体（chat_agent）已有稳定的委派/图表/报告 todos，
不需要本协议。
"""
from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

# 协议文本（追加到 system prompt 末尾；措辞必须压过 TodoListMiddleware 的
# "simple/few-step 请求不要用"默认说明）
WRITE_TODOS_PROTOCOL = """\
## `write_todos` 强制协议（必须遵守——覆盖上面"简单任务可跳过"的默认说明）

在本 NL2SQL 流水线中，`write_todos` 不是可选项，而是硬性要求，**无论问题看起来多简单**（如"有多少张表"）：

1. **开工前先初始化**：执行任何查询步骤之前，必须先调用一次 `write_todos` 创建完整步骤列表，
   条目与本次实际经过的流水线阶段一一对应（策略 A 用系统提示词「原则 9」示例的列表；
   策略 B/C 列出实际经过的阶段）。即使全程只有 1-2 步也必须创建。
2. **完成一步立即更新**：每完成一个步骤，立刻调用 `write_todos` 将该步置为 `completed`、
   下一步置为 `in_progress`。禁止攒到流程结束才批量更新。
3. **content 用阶段名**：todos 的 content 用可读阶段名（如 `Knowledge Loader`、`Schema Linking`、
   `SQL生成与验证`、`查询执行`），与「原则 9」示例一致，供前端进度条展示。

违反上述任何一条都视为流程缺陷。"""


class WriteTodosProtocolMiddleware(AgentMiddleware):
    """把 write_todos 强制协议追加到 system prompt 末尾。"""

    def wrap_model_call(self, request, handler):
        return self._inject(request, handler)

    async def awrap_model_call(self, request, handler):
        # 必须 await：async 链中 handler 是 async callable，不 await 会把
        # coroutine 对象原样返回，外层 _to_composed_result 拿到 coroutine，
        # _build_commands 的 `model_response.result` 报
        # `'coroutine' object has no attribute 'result'`（2026-08-14 实证）。
        return await self._inject(request, handler)

    def _inject(self, request, handler):
        if request.system_message is not None:
            new_system_content = [
                *request.system_message.content_blocks,
                {"type": "text", "text": f"\n\n{WRITE_TODOS_PROTOCOL}"},
            ]
        else:
            new_system_content = [{"type": "text", "text": WRITE_TODOS_PROTOCOL}]
        new_system_message = SystemMessage(content=new_system_content)
        return handler(request.override(system_message=new_system_message))
