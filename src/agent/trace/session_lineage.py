"""SessionLineageEngine — 多智能体会话谱系追踪。

追踪 chat_agent 与 nl2sql_agent 之间的父子关系，
构建完整的会话树结构。
"""
from __future__ import annotations

from typing import Optional

from .event_store import EventStore


class SessionLineageEngine:
    """基于 EventStore 的会话谱系查询引擎。

    示例：
    chat_agent (root)
      ├─ nl2sql_agent (task_1: "查询销售额")
      ├─ nl2sql_agent (task_2: "查询用户数")
      └─ nl2sql_agent (task_3: "查询订单量")
    """

    def __init__(self, store: EventStore):
        self._store = store

    def trace_session(self, thread_id: str) -> dict:
        """返回以 thread_id 为根的完整谱系树。"""
        return self._store.get_full_lineage_tree(thread_id)

    def get_ancestors(self, thread_id: str) -> list[dict]:
        """获取祖先链（从根到当前父节点）。"""
        return self._store.get_ancestor_chain(thread_id)

    def get_descendants(self, thread_id: str) -> list[dict]:
        """获取所有直接后代（子任务）。"""
        return self._store.get_descendants(thread_id)

    def get_root(self, thread_id: str) -> Optional[str]:
        """追溯到根 thread_id。"""
        chain = self._store.get_ancestor_chain(thread_id)
        return chain[0]["thread_id"] if chain else thread_id

    def get_concurrent_tasks(self, parent_thread_id: str) -> list[dict]:
        """获取某次会话中所有并发子任务。"""
        return self._store.get_descendants(parent_thread_id)

    def register_session(
        self,
        thread_id: str,
        parent_thread_id: str = "",
        agent_type: str = "chat_agent",
        metadata: Optional[dict] = None,
    ) -> None:
        """注册一个新会话（建立谱系记录）。"""
        self._store.upsert_lineage(
            thread_id=thread_id,
            parent_thread_id=parent_thread_id,
            agent_type=agent_type,
            status="active",
            metadata=metadata,
        )

    def mark_completed(self, thread_id: str) -> None:
        """标记会话完成。"""
        self._store.update_lineage_status(thread_id, "completed")

    def mark_error(self, thread_id: str) -> None:
        """标记会话错误。"""
        self._store.update_lineage_status(thread_id, "error")