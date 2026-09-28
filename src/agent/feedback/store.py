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
- 进程内锁串行化读写（P2-3 起是带计量的 `metered_rlock`，见 agent/utils/prom_metrics.py）；
  单连接 check_same_thread=False + WAL。
- 首次启动时若旧 message_feedback.json 存在且 SQLite 无数据，做一次性迁移。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)

from agent.utils.prom_metrics import metered_rlock

# P2-3：带计量的可重入锁（store 标签进 nl2sql_sqlite_lock_wait_seconds）
_LOCK = metered_rlock("feedback")

# 默认存储位置：优先 .env MESSAGE_FEEDBACK_PATH，否则由 WorkspaceManager 动态解析
_DEFAULT_PATH = os.getenv("MESSAGE_FEEDBACK_PATH", "") or None
# 旧 JSON 文件（用于一次性迁移；迁移后不再使用）
_LEGACY_JSON_PATH = os.getenv("MESSAGE_FEEDBACK_JSON_PATH", "") or None

VALID_RATINGS = ("positive", "negative")
MAX_NOTE_BYTES = 2048
# SQL 快照（feedback.sql / annotation.bad_sql）的截断线。快照既可能是模型写的
# 语义层 SQL（几百字符），也可能是 Cube 通道复算出的**物理 SQL**（展开 MDL 视图
# 后 3~11KB，见 agent/utils/wren_call_extract）——后者是本上限的由来：超过它就只能
# 存一条跑不了的半截 SQL，不如退回语义层 SQL。调用方截断时统一引用本常量。
#
# 2026-09-19：由 8000 提到 64000。8000 这个数压不住实测上限（复杂口径的物理 SQL
# 见过 10.9KB），一超线就退回语义层 SQL —— 而语义层 SQL 在物理库跑不了：标注页
# 「执行校验」会失败，入集的 physical_sql_original 也不再是物理 SQL（键名撒谎）。
# 64000 相对实测最大值留约 6 倍余量，正常口径碰不到这条线；真碰到的行为与从前
# 一致（退语义层 SQL），只是那条路现在几乎不可达。
MAX_SNAPSHOT_SQL = 64000

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
    cube_spec   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_thread ON feedback(thread_id);
CREATE TABLE IF NOT EXISTS feedback_annotation (
    thread_id   TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    feedback_type TEXT NOT NULL DEFAULT '',
    question    TEXT NOT NULL DEFAULT '',
    bad_sql     TEXT NOT NULL DEFAULT '',
    cube_spec   TEXT NOT NULL DEFAULT '',
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
    auto_good   INTEGER NOT NULL DEFAULT 0,
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

# 可以被「硬删除」的四个状态（标注页左栏的四个本地 Tab）。
# rejected 虽是终态但没有外部产物，可删；good / badcase 各有对应物（Langfuse
# Dataset 条目、badcase_status.json 条目），删本地行会把它们变成孤儿——它们各有
# 自己的撤回路径（revoke-good、Langfuse UI），故排除在外。
# 必须是**正白名单**：list_annotations(status=None) 意味着「全部状态」，用黑名单
# 配 `status or None` 会让一次「清空本 Tab」连 good/badcase 一起删掉。
ANNOTATION_DELETABLE = ("queued", "annotating", "validated", "rejected")


class VersionConflictError(Exception):
    """CAS 冲突：请求携带的 if_version 与存储 version 不一致。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads_spec(v) -> dict:
    """cube_spec 列（TEXT/JSON）→ dict；任何脏值退回空 dict（绝不抛）。

    与 ``context``/``context_json`` 同一套做法：dataclass 里是 dict（API 直接可序列化），
    库里是 JSON 串。老库该列不存在时 row 取值会是 None → 空 dict。
    """
    if isinstance(v, dict):
        return v
    if not v:
        return {}
    try:
        got = json.loads(v)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


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
    # Cube 通道的规范化查询定义（measures/dimensions/filters…）；非 Cube 通道为空 dict。
    # 与 sql 快照同一次复算产出，供入 BadCase/Good Set 时带上聚合口径做监控与评测比对。
    cube_spec: dict = field(default_factory=dict)

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
            cube_spec=_loads_spec(data.get("cube_spec")),
        )


@dataclass
class AnnotationRecord:
    """待标注队列一条（用户反馈 → 人工复审 → 金标 SQL → BadCase）。"""

    thread_id: str
    message_id: str
    feedback_type: str = ""
    question: str = ""
    bad_sql: str = ""
    # Cube 通道的规范化查询定义（与 bad_sql 同一次复算产出）；非 Cube 通道为空 dict
    cube_spec: dict = field(default_factory=dict)
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
    # 1 = 本条 Good Set 由「点赞自动入集」写入（无人点击），0 = 人工确认或未入集。
    # 用途：① 撤回端点区分「机器写的」与「人写的」；② 用户撤销点赞时只自动收回
    # 自动写入的那条（人的判断依据不止那个 👍）；③ 前端标注「自动入集」。
    auto_good: int = 0

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
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            self._conn.execute("PRAGMA journal_mode=WAL")
            # 跨进程写等待：API 进程与 CLI/标注脚本可能同时访问同一份反馈库。
            self._conn.execute("PRAGMA busy_timeout=15000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._migrate_schema()
        self._migrate_from_json()

    def _migrate_schema(self) -> None:
        """存量库增量迁移（幂等）：feedback 补 feedback_type/cube_spec；标注表补 db_name/cube_spec。

        每列一个独立 try：某列迁移失败不该挡住其余列（旧库结构千奇百怪，按已有结构继续）。
        """
        pending = (
            ("feedback", "feedback_type", "TEXT NOT NULL DEFAULT ''"),
            # cube_spec：Cube 通道的规范化查询定义（measures/dimensions… JSON 串）。
            # 与 sql 快照**同一时刻**由同一次复算产出（见 message_feedback 的
            # _extract_sql_with_cube / feedback_annotation 的 _backfill_annotation）——
            # 落库是为了入 BadCase/Good Set 时能带上「聚合口径」，供监控与评测比对
            # （只存物理 SQL 的话，口径差异从 SQL 里读起来很费劲）。
            ("feedback", "cube_spec", "TEXT NOT NULL DEFAULT ''"),
            ("feedback_annotation", "db_name", "TEXT NOT NULL DEFAULT ''"),
            ("feedback_annotation", "cube_spec", "TEXT NOT NULL DEFAULT ''"),
            # auto_good：本条 Good Set 是不是「点赞自动入集」写的（见 AnnotationRecord）。
            ("feedback_annotation", "auto_good", "INTEGER NOT NULL DEFAULT 0"),
        )
        with _LOCK:
            for table, col, decl in pending:
                try:
                    cols = {
                        r["name"]
                        for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                    }
                    if cols and col not in cols:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                        self._conn.commit()
                        _logger.info("[feedback] 迁移：%s 表新增 %s 列", table, col)
                except Exception as e:  # noqa: BLE001
                    _logger.warning("[feedback] %s.%s 列迁移失败（按已有结构继续）: %s",
                                    table, col, e)

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
            cube_spec=_loads_spec(row["cube_spec"] if "cube_spec" in row.keys() else None),
        )

    def _insert(self, rec: FeedbackRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO feedback
                (thread_id, message_id, rating, note, version, created_at, updated_at,
                 context_json, question, sql, feedback_type, cube_spec)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.thread_id, rec.message_id, rec.rating, rec.note, rec.version,
                rec.created_at, rec.updated_at,
                json.dumps(rec.context, ensure_ascii=False), rec.question, rec.sql,
                rec.feedback_type or "",
                json.dumps(rec.cube_spec or {}, ensure_ascii=False),
            ),
        )

    def _update(self, rec: FeedbackRecord) -> None:
        self._conn.execute(
            """
            UPDATE feedback SET rating=?, note=?, version=?, updated_at=?,
                context_json=?, question=?, sql=?, cube_spec=?
            WHERE thread_id=? AND message_id=?
            """,
            (
                rec.rating, rec.note, rec.version, rec.updated_at,
                json.dumps(rec.context, ensure_ascii=False), rec.question, rec.sql,
                json.dumps(rec.cube_spec or {}, ensure_ascii=False),
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

    def update_snapshot(
        self,
        thread_id: str,
        message_id: str,
        question: str,
        sql: str,
        cube_spec: Optional[dict] = None,
    ) -> bool:
        """后台补齐 question/sql/cube_spec 快照。

        - 不 bump version：避免与前端已缓存的 version 产生 CAS 冲突。
        - 记录已被删除（撤销反馈）时 no-op，返回 False。
        - ``cube_spec=None`` 表示「本次没算」→ 不覆盖已有值；传 ``{}`` 表示「算过，
          确认非 Cube 通道」→ 写空。两者语义不同，别混。
        """
        with _LOCK:
            row = self._conn.execute(
                "SELECT 1 FROM feedback WHERE thread_id=? AND message_id=?",
                (thread_id, message_id),
            ).fetchone()
            if row is None:
                return False
            if cube_spec is None:
                self._conn.execute(
                    "UPDATE feedback SET question=?, sql=? WHERE thread_id=? AND message_id=?",
                    (question, sql, thread_id, message_id),
                )
            else:
                self._conn.execute(
                    "UPDATE feedback SET question=?, sql=?, cube_spec=?"
                    " WHERE thread_id=? AND message_id=?",
                    (question, sql, json.dumps(cube_spec or {}, ensure_ascii=False),
                     thread_id, message_id),
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

        2026-09-02 复活规则：取消反馈（撤销）会把原标注回滚为 rejected（见
        revoke_annotations_for_message），用户对同一条消息**重新反馈**时须把
        rejected 复活为 queued——否则该反馈永远不再出现在待判断列表。rejected
        属 _TERMINAL（update_annotation 状态机禁止回退），故用直连 UPDATE；
        question/sql 新值此时可能为空（后台快照补齐尚未完成），保留旧值避免
        标题回退「无问题摘要」。终态 badcase/good 是已完成的标注产物，不受
        重新反馈影响，保持原样。
        """
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM feedback_annotation WHERE thread_id=? AND message_id=?",
                (thread_id, message_id),
            ).fetchone()
            if row is not None:
                cur = self._row_to_annotation(row)
                if cur.status == "rejected":
                    self._conn.execute(
                        "UPDATE feedback_annotation SET status='queued', is_valid=NULL,"
                        " question=?, bad_sql=?, rating=?, note=?, feedback_type=?,"
                        " db_name=?, annotator='', annotated_at='', badcase_at='',"
                        " bad_type='', gold_sql='', gold_result='', exec_error='',"
                        " auto_good=0"
                        " WHERE thread_id=? AND message_id=?",
                        (
                            (question or "")[:2000] or cur.question,
                            (sql or "")[:MAX_SNAPSHOT_SQL] or cur.bad_sql,
                            rating,
                            (note or "")[:2000],
                            feedback_type,
                            (db_name or "")[:128],
                            thread_id,
                            message_id,
                        ),
                    )
                    self._conn.commit()
                    return self._row_to_annotation(
                        self._conn.execute(
                            "SELECT * FROM feedback_annotation"
                            " WHERE thread_id=? AND message_id=?",
                            (thread_id, message_id),
                        ).fetchone()
                    )
                return cur
            now = _now_iso()
            rec = AnnotationRecord(
                thread_id=thread_id,
                message_id=message_id,
                feedback_type=feedback_type or "",
                question=(question or "")[:2000],
                bad_sql=(sql or "")[:MAX_SNAPSHOT_SQL],
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
            cube_spec=_loads_spec(row["cube_spec"] if "cube_spec" in row.keys() else None),
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
            auto_good=int(row["auto_good"] or 0) if "auto_good" in row.keys() else 0,
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
            "feedback_type", "question", "bad_sql", "cube_spec", "exec_error", "note",
            "rating", "db_name", "status", "is_valid", "gold_sql", "gold_result",
            "bad_type", "annotator", "annotated_at", "badcase_at", "auto_good",
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
            # cube_spec 在记录里是 dict、在库里是 JSON 串（与 context/context_json 同构）；
            # 本函数是**动态拼列**的，不像 _insert 有固定语句，故序列化只能落在绑定处。
            vals = [
                json.dumps(v, ensure_ascii=False) if k == "cube_spec" else v
                for k, v in updates.items()
            ] + [thread_id, message_id]
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

    def reopen_annotation(self, thread_id: str, message_id: str) -> AnnotationRecord | None:
        """把终态 good 撤回为 queued（「撤回入集」专用入口）。不存在返回 None。

        为什么必须另开方法：update_annotation 的「终态不可回退」是 badcase/good/
        rejected 三条终态共用的护栏（防止误操作把已定稿的标注改回去）。撤回是
        唯一合法的回退场景，且要连带清空金标字段——写成一个语义明确的动作，
        不去松动那条护栏。

        清空 gold_sql/gold_result/bad_type/annotator/auto_good，是因为这些字段
        描述的是「已入集的那个决定」，撤回后条目回到「待判断」，留着会让详情页
        显示一份不存在的金标。bad_sql（模型当时的 SQL）保留——它是事实，不是决定。
        """
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM feedback_annotation WHERE thread_id=? AND message_id=?",
                (thread_id, message_id),
            ).fetchone()
            if row is None:
                return None
            if self._row_to_annotation(row).status != "good":
                return None  # 只撤 good；其余状态不该走这条路
            self._conn.execute(
                "UPDATE feedback_annotation SET status='queued', is_valid=NULL,"
                " gold_sql='', gold_result='', bad_type='', annotator='',"
                " annotated_at='', badcase_at='', auto_good=0"
                " WHERE thread_id=? AND message_id=?",
                (thread_id, message_id),
            )
            self._conn.commit()
            return self._row_to_annotation(
                self._conn.execute(
                    "SELECT * FROM feedback_annotation WHERE thread_id=? AND message_id=?",
                    (thread_id, message_id),
                ).fetchone()
            )

    def delete_annotation(
        self,
        thread_id: str,
        message_id: str,
        expected_status: Optional[str] = None,
    ) -> bool:
        """硬删一条标注队列条目。返回是否真的删到了行。

        expected_status 给定时按状态条件删除（CAS）——这不是可选的谨慎，是必需的：
        「确认入 BadCase」在「读 ann → 执行金标 SQL → 写终态」之间隔着一次真实库
        往返（秒级），先查后删会删掉一条刚刚变成 badcase 的行，把它在 Langfuse
        Dataset:badcase 与 badcase_status.json 里的产物留成孤儿。

        返回 False 有两种含义，调用方按自己的语义区分：行本来就不存在，或行还在但
        状态已经不等于 expected_status（CAS 未命中，说明它中途变了）。

        只删这一张行。级联（用户反馈、Langfuse 哨兵分）由调用方按序处理，见
        api.message_feedback.purge_feedback。
        """
        sql = "DELETE FROM feedback_annotation WHERE thread_id=? AND message_id=?"
        params: tuple = (thread_id, message_id)
        if expected_status is not None:
            sql += " AND status=?"
            params += (expected_status,)
        with _LOCK:
            cur = self._conn.execute(sql, params)
            if cur.rowcount:
                self._conn.commit()
                return True
        return False

    # ── 看板聚合（优化②）──────────────────────────────────

    def count_annotations(self) -> dict[str, int]:
        """按状态统计标注条数（标注页头部的统计条用）。零网络、瞬时。

        六个状态一律给值（缺席补 0）：调用方直接读 `d["queued"]` 即可，不必到处
        写 `d.get("queued") or 0`——空库与「查不到」在这里是同一件事，不该让每个
        读方各写一遍兜底。
        """
        with _LOCK:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM feedback_annotation GROUP BY status"
            ).fetchall()
            seen = {r["status"] or "": int(r["n"]) for r in rows}
        return {s: seen.get(s, 0) for s in ANNOTATION_STATUSES}

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
