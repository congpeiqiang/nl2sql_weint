# -*- coding: utf-8 -*-
"""在 nl2sql 项目下查找 trace_id 以 019fee9a 开头的 runs"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 查询 nl2sql 项目最近的 runs
runs = list(client.list_runs(
    project_name="nl2sql",
    limit=100,
    order_by="-start_time",
))

# 查找 trace_id 以 019fee9a 开头的
matches = [r for r in runs if r.trace_id and str(r.trace_id).startswith("019fee9a")]
print("matches for 019fee9a:", len(matches))

out = []
for r in matches:
    dur = None
    if r.start_time and r.end_time:
        dur = round((r.end_time - r.start_time).total_seconds(), 3)
    out.append({
        "name": r.name,
        "run_type": r.run_type,
        "start": str(r.start_time),
        "duration_s": dur,
        "parent_id": str(r.parent_run_id)[:8] if r.parent_run_id else None,
        "trace_id": str(r.trace_id),
        "id": str(r.id),
    })

with open(HOST_TMP + r"\trace_019fee9a.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("DONE")
