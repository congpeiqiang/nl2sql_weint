# -*- coding: utf-8 -*-
"""查询 nl2sql 项目中 trace_id 以 019fee9 开头的 runs（用 filter 限制时间）"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 用 filter 查询今天 02:00 之后的 runs
runs = list(client.list_runs(
    project_name="nl2sql",
    filter='and(gte(start_time, "2026-08-11T02:00:00Z"))',
    limit=100,
    order_by="-start_time",
))

# 过滤出 trace_id 以 019fee9 开头的
target = [r for r in runs if r.trace_id and str(r.trace_id).startswith("019fee9")]
print("runs with trace 019fee9:", len(target))

out = []
for r in target:
    dur = None
    if r.start_time and r.end_time:
        dur = round((r.end_time - r.start_time).total_seconds(), 3)
    out.append({
        "name": r.name,
        "run_type": r.run_type,
        "start": str(r.start_time),
        "duration_s": dur,
        "parent_id": str(r.parent_run_id)[:8] if r.parent_run_id else None,
        "trace_id": str(r.trace_id) if r.trace_id else None,
        "id": str(r.id),
    })

with open(HOST_TMP + r"\trace_019fee9_all.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("DONE")
