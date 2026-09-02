"""统一事件日志 — 数据模型定义。

参考 DeepSeek Harness 的 Session Event Log 设计，
将所有 agent 交互（LLM 调用、Tool 调用、子 agent 生命周期等）
归一化为 TraceEvent，配以单调递增的 seq 序号。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EventType(str, Enum):
    """事件类型枚举。"""

    # ── 会话生命周期 ──
    SESSION_CREATED = "session/created"

    # ── 用户交互 ──
    USER_MESSAGE = "user/message"
    ASSISTANT_MESSAGE = "assistant/message"
    ASSISTANT_CHUNK = "assistant/chunk"  # 流式 delta（仅首 chunk 记录）

    # ── LLM 调用 ──
    LLM_CALL_START = "llm/call_start"
    LLM_CALL_END = "llm/call_end"  # 含 usage_metadata

    # ── 工具调用 ──
    TOOL_CALL_START = "tool/call_start"
    TOOL_CALL_END = "tool/call_end"  # 含 result 摘要

    # ── 多智能体 ──
    SUBAGENT_SPAWN = "subagent/spawn"  # start_async_task 被调用
    SUBAGENT_PROGRESS = "subagent/progress"  # sync 线程写入进度
    SUBAGENT_COMPLETE = "subagent/complete"  # 子 agent 完成

    # ── 系统事件 ──
    AUTO_CONTINUE = "system/auto_continue"  # 前端自动续跑
    STATE_UPDATE = "system/state_update"  # sync 线程 updateState
    ERROR = "system/error"


@dataclass
class TraceEvent:
    """统一事件记录。

    Attributes:
        seq: 单调递增序号（per thread）。
        thread_id: LangGraph thread_id。
        agent_type: "chat_agent" | "nl2sql_agent"。
        task_id: 子任务 ID（多查询场景，子 agent 的 thread_id 即 task_id）。
        parent_thread_id: 父 agent 的 thread_id（子 agent 用于建立谱系）。
        event_type: 事件类型。
        timestamp: Unix 时间戳（秒）。
        data: 事件载荷（dict，可 JSON 序列化）。
    """

    seq: int = 0
    thread_id: str = ""
    agent_type: str = ""
    task_id: Optional[str] = None
    parent_thread_id: Optional[str] = None
    event_type: EventType = EventType.SESSION_CREATED
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> tuple:
        """转换为 SQLite 插入行 (9 字段)。"""
        import json

        return (
            self.seq,
            self.thread_id,
            self.agent_type,
            self.task_id or "",
            self.parent_thread_id or "",
            self.event_type.value,
            self.timestamp,
            json.dumps(self.data, ensure_ascii=False, default=str),
        )

    @classmethod
    def from_row(cls, row: tuple) -> "TraceEvent":
        """从 SQLite 查询行还原。"""
        import json

        return cls(
            seq=row[0],
            thread_id=row[1],
            agent_type=row[2],
            task_id=row[3] or None,
            parent_thread_id=row[4] or None,
            event_type=EventType(row[5]),
            timestamp=row[6],
            data=json.loads(row[7]) if row[7] else {},
        )