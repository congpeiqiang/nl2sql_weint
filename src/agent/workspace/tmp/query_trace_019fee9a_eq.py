# -*- coding: utf-8 -*-
"""用 eq filter 查询 nl2sql 项目中 trace_id 为 019fee9a 的 runs"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 尝试用 thread_id 作为 trace_id
trace_id = "019fee9a-1690-7ab2-833e-cc91315df58d"
runs = list(client.list_runs(
    project_name="nl2sql",
    filter=f'and(eq(trace_id, "{trace_id}"))',
    limit=100,
))

out = []
for r in runs:
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

with open(HOST_TMP + r"\trace_019fee9a_eq.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("DONE runs:", len(out))
