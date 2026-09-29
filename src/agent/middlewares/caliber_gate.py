"""CaliberGateMiddleware — 逼子 agent 在「业务口径」里逐条引知识库原文。

## 为什么要确定性闸

`NL2SQL_SYSTEM_PROMPT.md` §十一第 3 条与 `wren-execution/SKILL.md` 结果呈现第 4 条
**本来就写着**「内容取原文」「出处必须是知识库里真实存在的文件名」，模型仍然不守。
生产实证（trace `9c81d3181f2a72f27cf9d092d7185fab`）：该 run 明明调过
`wrenai_witops_get_instructions`（20921 字符，含 `业务域口径`/`报工与工时`/`R1`/`R3`），
报告里却写成「内容 = 自己的转述、出处 = `workhour_analysis（Cube）` / `v_workhour`
/ `语义库字段字典`」。⇒ 纯提示词已证伪，必须程序化兜底。

## 本中间件只「逼」，不下判决

核验结论**只在报告装配时产生**（`report_builder` 用同一份磁盘语料重算，见
`utils/caliber_evidence.py` 模块 docstring），本中间件只负责在终态答案不合规时把模型
打回重写。这样：
- 判决只有一处，不会出现「中间件说合规、报告说未核验」的两套账；
- 重试耗尽也不需要往消息里塞结论（原文本会被摘要掉），报告侧独立重算即如实标注。

⚠️ 因此本中间件**不往答复里写任何「核验结论」行**。曾经设计过「重试耗尽时在口径块里追加
一行 `> 口径核验：…`，让主 agent 一起抽走」，**已否决**：报告侧能摘到的这类行只可能是
**模型自己写的**自评（「本表已逐字核验通过」），把它印进报告就是「报告撒谎」换个人称。
核验结论必须由代码产生 —— 见 `report_builder._render_business_caliber` 的四态脚注。

## 两条执法分支（失败形态不同，预算也分开）

| 终稿形态 | 判定 | 处置 | 预算 |
|---|---|---|---|
| 有 `## 业务口径` 块，但条目核验不过 | `_after` 主体 | 打回列出**哪几条**不合格 | `_MAX_RETRIES`=2 |
| **整节没有**块，且本轮取过知识料 | `_no_block` | 打回要求补块（或声明「本次未依据知识库口径」） | `_MAX_MISSING_RETRIES`=1 |
| 末条是**失败终局**（`failure_signal` 有戳） | `_after` 开头 | **不打回**，放行让回合结束 | — |

第二条是 2026-09-26 生产 trace `f222a8a5…` 逼出来的：那一轮**提示词 v4 + 本中间件都已在线上**，
子 agent 手里有 20.9k 口径原文（含 R1/R6/R7/R8）却整节省略，报告因此完全没有业务口径节。
原先「只对已存在的块执法」等于把「省略」变成最省事的过关方式 —— 契约文本两次被证伪
（先造假出处、再整节省略），只剩程序化兜底这一条路。

第三条是 2026-09-28 生产 trace `3dcc9a66…` 逼出来的：超时被 `ModelTimeoutMiddleware` 吞成一条
友好文档后掉进 `_no_block`，两条逃生口对失败消息都不成立 ⇒ 打回 → 图又发起第二次 240s 调用，
用户看到 234s 空窗、整轮 620.8s 一 SQL 未执行。**失败消息不是"没写完的终稿"，它没有终稿可言**
（`agent/utils/failure_signal.py` 有完整事故链）。

## 重答机制：`after_model` + `jump_to="model"`

三个已实测/已核源码的事实（细节见 `.venv/.../langchain/agents/factory.py`）：

1. `after_model` **每轮必跑**（`:1738` 无条件 `add_edge("model", …after_model)`），
   终态轮（无 tool_calls）也跑；
2. 但**只往 state 注入消息不会让模型重答**——`:1867` 见 `tool_calls` 为空直接退出循环；
   重答的正规通道是 `jump_to`（`:1849-1855` 优先读它 → `_resolve_jump(..., "model")`），
   库注释写明这是给 after_model 钩子用的（HITL 同款）。`jump_to` 是 `EphemeralValue`
   （`middleware/types.py:351`），每个 superstep 后自动清空 ⇒ **不会死循环**。
3. ⚠️ **必须显式 `@hook_config(can_jump_to=["model"])`**，因为**我们不是
   `loop_exit_node`**。`jump_to` 只在本节点的出边是**条件边**时才会被读：

   - `loop_exit_node = middleware_w_after_model[0]`（`:1610`）＝列表里**第一条**带
     after_model 的中间件。有 tools 时它的出边是 `_make_model_to_tools_edge`
     （`:1640-1671`；destinations 因「存在 after_model 钩子」而加了 `loop_entry_node`，
     库注释原文 *allows jump_to to model potentially artificially injected tool messages,
     ex HITL*），那条边**无条件读 `jump_to`** ⇒ 它**不写装饰器也能跳**。
   - 其余每个节点靠 `_add_middleware_edge`（`:1957-2000`）建边：`can_jump_to` 非空才建
     条件边，**空则退化成 `graph.add_edge(name, default_destination)` 一条平边** ——
     `jump_to` 被静默丢弃、注入的消息变成末条污染尾部。
   - deepagents 把 `TodoListMiddleware()` 恒定放在整条 middleware 栈首位
     （`deepagents/graph.py:773-775`，用户 middleware 到 `:812` 才 extend 进去）
     ⇒ 我们**永远不是** `middleware_w_after_model[0]` ⇒ 那条「不写装饰器也能跳」的
     便宜路与我们无关，装饰器是**硬要求**。

   实测四组合（`create_agent` + tools，与生产同形；复现见
   `scripts/verify_caliber_evidence.py` t10）：

   | 位置 | 装饰器 | 结果 |
   |---|---|---|
   | `[1]`（前面还有一个 after_model 中间件） | 有 | ✅ 模型被调 2 次（打回生效） |
   | `[1]` | **无** | ❌ 只调 1 次，末条变成纠正提示（尾部污染） |
   | `[0]` = `loop_exit_node` | 有 | ✅ 2 次 |
   | `[0]` = `loop_exit_node` | 无 | ✅ 2 次（那条边无条件读 `jump_to`） |

   另有一个静默坑：hook 的第二参数**必须叫 `runtime`**（langchain 按参数名注入），
   改叫 `r` 会在运行期抛 `missing 1 required positional argument`。

## 挂载位置

`nl2sql_agent._middleware` 里 **`ProgressBoundaryMiddleware` 之前**（列表 index 更小）。
after_model 链是 `model → [末] → … → [0]`（index 越大跑得越早），所以 index 更小 =
跑得更晚 = `ProgressBoundary` 已经推进完 todos 再打回；反过来的话打回会跳过它本轮推进。
"""
from __future__ import annotations

import logging
import os

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage

from agent.utils.caliber_evidence import (
    is_caliber_evidence_tool,
    load_knowledge_corpus,
    parse_caliber_block,
    verify_caliber_entries,
)
from agent.utils.failure_signal import failed_mark

_logger = logging.getLogger(__name__)

# 注入消息前缀：**必须**用仓内既有的 `[系统自动通知]`。有四处过滤器认它，换了前缀就会
# 踩到副作用：前端问题标题（sync_subagent_todos.py:1474 精确前缀）、评测（eval_subject.py:115）、
# 报告轮次锚点（report_builder.py:583）、Langfuse trace 归并（langfuse_client.py:684）。
_RETRY_TAG = "[系统自动通知] 口径核验未通过"
# 「整节缺失」用**另一个**标签 ⇒ 它的重试预算独立（见 `_MAX_MISSING_RETRIES`）。不复用
# `_RETRY_TAG` 是为了让 state 推导的计数把两类打回分开算：「缺块 → 补块 → 出处不合格」
# 是一条**正常的升级路径**，共用一个计数器会让它被误伤成「已用完」。
_MISSING_TAG = "[系统自动通知] 业务口径块缺失"
_MAX_RETRIES = int(os.environ.get("CALIBER_EVIDENCE_MAX_RETRIES", "2") or "2")
# 缺块只打回 **1** 次：料在手里，提醒一次就会补；再多是白烧模型轮次。
_MAX_MISSING_RETRIES = int(os.environ.get("CALIBER_MISSING_MAX_RETRIES", "1") or "1")

# 「确实一条口径都没用」的**逃生声明**，命中即放行（不再打回），让循环有终点。
# ⚠️ 刻意**不**把它渲染进报告（见模块 docstring「本中间件只逼、不下判决」）：它是模型自述，
# 印出去等于「报告替模型背书」，而它恰恰是免除打回的捷径、最容易被滥用。
_NO_CALIBER_MARKS = ("本次未依据知识库口径", "本次未使用业务口径", "未依据知识库口径")

# 澄清轮标记（query_gate 定义）：追问更可能是终态但不该被口径闸打断。
_CLARIFY_MARK = "[需要澄清]"

_CORRECTION = """{tag}：本次答复里「## 业务口径」表的以下条目**没有通过程序化核验**：

{problems}

请**重发完整最终答复**（不是只发口径块，也不是发一句"已修正"——短答复会被结果摘要
当成收尾语丢弃，用户将看到你上一版**未修正**的答案）。要求：

1. `内容` 必须是知识库原文的**逐字片段**：从你本轮取料结果里直接复制，不要翻译、概括、
   合并成"人话"；长条目可用 `…` 省略中段（最多 3 处，省略的字数不得多于引到的字数）。
2. `出处` 必须是知识库里**真实存在的 `.md` 文件名**（可带目录前缀与条目号，如
   `rules/报工与工时.md R3`）。表格名、视图名（`v_*`）、Cube 名、字段字典、MDL
   都**不是**出处。
3. 手上没有对应原文的条目**直接删掉**，不要凑数、不要凭印象补。
4. 其余部分（结论、数据表、图表）保持不变。"""

_MISSING_CORRECTION = """{tag}：你本轮**取过知识料**（get_instructions / get_context / recall_queries 等），
但最终答复里**没有 `## 业务口径` 块** —— 主 agent 抽不到这一节，用户的报告里就不会有业务口径，
而这一节正是用户用来判断你给的数对不对的依据。

请**重发完整最终答复**（不是只补一节、也不是发一句"已补"——短答复会被结果摘要当成收尾语丢弃，
用户将看到你上一版**没有口径**的答案）。二选一：

A. **用到了口径** → 在答复**末尾**补 `## 业务口径` 块（标题逐字就是这四个字），每条一行三字段
   `口径项 | 内容 | 出处`：
   - `内容` 从本轮取料返回里**直接复制原文**，不要翻译、概括、合并成"人话"，也不要用库对象名代替；
     长条目可用 `…` 省略中段（最多 3 处，省掉的字数不得多于引到的字数）。
   - `出处` **照抄取料返回里出现的文件名**（如 `报工与工时.md`、`通用规则.md`）即可，**不必也
     禁止自己拼目录前缀**（裸文件名系统认；自己拼 `rules/` 反而可能拼错成不存在的路径）。
   - 手上没有原文的那一条**删掉**，但**不要因此整节省略**。
B. **确实一条口径都没用到**（纯明细列举）→ 在末尾单独一行写：`本次未依据知识库口径`。"""


def _content_str(msg) -> str:
    """消息文本（兼容 content 为 str 或 blocks 列表）。"""
    c = getattr(msg, "content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict):
                parts.append(str(b.get("text") or ""))
            else:
                parts.append(str(b or ""))
        return "".join(parts)
    return str(c or "")


def _msgs(state) -> list:
    try:
        if isinstance(state, dict):
            return list(state.get("messages") or [])
        return list(getattr(state, "messages", None) or [])
    except Exception:  # noqa: BLE001
        return []


def _turn_start(msgs: list) -> int:
    """本问题轮次的起点下标＝最后一条**真实**用户消息（跳过 `[系统自动通知]` 注入）。

    口径与 `report_builder._current_turn_start` / `sync_subagent_todos` 一致。
    """
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if isinstance(m, HumanMessage) and not _content_str(m).lstrip().startswith(
            "[系统自动通知]"
        ):
            return i
    return 0


def _retries_in_turn(msgs: list, tag: str = _RETRY_TAG) -> int:
    """本问题轮次内已注入过几次**该类**纠正（`tag` 区分「核验未通过」与「块缺失」）。

    **由 state 推导，不用进程内计数**——进程重启后从 checkpoint 恢复时，进程内的
    set/dict 已清空，会再多打回 2 轮；state 推导天然跨重启（仓内 `query_gate._REMINDED`
    就是踩过这个坑的进程内版本）。
    """
    start = _turn_start(msgs)
    return sum(
        1
        for m in msgs[start:]
        if isinstance(m, HumanMessage) and _content_str(m).startswith(tag)
    )


def _used_evidence_tool(msgs: list) -> bool:
    """**本轮**是否调过知识取料工具。

    只扫本轮（`_turn_start` 之后）：上一轮取过料不能成为本轮要求口径块的依据，否则
    连续追问里的纯明细列举会被反复索要口径块。
    """
    for m in msgs[_turn_start(msgs):]:
        for tc in getattr(m, "tool_calls", None) or []:
            if is_caliber_evidence_tool(tc.get("name") or ""):
                return True
    return False


class CaliberGateMiddleware(AgentMiddleware):
    """终态答复的「业务口径」逐字证据核验：不合规 → `jump_to="model"` 打回重写（≤N 次）。"""

    @hook_config(can_jump_to=["model"])  # 漏了这行 jump_to 会被静默丢弃（见模块 docstring）
    def after_model(self, state, runtime):
        return self._after(state, runtime)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):
        return self._after(state, runtime)

    def _after(self, state, runtime) -> dict | None:
        msgs = _msgs(state)
        if not msgs:
            return None
        last = msgs[-1]
        # 尾部不是 AI（刚注入过纠正）→ 不判，避免自我循环
        if not isinstance(last, AIMessage):
            return None
        # 失败终局（超时 / 额度耗尽 / 未配模型，见 failure_signal）→ 放行，让回合结束。
        # **这不是可选的**：这类消息没有 tool_calls、也不含 `## 业务口径` 块，会直接掉进
        # `_no_block`，而它的两条逃生口（模型已声明未依据知识库口径 / 本轮没取过知识料）
        # 对失败消息都不成立 —— 本轮确实取过知识料，超时文案里也没有那句声明 ⇒ 必然
        # `jump_to="model"` 再烧一次完整的模型调用。2026-09-28 生产实证：一次超时被打回后
        # 又白等 243.5s，用户看到 234s 空窗，整轮 620.8s 且一 SQL 未执行。
        _mark = failed_mark(last)
        if _mark:
            _logger.info(
                "[caliber] 末条为失败终局消息（kind=%s），跳过口径执法，让回合结束",
                _mark.get("kind"),
            )
            return None
        # 非终态：模型还在干活（"边说边查"那轮）。invalid_tool_calls 是坏 JSON，
        # 另有 DanglingToolCallsMiddleware 兜底，这里不掺和。
        if getattr(last, "tool_calls", None) or getattr(last, "invalid_tool_calls", None):
            return None
        text = _content_str(last)
        t = text.strip()
        if not t or t.startswith("[系统") or _CLARIFY_MARK in t:
            return None

        entries = parse_caliber_block(text)
        if not entries:
            return self._no_block(msgs, text)

        db = _current_db_name()
        corpus = load_knowledge_corpus(db)
        if not corpus:
            # fail-open：读不到语料 = 无法核验，绝不因此判模型不合规（也绝不谎称合规，
            # 报告侧会写「未核验」脚注）。
            _logger.info("[caliber] 库 %s 无知识库语料，跳过核验（报告侧标「未核验」）", db or "?")
            return None

        verdicts = verify_caliber_entries(entries, corpus)
        bad = [v for v in verdicts if not v.ok]
        if not bad:
            return None

        n = _retries_in_turn(msgs)
        if n >= _MAX_RETRIES:
            _logger.warning(
                "[caliber] 库 %s 已打回 %d 次仍不合规（%d/%d 条），交由报告侧如实标注未通过",
                db or "?", n, len(bad), len(verdicts),
            )
            return None

        problems = "\n".join(f"- 「{v.title or v.item}」：{v.reason_human}" for v in bad)
        _logger.info(
            "[caliber] 库 %s 口径核验 %d/%d 条通过，注入纠正并打回重写（第 %d 次）",
            db or "?", len(verdicts) - len(bad), len(verdicts), n + 1,
        )
        return {
            "jump_to": "model",
            "messages": [HumanMessage(content=_CORRECTION.format(tag=_RETRY_TAG, problems=problems))],
        }

    def _no_block(self, msgs: list, text: str) -> dict | None:
        """终稿**没有** `## 业务口径` 块时的处置（2026-09-26 生产实证新增）。

        **为什么从「不追」改成「打回 1 次」**：生产 trace `f222a8a549f2d545df7ac10408f9ad85`
        （提示词 v4 + 本中间件已在线上）证明「不追」的真实后果是报告**整节没有业务口径**：
        那一轮子 agent 手里明明有 20.9k 的口径原文（`get_instructions` 返回，含 R1/R6/R7/R8），
        却只在正文里写了「统计口径说明」，主 agent 抽不到块、`build_report` 只能留空 ——
        而这一节正是用户用来判断答案准不准的依据。纯提示词契约至此两次被证伪（先造假出处、
        再整节省略），所以必须程序化兜一次。

        三条出口，缺一条就会变成死循环或误伤：
        - 模型已声明「本次未依据知识库口径」→ 放行（明确声明的省略是契约允许的，也是循环终点）；
        - 本轮没取过知识料 → 无从要求（纯明细列举、直连通道、纯澄清都会走到这里）；
        - 已打回满 `_MAX_MISSING_RETRIES` 次 → 放行（第二次仍缺就认了，报告侧如实缺节）。
        """
        if any(k in text for k in _NO_CALIBER_MARKS):
            return None
        if not _used_evidence_tool(msgs):
            return None
        n = _retries_in_turn(msgs, _MISSING_TAG)
        if n >= _MAX_MISSING_RETRIES:
            _logger.warning(
                "[caliber] 本轮取过知识料但终稿仍无「## 业务口径」块（已打回 %d 次），放行", n,
            )
            return None
        _logger.info(
            "[caliber] 本轮取过知识料但终稿无「## 业务口径」块，注入纠正并打回（第 %d 次）", n + 1,
        )
        return {
            "jump_to": "model",
            "messages": [HumanMessage(content=_MISSING_CORRECTION.format(tag=_MISSING_TAG))],
        }


def _current_db_name() -> str:
    """当前库名（configurable.db_name）；取不到返回空串（调用方据此跳过）。"""
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        return str((cfg.get("configurable") or {}).get("db_name", "") or "")
    except Exception:  # noqa: BLE001
        return ""


__all__ = [
    "CaliberGateMiddleware",
    "_retries_in_turn",
    "_turn_start",
    "_used_evidence_tool",
    "_MAX_RETRIES",
    "_MAX_MISSING_RETRIES",
    "_RETRY_TAG",
    "_MISSING_TAG",
    "_NO_CALIBER_MARKS",
]
