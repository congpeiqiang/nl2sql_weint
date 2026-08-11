# -*- coding: utf-8 -*-
"""查询 nl2sql 项目最近一次完整 trace，分析各步骤耗时"""
import json
from langsmith import Client

client = Client()

# 查询最近的 runs（按时间倒序），找到根节点（parent_run_id 为 None 的）
runs = list(client.list_runs(
    project_name="nl2sql",
    limit=100,
    order_by="-start_time",
))
print("total runs fetched:", len(runs))

# 找根节点：parent_run_id 为 None 的
roots = [r for r in runs if r.parent_run_id is None]
print("root runs found:", len(roots))

if not roots:
    print("NO_ROOT")
    raise SystemExit(0)

# 取最近一个根节点
root = roots[0]
print("\n=== ROOT RUN ===")
print("id:", root.id)
print("name:", root.name)
print("run_type:", root.run_type)
print("start_time:", root.start_time)
print("end_time:", root.end_time)
if root.start_time and root.end_time:
    print("total_duration_s:", round((root.end_time - root.start_time).total_seconds(), 3))

# 获取该 trace 的所有子 run
child_runs = list(client.list_runs(
    project_name="nl2sql",
    trace_id=root.id,
    limit=100,
))
print("\n=== CHILD RUNS (count=%d) ===" % len(child_runs))

# 构建耗时列表
rows = []
for r in child_runs:
    dur = None
    if r.start_time and r.end_time:
        dur = (r.end_time - r.start_time).total_seconds()
    rows.append({
        "name": r.name,
        "run_type": r.run_type,
        "start": str(r.start_time),
        "duration_s": round(dur, 3) if dur is not None else None,
        "id": str(r.id),
        "parent_id": str(r.parent_run_id) if r.parent_run_id else None,
    })

# 按耗时排序
rows_sorted = sorted(rows, key=lambda x: (x["duration_s"] is None, -(x["duration_s"] or 0)))
print("\n=== RUNS SORTED BY DURATION (desc) ===")
for r in rows_sorted:
    print(f"{str(r['duration_s']):>10}  {r['run_type']:<12} {r['name']}")

# 保存完整数据
with open("/workspace/tmp/trace_data.json", "w", encoding="utf-8") as f:
    json.dump({
        "root": {
            "id": str(root.id),
            "name": root.name,
            "start": str(root.start_time),
            "end": str(root.end_time),
            "total_duration_s": round((root.end_time - root.start_time).total_seconds(), 3) if root.start_time and root.end_time else None,
        },
        "runs": rows_sorted,
    }, f, ensure_ascii=False, indent=2)
print("\nSaved to /workspace/tmp/trace_data.json")
