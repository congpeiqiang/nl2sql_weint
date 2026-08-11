# -*- coding: utf-8 -*-
"""尝试用 thread_id 作为 trace_id 查询 019fee9a 的 runs"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 尝试多个可能的 trace_id
candidates = [
    "019fee9a-1690-7ab2-833e-cc91315df58d",  # thread_id
]

out = {}
for tid in candidates:
    try:
        runs = list(client.list_runs(
            project_name="nl2sql",
            trace_id=tid,
            limit=100,
        ))
        out[tid] = []
        for r in runs:
            dur = None
            if r.start_time and r.end_time:
                dur = round((r.end_time - r.start_time).total_seconds(), 3)
            out[tid].append({
                "name": r.name,
                "run_type": r.run_type,
                "start": str(r.start_time),
                "duration_s": dur,
                "parent_id": str(r.parent_run_id)[:8] if r.parent_run_id else None,
                "id": str(r.id),
            })
        print("trace", tid, "runs:", len(runs))
    except Exception as e:
        print("trace", tid, "ERROR:", e)

with open(HOST_TMP + r"\trace_019fee9a_thread.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("DONE")
