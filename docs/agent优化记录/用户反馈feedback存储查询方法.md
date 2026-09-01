# Feedback 存储查询方法

反馈数据存在 SQLite 单文件 [src/agent/workspace/feedback/message_feedback.db](../../src/agent/workspace/feedback/message_feedback.db)，
（已 gitignore，不入库）。表结构：

```sql
feedback(
    thread_id     TEXT,   -- 会话 ID
    message_id    TEXT,   -- 消息 ID（lc_run--...）
    rating        TEXT,   -- positive | negative
    note          TEXT,   -- 备注（≤2KB）
    version       INTEGER,-- CAS 乐观并发版本号
    created_at    TEXT,   -- ISO8601 UTC
    updated_at    TEXT,
    context_json  TEXT,   -- {"db_name": ...}
    question      TEXT,   -- 用户提问快照
    sql           TEXT,   -- 生成 SQL 快照
    PRIMARY KEY (thread_id, message_id)
)
```

## 1. HTTP API（服务在 2026 端口运行时）

```bash
# 某会话内全部反馈
curl http://localhost:2026/api/threads/{thread_id}/feedback

# 全量导出（评测回流，含 👍 正例）
curl http://localhost:2026/api/feedback/export
```

## 2. 直接查 SQLite（离线，不用起服务）

```bash
cd d:/code_work_space/llm/nl2sql
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -c "
import sqlite3
c = sqlite3.connect('src/agent/workspace/feedback/message_feedback.db')
for r in c.execute('SELECT rating, note, question, sql, context_json, created_at FROM feedback'):
    print(r)
"
```

常用 SQL：

```sql
-- 全量
SELECT * FROM feedback ORDER BY updated_at;

-- 按会话
SELECT * FROM feedback WHERE thread_id = '...' ORDER BY updated_at;

-- 只查负例（bad case 评测集）
SELECT thread_id, message_id, question, sql, note
FROM feedback WHERE rating = 'negative';
```

## 3. 代码里用 store

```python
from agent.feedback.store import get_store

get_store().export_all()            # 全量 list[FeedbackRecord]
get_store().list_thread(thread_id)  # 某会话
get_store().get(thread_id, message_id)  # 单条
```

## 已知边界（2026-08-17）

- `sql` 快照多数为空：SQL 实际在 nl2sql **子线程**（`run_sql` 工具）执行，
  主线程只有 `start_async_task`，提取不到 `tool_calls[].args.sql`。
- `question`/`sql` 只在**首次写入**快照，后续点 👍/👎 更新不覆盖
  （[store.py:243](../../src/agent/feedback/store.py)）；上线前写入的存量数据不会回填。

## Langfuse 打分的归属与去重（2026-08-26 修复）

背景：`put_feedback` 后台线程把反馈写进 Langfuse `user-feedback` score（好评 1 / 差评 0）。
修复前有两个问题（会话 `01a03e3c` 实测）：

1. **trace 归属错**：`_find_session_trace_id` 用 `find_session_main_trace_id`
   （会话最新 chat_agent 根），同一会话内对不同问题的反馈全归**最后一次查询**的 trace
   ——3 条反馈全落 Q4 的 `7a03f9`，Q1~Q3 各自 trace 没分。
2. **一条反馈两条记录**：评分 PUT（comment=👍/👎 auto）与存备注 PUT（comment=用户备注）
   各 `create_score` 一条、无幂等；Langfuse v4 无 score 更新/删除 API，改不了也删不掉。

修复（`langfuse_v4_reads.py` + `message_feedback.py`）：

- 新增 `find_message_trace_id(thread_id, message_id)`：升序扫 chat_agent 根
  root observation 的 output `{messages:[...]}`，第一个含 message_id 的 =
  **创建该消息的 trace**（更晚 run 的输出/输入会把它带在历史里，故升序首个命中；
  不能取「最新」）。无 message_id / 未命中回退 `find_session_main_trace_id`。
- `put_feedback` 仅**首次写入（if_version=None）或评分变化**时写分，note-only 变更
  不写分 → 同一条反馈在 Langfuse 至多一条 live score。备注仍存本地 store
  （`GET /api/threads/{tid}/feedback` / export），Langfuse comment 保留首次 auto 文本。
- score metadata 增加 `message_id`，便于审计/去重。

验证：`find_message_trace_id` 对 3 条真实反馈（`lc_run--01a03e3d/3e/3f`）各解析到
`8c52b21c132d`/`bf9c657bf263`/`f4804c512bd5`（Q1~Q3）；门控模拟
`D:/tmp/test_feedback_gate.py` 5/5 PASS（首次打分 / note-only 不打分 / 评分变化打分 /
撤销带 message_id / CAS 409）。

⚠ **需重启后端生效**；存量重复分不清理（无删除 API，服务器 ClickHouse
`ALTER TABLE events_full DELETE` 为运维层手段）。
