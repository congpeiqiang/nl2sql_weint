# -*- coding: utf-8 -*-
"""找到根节点 019fd9fc 的完整 id，并查询完整运行树"""
import json
from langsmith import Client
from datetime import datetime, timezone

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 查询 02:20 之前的 runs，找到根节点 019fd9fc 的完整 id
cutoff = datetime(2026, 8, 7, 2, 20, 0, tzinfo=timezone.utc)
runs = list(client.list_runs(
    project_name="nl2sql",
    limit=100,
    order_by="-start_time",
    end_time=cutoff,
))

# 找到 parent 为 019fd9fc 开头的 run，获取其 trace_id
target_trace = None
for r in runs:
    if r.parent_run_id and str(r.parent_run_id).startswith("019fd9fc"):
        target_trace = r.trace_id
        print("Found run with parent 019fd9fc:", r.name, "trace_id=", target_trace)
        break

if not target_trace:
    # 尝试直接找根节点
    roots = [r for r in runs if r.parent_run_id is None]
    print("roots:", len(roots))
    for rt in roots:
        print("  ROOT:", rt.name, rt.id, rt.start_time)
    raise SystemExit(0)

print("\n=== Querying full trace:", target_trace, "===")
trace_runs = list(client.list_runs(
    project_name="nl2sql",
    trace_id=target_trace,
    limit=100,
))
print("runs in trace:", len(trace_runs))

run_map = {}
for r in trace_runs:
    dur = None
    if r.start_time and r.end_time:
        dur = (r.end_time - r.start_time).total_seconds()
    run_map[str(r.id)] = {
        "name": r.name,
        "run_type": r.run_type,
        "start": str(r.start_time),
        "end": str(r.end_time),
        "duration_s": round(dur, 3) if dur is not None else None,
        "parent_id": str(r.parent_run_id) if r.parent_run_id else None,
        "id": str(r.id),
    }

roots = [v for v in run_map.values() if v["parent_id"] is None]
print("roots in trace:", len(roots))
for rt in roots:
    print("  ROOT:", rt["name"], rt["run_type"], rt["id"], "dur=", rt["duration_s"])

rows_sorted = sorted(run_map.values(), key=lambda x: (x["duration_s"] is None, -(x["duration_s"] or 0)))
print("\n=== ALL RUNS SORTED BY DURATION (desc) ===")
for r in rows_sorted:
    print(f"{str(r['duration_s']):>10}  {r['run_type']:<12} {r['name']}")

with open(HOST_TMP + r"\trace_data.json", "w", encoding="utf-8") as f:
    json.dump({"trace_id": target_trace, "runs": rows_sorted}, f, ensure_ascii=False, indent=2)
print("\nSaved to", HOST_TMP + r"\trace_data.json")
