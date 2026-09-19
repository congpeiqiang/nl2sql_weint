"""DanglingToolCallsMiddleware — 每轮 model 前为 orphan tool_call_id 补合成 ToolMessage。

背景：生产 trace 513489a0bdc46e3cc130489d90880bb2（2026-09-05，nl2sql 子 agent
「上上周多少人没报工」）调 LLM 报 400：
  "An assistant message with 'tool_calls' must be followed by tool messages
   responding to each 'tool_call_id'. (insufficient tool messages ...)"
根因链：模型（deepseek-v4-flash thinking）单轮并发发 4 个 tool_call，其中
write_todos 的 arguments JSON 生成坏（键值二次转义 ""content""）→ langchain
json.loads 失败 → 记为 invalid_tool_call（id call_00_klZo…），工具节点不执行它、
无 tool 响应。下一轮 model 调用时 langchain_openai 1.3.5 `_convert_message_to_dict`
（base.py:405-411）把 valid + invalid **合并**进 payload tool_calls=4，但只跟了
3 条 tool 消息 → 400（E3 与本地转换实验已复现/验证）。

机制事实（2026-09-05 源码核实，langchain 1.3.9 factory.py / types.py:443）：
- `before_agent`（deepagents PatchToolCallsMiddleware 所在）是 entry 节点，**每 run
  一次**——只能清理 run 起点历史里的存量悬空，够不着 run 中途才产生的 orphan。
- `before_model` 是独立 graph 节点，**每次 model 调用前必触发**（START 与 tools
  回环都先进它，factory loop_entry）。它返回 `{"messages":[...]}` 经 messages 通道
  add_messages reducer 归并进 state，随后 model_node 构造
  ModelRequest(messages=state["messages"]) → 合成 ToolMessage 本轮即入 payload，
  且持久化在正确位置（后续轮次不再重复补）。
- 选 `before_model` 而非 `wrap_model_call`：后者只改出站 payload（override 不落
  state）；要落 state 得走 ExtendedModelResponse + Command，会排到 model 回复之后
  （乱序）。before_model 是唯一「补进 state 正确位置 + 同一轮被 model 读到」的挂点。

只扫最后一个 AIMessage 的论证：mid-run orphan 由模型**本轮**输出引入，只在紧随其
后的下一次 before_model 仍处于「尾部 assistant」位置；若漏补、assistant 退居非尾
部，则成为历史存量——那部分由 run 起点 PatchToolCallsMiddleware.before_agent 兜。
本中间件与 PatchToolCalls 同步上线的 graph 中，orphan 一定在产生当轮被补掉，永不
成为存量。扫描全部历史合成会造成 add_messages 落位错乱（插入点失控），故不做。

合成文本与 deepagents PatchToolCallsMiddleware 一致（可读且不触发 exec 误判）。
status="error" 语义上标记该 call 未能执行（与 QueryGate/SqlReadOnly 的 deny 消息
同构；OpenAI payload 序列化只取 content/role/tool_call_id，status 不入 payload）。

挂载：nl2sql_agent._middleware 与 main_agent.middleware 各一个实例（两 graph 独立，
无共享工厂；create_deep_agent 默认已注入 run 级 PatchToolCalls，本中间件补每轮）。
"""
from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

_logger = logging.getLogger(__name__)

# 合成 ToolMessage 文本（与 deepagents PatchToolCallsMiddleware 一致）
_INVALID_TMPL = (
    "Tool call {name} with id {tool_call_id} could not be executed - "
    "arguments were malformed or truncated."
)
_CANCELLED_TMPL = (
    "Tool call {name} with id {tool_call_id} was cancelled - "
    "another message came in before it could be completed."
)


def _collect_dangling_tool_messages(messages) -> list[ToolMessage]:
    """扫最后一个 AIMessage：其 tool_calls+invalid_tool_calls 中 id 未被任何
    tool 消息应答的 → 合成对应 ToolMessage（invalid→malformed；valid 未应答→
    cancelled 防御）。无 orphan → 返回空列表。纯函数，供单测。"""
    patches: list[ToolMessage] = []
    if not messages:
        return patches

    # 只取最后一个 AIMessage（论证见模块 docstring）
    last_ai = None
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            last_ai = m
            break
    if last_ai is None:
        return patches

    # 已被任何 tool 消息应答的 id（含紧跟其后、真实工具对 valid 的响应）
    answered_ids = {
        msg.tool_call_id for msg in messages if isinstance(msg, ToolMessage)
    }

    def _scan(seq: list, is_invalid: bool) -> None:
        # 按来源字段判别 invalid（而非 in-band type 键，版本更稳）：
        # invalid_tool_calls 字段里的条目即 invalid；tool_calls 里的即 valid。
        # ToolCall/InvalidToolCall 在当前 langchain-core 是 TypedDict(dict)，但历史
        # 版本可能是 pydantic 对象——统一 isinstance(dict) 兼容两种形态。
        for tc in seq:
            tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if not tid or tid in answered_ids:
                continue
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            name = name or "unknown"
            tmpl = _INVALID_TMPL if is_invalid else _CANCELLED_TMPL
            patches.append(
                ToolMessage(
                    content=tmpl.format(name=name, tool_call_id=tid),
                    name=name,
                    tool_call_id=tid,
                    status="error",
                )
            )

    _scan(last_ai.tool_calls, is_invalid=False)
    _scan(last_ai.invalid_tool_calls, is_invalid=True)
    return patches


class DanglingToolCallsMiddleware(AgentMiddleware):
    """before_model：每次 model 调用前为 orphan tool_call_id 补合成 ToolMessage。

    不实现其它 hook：run 起点存量悬空仍由 deepagents PatchToolCallsMiddleware
    (before_agent) 兜；本中间件只补 run 中途新产生的 orphan。
    """

    def _before(self, state, runtime) -> dict | None:  # noqa: ARG002
        try:
            msgs = (state or {}).get("messages") or []
            patches = _collect_dangling_tool_messages(msgs)
        except Exception:  # noqa: BLE001  fail-open：异常不影响正常流程
            _logger.warning("[Dangling] 扫描 orphan tool_call 失败，fail-open", exc_info=True)
            return None
        if not patches:
            return None  # 每轮必触发；无 orphan 零开销
        for p in patches:
            _logger.warning(
                "[Dangling] 为 orphan tool_call_id %s (%s) 补合成 ToolMessage(status=error)",
                p.tool_call_id, p.name,
            )
        # add_messages reducer 追加到消息尾部 = 紧跟该 assistant 的工具响应之后，顺序正确
        return {"messages": patches}

    def before_model(self, state, runtime):
        return self._before(state, runtime)

    async def abefore_model(self, state, runtime):
        return self._before(state, runtime)
