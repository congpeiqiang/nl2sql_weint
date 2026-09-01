# -*- coding: utf-8 -*-
import os, json
from langsmith import Client

client = Client()

# 查询子智能体任务 01a021e9 的 trace
# 子智能体 trace_id 以 thread_id 前缀 01a021e9 开头
runs = client.list_runs(
    project_name="nl2sql",
    filter='and(gte(start_time, "2026-08-21T00:00:00Z"))',
    limit=100,
)

# 收集所有 trace_id，找出以 01a021e9 开头的
traces = {}
for r in runs:
    tid = str(r.trace_id)
    if tid.startswith("01a021e9"):
        if tid not in traces:
            traces[tid] = []
        traces[tid].append({
            "name": r.name,
            "run_type": r.run_type,
            "start": r.start_time.isoformat() if r.start_time else None,
            "end": r.end_time.isoformat() if r.end_time else None,
            "duration": round((r.end_time - r.start_time).total_seconds(), 3) if r.start_time and r.end_time else None,
            "run_id": str(r.id),
            "parent_run_id": str(r.parent_run_id) if r.parent_run_id else None,
        })

with open(r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\subagent_trace_01a021e9.json", "w", encoding="utf-8") as f:
    json.dump(traces, f, ensure_ascii=False, indent=2, default=str)

print("matching traces:", len(traces))
for tid, runs_list in traces.items():
    print(tid, "runs:", len(runs_list))
