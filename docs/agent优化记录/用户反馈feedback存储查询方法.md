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
