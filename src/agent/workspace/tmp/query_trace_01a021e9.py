# -*- coding: utf-8 -*-
import os, json
from langsmith import Client

client = Client()

# 查询最近的任务 trace，任务 thread_id 前缀 01a021e9
runs = client.list_runs(
    project_name="nl2sql",
    filter='and(gte(start_time, "2026-08-21T00:00:00Z"))',
    limit=100,
)

traces = {}
for r in runs:
    tid = r.trace_id
    if tid not in traces:
        traces[tid] = []
    traces[tid].append({
        "name": r.name,
        "run_type": r.run_type,
        "start": r.start_time.isoformat() if r.start_time else None,
        "end": r.end_time.isoformat() if r.end_time else None,
        "duration": (r.end_time - r.start_time).total_seconds() if r.start_time and r.end_time else None,
        "run_id": str(r.id),
        "parent_run_id": str(r.parent_run_id) if r.parent_run_id else None,
    })

with open(r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\traces_summary.json", "w", encoding="utf-8") as f:
    json.dump({str(k): v for k, v in traces.items()}, f, ensure_ascii=False, indent=2, default=str)

print("total traces:", len(traces))
for tid, runs_list in traces.items():
    print(tid, "runs:", len(runs_list))
