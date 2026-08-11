# -*- coding: utf-8 -*-
"""查询 pg::gpt-5.6-terra 项目最近的 runs，找到 019fee9a 任务"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

proj = "pg::gpt-5.6-terra::6ac5570e"
runs = list(client.list_runs(
    project_name=proj,
    limit=50,
    order_by="-start_time",
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

with open(HOST_TMP + r"\pg_terra_runs.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("DONE", len(out))
