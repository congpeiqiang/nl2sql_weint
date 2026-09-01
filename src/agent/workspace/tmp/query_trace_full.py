# -*- coding: utf-8 -*-
import os, json
from langsmith import Client

client = Client()

# 查询 trace 01a021f8 的所有 runs（含 IO）
trace_id = "01a021f8-8b29-7b02-a2dc-355608b0cf55"
runs = client.list_runs(
    project_name="nl2sql",
    filter=f'and(eq(trace_id, "{trace_id}"))',
    limit=100,
)

all_runs = []
for r in runs:
    all_runs.append({
        "name": r.name,
        "run_type": r.run_type,
        "start": r.start_time.isoformat() if r.start_time else None,
        "end": r.end_time.isoformat() if r.end_time else None,
        "duration": round((r.end_time - r.start_time).total_seconds(), 3) if r.start_time and r.end_time else None,
        "run_id": str(r.id),
        "parent_run_id": str(r.parent_run_id) if r.parent_run_id else None,
        "inputs": r.inputs,
        "outputs": r.outputs,
    })

with open(r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\trace_01a021f8_full.json", "w", encoding="utf-8") as f:
    json.dump(all_runs, f, ensure_ascii=False, indent=2, default=str)

print("total runs:", len(all_runs))
