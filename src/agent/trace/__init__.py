"""统一轨迹追踪包。

提供：
- EventType / TraceEvent：事件数据模型
- EventStore：SQLite 持久化存储
- SessionLineageEngine：多智能体会话谱系追踪
"""
from .event_log import EventType, TraceEvent
from .event_store import EventStore
from .session_lineage import SessionLineageEngine

__all__ = [
    "EventType",
    "TraceEvent",
    "EventStore",
    "SessionLineageEngine",
]