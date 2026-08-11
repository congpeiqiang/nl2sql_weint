# -*- coding: utf-8 -*-
"""查询指定 trace 的完整运行树，分析各步骤耗时"""
import json
from langsmith import Client

client = Client()
TRACE_ID = "019fda09-6d76-7210-8f33-910acf3870f9"

# 获取该 trace 的所有 runs
runs = list(client.list_runs(
    project_name="nl2sql",
    trace_id=TRACE_ID,
    limit=100,
))
print("total runs in trace:", len(runs))

# 构建 run 字典
run_map = {}
for r in runs:
    dur = None
    if r.start_time and r.end_time:
        dur = (r.end_time - r.start_time).total_seconds()
    run_map[str(r.id)] = {
        "name": r.name,
        "run_type": r.run_type,
        "start": r.start_time,
        "end": r.end_time,
        "duration_s": round(dur, 3) if dur is not None else None,
        "parent_id": str(r.parent_run_id) if r.parent_run_id else None,
        "id": str(r.id),
    }

# 找根节点
roots = [v for v in run_map.values() if v["parent_id"] is None]
print("roots:", len(roots))
for rt in roots:
    print("  ROOT:", rt["name"], rt["run_type"], rt["id"], "dur=", rt["duration_s"])

# 按耗时排序所有 runs
rows_sorted = sorted(run_map.values(), key=lambda x: (x["duration_s"] is None, -(x["duration_s"] or 0)))
print("\n=== ALL RUNS SORTED BY DURATION (desc) ===")
for r in rows_sorted:
    print(f"{str(r['duration_s']):>10}  {r['run_type']:<12} {r['name']}")

# 保存
with open("/workspace/tmp/trace_data.json", "w", encoding="utf-8") as f:
    json.dump({"trace_id": TRACE_ID, "runs": rows_sorted}, f, ensure_ascii=False, indent=2)
print("\nSaved to /workspace/tmp/trace_data.json")
