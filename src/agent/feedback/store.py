"""消息反馈存储 — SQLite（feedback.db），联合主键 (thread_id, message_id)。

对标 deepseek-harness `dsh-message-feedback`：逐条 assistant 消息 positive/negative
+ 可选 note，带 `version` 做乐观并发（CAS）。反馈是 UI 层元数据，不进 LangGraph
state、不打扰 LLM 上下文；后续通过导出端点回流 NL2SQL 评测集。

存储从单 JSON 文件（全量加载 + 每次全量重写）改为 SQLite，避免反馈量增长后单文件
膨胀；并新增 `question`（用户提问）与 `sql`（生成的 SQL）快照字段，用于
「问题 → SQL → 反馈」归因评测（此前只存 `context.db_name`，缺这两项）。

设计：
- SQLite 单文件（默认 `src/agent/workspace/feedback/message_feedback.db`，已 gitignore）。
- 表 feedback(thread_id, message_id, rating, note, version, created_at, updated_at,
  context_json, question, sql)，PRIMARY KEY(thread_id, message_id)。
- 进程内锁 `threading.RLock` 串行化读写；单连接 check_same_thread=False + WAL。
- 首次启动时若旧 message_feedback.json 存在且 SQLite 无数据，做一次性迁移。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# 默认存储位置：优先 .env MESSAGE_FEEDBACK_PATH，否则由 WorkspaceManager 动态解析
_DEFAULT_PATH = os.getenv("MESSAGE_FEEDBACK_PATH", "") or None
# 旧 JSON 文件（用于一次性迁移；迁移后不再使用）
_LEGACY_JSON_PATH = os.getenv("MESSAGE_FEEDBACK_JSON_PATH", "") or None

VALID_RATINGS = ("positive", "negative")
MAX_NOTE_BYTES = 2048

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    thread_id   TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    rating      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    question    TEXT NOT NULL DEFAULT '',
    sql         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_thread ON feedback(thread_id);
"""


class VersionConflictError(Exception):
    """CAS 冲突：请求携带的 if_version 与存储 version 不一致。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class FeedbackRecord:
    """一条消息反馈。"""

    thread_id: str
    message_id: str
    rating: str
    note: str = ""
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    context: dict = field(default_factory=dict)
    question: str = ""
    sql: str = ""

    @staticmethod
    def _key(thread_id: str, message_id: str) -> str:
        return f"{thread_id}::{message_id}"

    def to_mapping(self) -> dict:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: dict) -> "FeedbackRecord":
        return cls(
            thread_id=str(data.get("thread_id", "")),
            message_id=str(data.get("message_id", "")),
            rating=str(data.get("rating", "")),
            note=str(data.get("note", "")),
            version=int(data.get("version", 1)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            context=dict(data.get("context", {}) or {}),
            question=str(data.get("question", "")),
            sql=str(data.get("sql", "")),
        )


class FeedbackStore:
    """(thread_id, message_id) → FeedbackRecord 的 SQLite 存储。"""

    def __init__(self, path: Optional[str] = None):
        if path:
            self._path = Path(path)
        elif _DEFAULT_PATH:
            self._path = Path(_DEFAULT_PATH)
        else:
            from agent.workspace_manager import get_workspace_manager
            self._path = get_workspace_manager().feedback_dir / "message_feedback.db"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._migrate_from_json()

    # ── 迁移 ──────────────────────────────────────────────
    def _migrate_from_json(self) -> None:
        """旧 message_feedback.json → SQLite 一次性迁移（SQLite 空且有旧文件时）。"""
        legacy_path = _resolve_legacy_json_path()
        if not legacy_path:
            return
        legacy = Path(legacy_path)
        if not legacy.is_file():
            return
        try:
            with _LOCK:
                count = self._conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
                if count > 0:
                    return
                raw = json.loads(legacy.read_text(encoding="utf-8"))
                items = raw.get("feedback", {}) if isinstance(raw, dict) else {}
                for k, v in items.items():
                    if not isinstance(v, dict):
                        continue
                    rec = FeedbackRecord.from_mapping(v)
                    self._insert(rec)
                self._conn.commit()
                _logger.info("[feedback] 已从 JSON 迁移 %d 条反馈到 SQLite", len(items))
                # 迁移成功后将旧文件改名留档，避免重复迁移
                legacy.replace(legacy.with_suffix(".json.migrated"))
        except Exception as e:  # noqa: BLE001
            _logger.warning("[feedback] JSON→SQLite 迁移失败（按空库继续）: %s", e)

    # ── 底层行操作（调用方须持有 _LOCK）──
    def _row_to_record(self, row: sqlite3.Row) -> FeedbackRecord:
        context = {}
        try:
            context = json.loads(row["context_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            context = {}
        return FeedbackRecord(
            thread_id=row["thread_id"],
            message_id=row["message_id"],
            rating=row["rating"],
            note=row["note"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            context=context,
            question=row["question"],
            sql=row["sql"],
        )

    def _insert(self, rec: FeedbackRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO feedback
                (thread_id, message_id, rating, note, version, created_at, updated_at,
                 context_json, question, sql)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.thread_id, rec.message_id, rec.rating, rec.note, rec.version,
                rec.created_at, rec.updated_at,
                json.dumps(rec.context, ensure_ascii=False), rec.question, rec.sql,
            ),
        )

    def _update(self, rec: FeedbackRecord) -> None:
        self._conn.execute(
            """
            UPDATE feedback SET rating=?, note=?, version=?, updated_at=?,
                context_json=?, question=?, sql=?
            WHERE thread_id=? AND message_id=?
            """,
            (
                rec.rating, rec.note, rec.version, rec.updated_at,
                json.dumps(rec.context, ensure_ascii=False), rec.question, rec.sql,
                rec.thread_id, rec.message_id,
            ),
        )

    # ── CRUD ──────────────────────────────────────────────
    def upsert(
        self,
        thread_id: str,
        message_id: str,
        rating: str,
        note: str = "",
        context: Optional[dict] = None,
        question: str = "",
        sql: str = "",
        if_version: Optional[int] = None,
    ) -> FeedbackRecord:
        """新建或更新反馈。if_version 提供时做 CAS，不匹配抛 VersionConflictError。"""
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM feedback WHERE thread_id=? AND message_id=?",
                (thread_id, message_id),
            ).fetchone()
            existing = self._row_to_record(row) if row is not None else None
            if existing is not None and if_version is not None and existing.version != if_version:
                raise VersionConflictError(
                    f"version 冲突：存储 {existing.version}，请求 {if_version}"
                )
            now = _now_iso()
            if existing is None:
                rec = FeedbackRecord(
                    thread_id=thread_id,
                    message_id=message_id,
                    rating=rating,
                    note=note,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    context=dict(context or {}),
                    question=question,
                    sql=sql,
                )
                self._insert(rec)
            else:
                existing.rating = rating
                existing.note = note
                existing.version += 1
                existing.updated_at = now
                # context/question/sql 只在首次写入快照，更新不覆盖（评测归因以首次为准）
                rec = existing
                self._update(rec)
            self._conn.commit()
            return rec

    def update_snapshot(self, thread_id: str, message_id: str, question: str, sql: str) -> bool:
        """后台补齐 question/sql 快照。

        - 不 bump version：避免与前端已缓存的 version 产生 CAS 冲突。
        - 记录已被删除（撤销反馈）时 no-op，返回 False。
        """
        with _LOCK:
            row = self._conn.execute(
                "SELECT 1 FROM feedback WHERE thread_id=? AND message_id=?",
                (thread_id, message_id),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE feedback SET question=?, sql=? WHERE thread_id=? AND message_id=?",
                (question, sql, thread_id, message_id),
            )
            self._conn.commit()
            return True

    def delete(
        self, thread_id: str, message_id: str, if_version: Optional[int] = None
    ) -> bool:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM feedback WHERE thread_id=? AND message_id=?",
                (thread_id, message_id),
            ).fetchone()
            if row is None:
                return False
            existing = self._row_to_record(row)
            if if_version is not None and existing.version != if_version:
                raise VersionConflictError(
                    f"version 冲突：存储 {existing.version}，请求 {if_version}"
                )
            self._conn.execute(
                "DELETE FROM feedback WHERE thread_id=? AND message_id=?",
                (thread_id, message_id),
            )
            self._conn.commit()
            return True

    def get(self, thread_id: str, message_id: str) -> Optional[FeedbackRecord]:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM feedback WHERE thread_id=? AND message_id=?",
                (thread_id, message_id),
            ).fetchone()
            return self._row_to_record(row) if row is not None else None

    def list_thread(self, thread_id: str) -> list[FeedbackRecord]:
        """某会话下全部反馈（前端打开会话时回显图标态）。"""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM feedback WHERE thread_id=? ORDER BY updated_at",
                (thread_id,),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def export_all(self) -> list[FeedbackRecord]:
        """导出全部反馈（bad case 评测集回流用）。"""
        with _LOCK:
            rows = self._conn.execute("SELECT * FROM feedback ORDER BY updated_at").fetchall()
            return [self._row_to_record(r) for r in rows]


_store: Optional[FeedbackStore] = None
_store_path: Optional[str] = None


def _resolve_legacy_json_path() -> Optional[str]:
    """解析旧 JSON 迁移文件路径。"""
    if _LEGACY_JSON_PATH:
        return _LEGACY_JSON_PATH
    try:
        from agent.workspace_manager import get_workspace_manager
        return str(get_workspace_manager().feedback_dir / "message_feedback.json")
    except Exception:
        return None


def get_store() -> FeedbackStore:
    global _store, _store_path
    try:
        from agent.workspace_manager import get_workspace_manager
        current_path = str(get_workspace_manager().feedback_dir / "message_feedback.db")
    except Exception:
        current_path = str(_DEFAULT_PATH or "")
    with _LOCK:
        if _store is None or _store_path != current_path:
            _store = FeedbackStore(path=current_path if current_path else None)
            _store_path = current_path
        return _store
