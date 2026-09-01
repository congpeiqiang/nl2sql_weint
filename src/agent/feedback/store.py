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
    feedback_type TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_thread ON feedback(thread_id);
CREATE TABLE IF NOT EXISTS feedback_annotation (
    thread_id   TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    feedback_type TEXT NOT NULL DEFAULT '',
    question    TEXT NOT NULL DEFAULT '',
    bad_sql     TEXT NOT NULL DEFAULT '',
    exec_error  TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    rating      TEXT NOT NULL DEFAULT '',
    db_name     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'queued',
    is_valid    INTEGER,
    gold_sql    TEXT NOT NULL DEFAULT '',
    gold_result TEXT NOT NULL DEFAULT '',
    bad_type    TEXT NOT NULL DEFAULT '',
    annotator   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    annotated_at TEXT NOT NULL DEFAULT '',
    badcase_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_fa_status ON feedback_annotation(status);
"""

# 标注状态机（见 docs/langfuse平台/NL2SQL反馈闭环优化设计方案.md §4.1）：
#   queued（待判断）→ annotating（有效，编辑中）→ validated（金标就绪）
#                                          ↘ rejected（无效/误报/闲聊）
#   validated → badcase（确认入 BadCase，终态）
#   validated → good（确认入 Good Set 正向样本，终态）
ANNOTATION_STATUSES = ("queued", "annotating", "validated", "rejected", "badcase", "good")
# 非终态：撤销反馈时回滚为 rejected
ANNOTATION_NON_TERMINAL = ("queued", "annotating", "validated")
# 终态：不可再 judge/confirm/execute（只能人工改库恢复）
_TERMINAL = ("badcase", "good", "rejected")


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
    feedback_type: str = ""

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
            feedback_type=str(data.get("feedback_type", "") or ""),
        )


@dataclass
class AnnotationRecord:
    """待标注队列一条（用户反馈 → 人工复审 → 金标 SQL → BadCase）。"""

    thread_id: str
    message_id: str
    feedback_type: str = ""
    question: str = ""
    bad_sql: str = ""
    exec_error: str = ""
    note: str = ""
    rating: str = ""
    db_name: str = ""
    status: str = "queued"
    is_valid: int | None = None
    gold_sql: str = ""
    gold_result: str = ""
    bad_type: str = ""
    annotator: str = ""
    created_at: str = ""
    annotated_at: str = ""
    badcase_at: str = ""

    def to_mapping(self) -> dict:
        return asdict(self)


class FeedbackStore:
    """(thread_id, message_id) → FeedbackRecord 的 SQLite 存储。"""

    def __init__(self, path: Optional[str] = None):
        if path:
            self._path = Path(path)
        elif _DEFAULT_PATH:
            self._path = Path(_DEFAULT_PATH)
        else:
            from agent.workspace_manager import get_workspace_manager
            self._path = get_workspace_manager().shared_feedback_dir / "message_feedback.db"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._migrate_schema()
        self._migrate_from_json()

    def _migrate_schema(self) -> None:
        """存量库增量迁移（幂等）：feedback 表补 feedback_type；标注表补 db_name。"""
        with _LOCK:
            try:
                cols = {
                    r["name"]
                    for r in self._conn.execute("PRAGMA table_info(feedback)").fetchall()
                }
                if "feedback_type" not in cols:
                    self._conn.execute(
                        "ALTER TABLE feedback ADD COLUMN feedback_type TEXT NOT NULL DEFAULT ''"
                    )
                    self._conn.commit()
                    _logger.info("[feedback] 迁移：feedback 表新增 feedback_type 列")
            except Exception as e:  # noqa: BLE001
                _logger.warning("[feedback] feedback_type 列迁移失败（按已有结构继续）: %s", e)
            try:
                cols = {
                    r["name"]
                    for r in self._conn.execute("PRAGMA table_info(feedback_annotation)").fetchall()
                }
                if cols and "db_name" not in cols:
                    self._conn.execute(
                        "ALTER TABLE feedback_annotation ADD COLUMN db_name TEXT NOT NULL DEFAULT ''"
                    )
                    self._conn.commit()
                    _logger.info("[feedback] 迁移：feedback_annotation 表新增 db_name 列")
            except Exception as e:  # noqa: BLE001
                _logger.warning("[feedback] 标注表 db_name 列迁移失败: %s", e)

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
            feedback_type=row["feedback_type"] if "feedback_type" in row.keys() else "",
        )

    def _insert(self, rec: FeedbackRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO feedback
                (thread_id, message_id, rating, note, version, created_at, updated_at,
                 context_json, question, sql, feedback_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.thread_id, rec.message_id, rec.rating, rec.note, rec.version,
                rec.created_at, rec.updated_at,
                json.dumps(rec.context, ensure_ascii=False), rec.question, rec.sql,
                rec.feedback_type or "",
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

    # ── 反馈类型（优化①：查询 vs 闲聊）────────────────────
    def set_feedback_type(self, thread_id: str, message_id: str, ftype: str) -> bool:
        """回填反馈类型（query/chat）。不 bump version（仿 update_snapshot）。"""
        if ftype not in ("query", "chat"):
            return False
        with _LOCK:
            cur = self._conn.execute(
                "UPDATE feedback SET feedback_type=? WHERE thread_id=? AND message_id=?",
                (ftype, thread_id, message_id),
            )
            if cur.rowcount:
                self._conn.commit()
                return True
        return False

    def session_feedback(self, thread_id: str) -> list[FeedbackRecord]:
        """某会话（LangGraph thread）下全部反馈（collect_badcase 类型判定用）。"""
        return self.list_thread(thread_id)

    # ── 待标注队列（优化③）────────────────────────────────
    def enqueue_annotation(
        self,
        thread_id: str,
        message_id: str,
        rating: str = "",
        note: str = "",
        question: str = "",
        sql: str = "",
        feedback_type: str = "",
        db_name: str = "",
    ) -> AnnotationRecord:
        """把一条反馈放入待标注队列（幂等：已存在不重复入队）。

        入队后若 question/sql 为空，标注详情端可惰性补齐（见
        api/feedback_annotation.py get 端点）。返回当前记录。
        """
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM feedback_annotation WHERE thread_id=? AND message_id=?",
                (thread_id, message_id),
            ).fetchone()
            if row is not None:
                return self._row_to_annotation(row)
            now = _now_iso()
            rec = AnnotationRecord(
                thread_id=thread_id,
                message_id=message_id,
                feedback_type=feedback_type or "",
                question=(question or "")[:2000],
                bad_sql=(sql or "")[:8000],
                note=(note or "")[:2000],
                rating=rating,
                db_name=(db_name or "")[:128],
                status="queued",
                created_at=now,
            )
            self._conn.execute(
                """
                INSERT INTO feedback_annotation
                    (thread_id, message_id, feedback_type, question, bad_sql, note,
                     rating, db_name, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    rec.thread_id, rec.message_id, rec.feedback_type,
                    rec.question, rec.bad_sql, rec.note, rec.rating, rec.db_name, now,
                ),
            )
            self._conn.commit()
            return rec

    def _row_to_annotation(self, row: sqlite3.Row) -> AnnotationRecord:
        return AnnotationRecord(
            thread_id=row["thread_id"],
            message_id=row["message_id"],
            feedback_type=row["feedback_type"] or "",
            question=row["question"] or "",
            bad_sql=row["bad_sql"] or "",
            exec_error=row["exec_error"] or "",
            note=row["note"] or "",
            rating=row["rating"] or "",
            db_name=row["db_name"] if "db_name" in row.keys() else "",
            status=row["status"] or "queued",
            is_valid=row["is_valid"],
            gold_sql=row["gold_sql"] or "",
            gold_result=row["gold_result"] or "",
            bad_type=row["bad_type"] or "",
            annotator=row["annotator"] or "",
            created_at=row["created_at"] or "",
            annotated_at=row["annotated_at"] or "",
            badcase_at=row["badcase_at"] or "",
        )

    def get_annotation(self, thread_id: str, message_id: str) -> AnnotationRecord | None:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM feedback_annotation WHERE thread_id=? AND message_id=?",
                (thread_id, message_id),
            ).fetchone()
            return self._row_to_annotation(row) if row is not None else None

    def list_annotations(self, status: str | None = None, limit: int = 50) -> list[AnnotationRecord]:
        """队列列表。status 指定时按状态过滤，默认全状态按 created_at 倒序。"""
        sql = "SELECT * FROM feedback_annotation"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        with _LOCK:
            rows = self._conn.execute(sql, params + (int(limit),)).fetchall()
            return [self._row_to_annotation(r) for r in rows]

    def update_annotation(
        self,
        thread_id: str,
        message_id: str,
        **fields: object,
    ) -> AnnotationRecord | None:
        """按需更新标注记录（白名单字段；校验状态机，非法状态转换返回 None）。"""
        allowed = {
            "feedback_type", "question", "bad_sql", "exec_error", "note", "rating",
            "db_name", "status", "is_valid", "gold_sql", "gold_result", "bad_type",
            "annotator", "annotated_at", "badcase_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_annotation(thread_id, message_id)
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM feedback_annotation WHERE thread_id=? AND message_id=?",
                (thread_id, message_id),
            ).fetchone()
            if row is None:
                return None
            cur = self._row_to_annotation(row)
            # 状态机校验：目标状态在合法迁移内
            new_status = updates.get("status")
            if new_status is not None and new_status not in ANNOTATION_STATUSES:
                return None
            if new_status is not None and cur.status in _TERMINAL and new_status != cur.status:
                return None  # 终态不可回退
            for k, v in updates.items():
                setattr(cur, k, v)
            cols = ", ".join(f"{k}=?" for k in updates)
            vals = list(updates.values()) + [thread_id, message_id]
            self._conn.execute(
                f"UPDATE feedback_annotation SET {cols} WHERE thread_id=? AND message_id=?",
                tuple(vals),
            )
            self._conn.commit()
            return self._row_to_annotation(
                self._conn.execute(
                    "SELECT * FROM feedback_annotation WHERE thread_id=? AND message_id=?",
                    (thread_id, message_id),
                ).fetchone()
            )

    def revoke_annotations_for_message(self, thread_id: str, message_id: str) -> int:
        """反馈被撤销时，把该消息非终态标注回滚为 rejected（不打扰已终态工作）。"""
        with _LOCK:
            cur = self._conn.execute(
                "UPDATE feedback_annotation SET status='rejected', annotated_at=?, note="
                "CASE WHEN note='' THEN '反馈已撤销' ELSE note END "
                "WHERE thread_id=? AND message_id=? AND status IN (?, ?, ?)",
                (_now_iso(), thread_id, message_id, *ANNOTATION_NON_TERMINAL),
            )
            if cur.rowcount:
                self._conn.commit()
            return cur.rowcount or 0

    # ── 看板聚合（优化②）──────────────────────────────────
    def records_in_window(self, since_iso: str) -> list[FeedbackRecord]:
        """近 N 天反馈（updated_at ≥ since，ISO UTC 字符串比较）。"""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM feedback WHERE updated_at >= ? ORDER BY updated_at",
                (since_iso,),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]


_store: Optional[FeedbackStore] = None
_store_path: Optional[str] = None


def _resolve_legacy_json_path() -> Optional[str]:
    """解析旧 JSON 迁移文件路径。"""
    if _LEGACY_JSON_PATH:
        return _LEGACY_JSON_PATH
    try:
        from agent.workspace_manager import get_workspace_manager
        return str(get_workspace_manager().shared_feedback_dir / "message_feedback.json")
    except Exception:
        return None


def get_store() -> FeedbackStore:
    global _store, _store_path
    try:
        from agent.workspace_manager import get_workspace_manager
        current_path = str(get_workspace_manager().shared_feedback_dir / "message_feedback.db")
    except Exception:
        current_path = str(_DEFAULT_PATH or "")
    with _LOCK:
        if _store is None or _store_path != current_path:
            _store = FeedbackStore(path=current_path if current_path else None)
            _store_path = current_path
        return _store
