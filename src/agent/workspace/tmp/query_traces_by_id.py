# -*- coding: utf-8 -*-
import os, json
from langsmith import Client

client = Client()

# 查询 nl2sql 项目最近的所有 runs
runs = client.list_runs(
    project_name="nl2sql",
    filter='and(gte(start_time, "2026-08-21T01:00:00Z"))',
    limit=100,
)

# 按 trace_id 分组
traces = {}
for r in runs:
    tid = str(r.trace_id)
    if tid not in traces:
        traces[tid] = {"count": 0, "names": set(), "start": None, "end": None}
    traces[tid]["count"] += 1
    traces[tid]["names"].add(r.name)
    if r.start_time and (traces[tid]["start"] is None or r.start_time < traces[tid]["start"]):
        traces[tid]["start"] = r.start_time
    if r.end_time and (traces[tid]["end"] is None or r.end_time > traces[tid]["end"]):
        traces[tid]["end"] = r.end_time

result = {}
for tid, info in traces.items():
    result[tid] = {
        "count": info["count"],
        "names": sorted(info["names"]),
        "start": info["start"].isoformat() if info["start"] else None,
        "end": info["end"].isoformat() if info["end"] else None,
    }

with open(r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\traces_by_id.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)

print("total traces:", len(traces))
for tid, info in result.items():
    print(tid, "| runs:", info["count"], "| start:", info["start"])
