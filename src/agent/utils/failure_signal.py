"""失败终局信号：把「中间件把失败吞成一条友好回答」这件事变成**机器可读**。

## 为什么需要它（2026-09-28 生产事故，trace `3dcc9a66…` / 子 run `01a0e695`）

`ModelTimeoutMiddleware` 的设计初衷是不抛异常、返回一条友好中文文案，让 agent 正常
收尾、界面不卡（见 `model_timeout.py` 模块 docstring）。副作用是：**这条消息与「模型
真的答完了」在数据上完全无法区分**——于是下游三处各自猜，各有各的错法：

1. `CaliberGateMiddleware` 把它当成「终稿缺 `## 业务口径` 块」，注入纠正并
   `jump_to="model"` **打回**，图又发起第二次 240s 调用（用户白等 234s，一 SQL 未执行）；
2. `check_async_task` 只看 `run.status`，于是主 agent 读到 `status: "success"` +
   一段超时文案，只能在自己的推理里写「marked as success, but the result is actually
   a timeout error」——**契约错，不是模型的错**；
3. watcher 的强制 timeout 与 run 自己的 success 各记一套账，同一条任务两套终态。

修法就是本模块：给这类消息盖一个**只有代码能盖**的戳，所有终态消费者改成读戳。

## 契约

- 存放位置：`AIMessage.additional_kwargs[FAILURE_KEY]`。这是「调用方附加」袋子，
  全仓已在当平台语义用（`process_audit` / `wren_call_extract` 读
  `additional_kwargs.tool_calls`，`thread_compact` 用 `lc_source` 打标签），能完整过
  checkpoint 序列化往返、一路到前端。`response_metadata` 是 provider 侧重建的袋子，
  不适合放平台语义。
- 值**只能是 JSON 原生标量**（内部统一 `str()`）：消息要过 checkpoint 与
  `/threads/{tid}/state`，序列化器遇未知类型会抛异常，那比现状更糟。
- **不用文本匹配**：文案是给用户看的可调项；主 agent 向用户解释失败时正文里也会出现
  「模型调用超时」几个字，按文本判会误伤正常回答。
- 消息正文（用户可见文案）**一字符不改** —— 既有断言按内容比较，且文案是用户契约。

## 边界

- 无标记 ⇒ 下游行为与今天**逐字相同**（所有消费点都由负对照锁住）。
- 标记随上下文压缩（`thread_compact` 摘要）消失是可接受的：所有消费点都在 run 收尾
  时就近读（终态那一刻），没有跨压缩消费者，因此不新增 thread-level state key。

判定链见 `scripts/verify_failure_signal.py`。
"""
from __future__ import annotations

from typing import Any

# 附加袋里的键名。加 `nl2sql_` 前缀，避开 provider 自己可能使用的名字
# （`reasoning_content` / `tool_calls` 就是 provider 与 SDK 都会写的）。
FAILURE_KEY = "nl2sql_failure"

KIND_MODEL_TIMEOUT = "model_timeout"
KIND_QUOTA_EXHAUSTED = "quota_exhausted"
KIND_MODEL_REQUIRED = "model_required"

# kind → 下游终态串。**只许产出这两个值**：它们必须落在
# `sync_subagent_todos._RUN_DONE_STATUSES` 与前端卡片「已结束」状态集合里，
# 否则卡片会永远停在「执行中」。
_FAILED_RUN_STATUS = {KIND_MODEL_TIMEOUT: "timeout"}
_DEFAULT_RUN_STATUS = "error"

# detail 进 checkpoint / 前端 state / Langfuse，截断避免随消息体积膨胀
_DETAIL_MAX = 500


def _additional_kwargs(msg: Any) -> dict:
    """取消息的附加袋（兼容 BaseMessage 对象与 dict 两种形态）。"""
    if isinstance(msg, dict):
        kwargs = msg.get("additional_kwargs")
    else:
        kwargs = getattr(msg, "additional_kwargs", None)
    return kwargs if isinstance(kwargs, dict) else {}


def mark_failed(msg: Any, kind: str, detail: str = "", *, at: str = "") -> Any:
    """给一条消息盖失败终局戳，返回**同一对象**（便于在构造处链式调用）。

    `detail` 是给下游当「失败原因」用的（`async_tasks[task].error`、run-status 的
    `last_error`）—— 调用方传用户可见文案即可，不必另造一套。
    """
    mark: dict[str, Any] = {"kind": str(kind or "")}
    text = str(detail or "")[:_DETAIL_MAX]
    if text:
        mark["detail"] = text
    if at:
        mark["at"] = str(at)
    kwargs = dict(_additional_kwargs(msg))
    kwargs[FAILURE_KEY] = mark
    if isinstance(msg, dict):
        msg["additional_kwargs"] = kwargs
    else:
        msg.additional_kwargs = kwargs
    return msg


def failed_mark(msg: Any) -> dict | None:
    """取消息上的失败标记；没有（或结构不对）返回 None。"""
    mark = _additional_kwargs(msg).get(FAILURE_KEY)
    if isinstance(mark, dict) and mark.get("kind"):
        return mark
    return None


def is_failed(msg: Any) -> bool:
    return failed_mark(msg) is not None


def _type_of(msg: Any) -> str:
    """消息角色（BaseMessage 的 `.type` / dict 的 `type` 或 `role`）。"""
    if isinstance(msg, dict):
        return str(msg.get("type") or msg.get("role") or "")
    return str(getattr(msg, "type", "") or "")


def last_failed_mark(messages: Any) -> dict | None:
    """**最后一条 AI 消息**的失败标记；该条没有标记就返回 None。

    只看最后一条 AI（不往回扫）：本轮的失败终局必然是最后一条 AI——「被打回后第二次
    调用成功」的场景里末条是模型真正写完的回答，那一轮就该算成功。往回扫会把历史轮次
    的标记翻出来，制造假失败。
    """
    for msg in reversed(list(messages or [])):
        if _type_of(msg) != "ai":
            continue
        return failed_mark(msg)
    return None


def run_status_for(kind: str) -> str:
    """失败 kind → 下游终态串（`timeout` / `error`）。"""
    return _FAILED_RUN_STATUS.get(str(kind or ""), _DEFAULT_RUN_STATUS)


def detail_of(mark: Any) -> str:
    """标记里的可读原因（没有返回空串）。"""
    if not isinstance(mark, dict):
        return ""
    return str(mark.get("detail") or "")
