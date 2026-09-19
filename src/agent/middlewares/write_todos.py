"""WriteTodosProtocolMiddleware — 追加 write_todos 分级协议。

deepagents 基础栈的 `TodoListMiddleware` 会给最终 system prompt 追加默认指引
`WRITE_TODOS_SYSTEM_PROMPT`，其中明确写着 *"simple/few-step 请求不要用 write_todos"*。
此前为了前端进度条有数据，使用强硬协议文本压过默认指引，强制所有查询都执行 write_todos。

但简单查询（策略 B/快速通道）仅为进度条调用 write_todos 会引入额外 LLM 轮次：
一次 write_todos 初始化 + 1-2 次状态更新，在 qwen3.7-max 等不稳定时刻显著增加延迟。
前端已有 `deriveStepsFromSubMessages` 兜底（从工具调用序列推导步骤），
因此策略 B 可安全跳过 write_todos。

本中间件放在子智能体 `middleware` 列表末尾，与 TodoListMiddleware 的默认指引
兼容共存——策略 A/C 是强要求，策略 B 可跳过。

本文件由 nl2sql_agent.py 引入；主智能体（chat_agent）已有稳定的委派/图表/报告 todos，
不需要本协议。
"""
from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

# 协议文本（追加到 system prompt 末尾；与 TodoListMiddleware 默认指引兼容共存，
# 而非覆盖——策略 A/C 要求 write_todos，策略 B 可跳过）
WRITE_TODOS_PROTOCOL = """\
## `write_todos` 分级使用指引

本 NL2SQL 流水线中 `write_todos` 按策略分级使用：

1. **策略 A（标准流水线）/ C（Cube 通道）**：步骤多（7-8 步），必须开工前初始化完整步骤列表，
   每步完成时立即更新。content 用阶段名（如 `理解建模-清晰度与知识`、`Schema 提取与裁剪`、
   `SQL 生成与验证`、`查询执行`）。
2. **策略 B（快速通道，单表/简单筛选/计数）**：步骤少（≤3 步），可跳过 write_todos——
   系统会自动从工具调用序列推导进度，无需手动维护。
3. **执行某步前先 read_file 该步所属 SKILL.md**（判据表/输出契约在正文里）；不要
   read_file 与当前步骤无关的 SKILL.md。

### 中途更新是免费的：与干活工具同一条消息并行发出（重要）

**write_todos 必须与"完成该步的那个实质工具调用"放在同一条 assistant 消息里并行发出**，
禁止单独为写 todo 多发一轮（那是白费的延迟和 token）。

- 并行完全合法：唯一禁止是**同一条消息里 ≥2 个 `write_todos`**；`write_todos` + `run_sql`
  或 `write_todos` + `describe_schema` 等并行不受限。
- 正确节奏（在发实质工具调用的同一轮里带上 write_todos）：
  - 前段是统一的理解建模（`nl2sql-understand`）：先 get_context + get_instructions 做
    清晰度裁决（clear=false 停），clear 后再 get_all_knowledge + recall_queries 加载知识；
    首次 schema 检索工具（`describe_schema`/`get_mdl`/`describe_model`）发出时 →
    把 `理解建模-清晰度与知识` completed、`Schema 提取与裁剪` in_progress；
  - 首个执行工具（`dry_run`/`run_sql`/`query_cube`）发出时 → 把 `Schema 提取与裁剪`、
    `Subproblem 分解`、`Query Plan 生成`、`SQL 生成与验证`、`性能优化` completed、
    `查询执行` in_progress；
  - run_sql 返回后 → 把 `查询执行` completed、`结果汇总` in_progress（最终回复真正写完
    才把最后一步标 completed，禁止提前全勾）。
"""


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
