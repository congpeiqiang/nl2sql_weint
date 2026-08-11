# -*- coding: utf-8 -*-
"""查询 trace 019fee9a-16a2-7903-8455-2badd2dc6e6c 的完整运行树"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

trace_id = "019fee9a-16a2-7903-8455-2badd2dc6e6c"
runs = list(client.list_runs(
    project_name="nl2sql",
    trace_id=trace_id,
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
        "end": str(r.end_time) if r.end_time else None,
        "duration_s": dur,
        "parent_id": str(r.parent_run_id)[:8] if r.parent_run_id else None,
        "id": str(r.id),
    })

with open(HOST_TMP + r"\trace_019fee9a_full.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("DONE runs:", len(out))
