# -*- coding: utf-8 -*-
"""查询指定数据查询 trace 的完整运行树，分析各步骤耗时"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"
TRACE_ID = "019fd9fc"  # 根节点前缀，需要完整 id

# 先找到这个 trace 的完整根节点 id
# 从之前查询知道 parent=019fd9fc，需要完整 id
# 查询该 trace 的所有 runs
runs = list(client.list_runs(
    project_name="nl2sql",
    trace_id=TRACE_ID,
    limit=100,
))
print("runs in trace (prefix match):", len(runs))

if not runs:
    print("No runs found with prefix, trying full trace query")
    # 尝试用完整 trace_id 查询
    # 先找到根节点
    all_runs = list(client.list_runs(
        project_name="nl2sql",
        limit=100,
        order_by="-start_time",
        end_time=__import__('datetime').datetime(2026, 8, 7, 2, 20, 0, tzinfo=__import__('datetime').timezone.utc),
    ))
    roots = [r for r in all_runs if r.parent_run_id is None]
    print("roots found:", len(roots))
    for rt in roots:
        print("  ROOT:", rt.name, rt.id, rt.start_time)
    raise SystemExit(0)

# 构建 run 字典
run_map = {}
for r in runs:
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

# 找根节点
roots = [v for v in run_map.values() if v["parent_id"] is None]
print("roots in trace:", len(roots))
for rt in roots:
    print("  ROOT:", rt["name"], rt["run_type"], rt["id"], "dur=", rt["duration_s"])

# 按耗时排序
rows_sorted = sorted(run_map.values(), key=lambda x: (x["duration_s"] is None, -(x["duration_s"] or 0)))
print("\n=== ALL RUNS SORTED BY DURATION (desc) ===")
for r in rows_sorted:
    print(f"{str(r['duration_s']):>10}  {r['run_type']:<12} {r['name']}")

# 保存
with open(HOST_TMP + r"\trace_data.json", "w", encoding="utf-8") as f:
    json.dump({"trace_id": TRACE_ID, "runs": rows_sorted}, f, ensure_ascii=False, indent=2)
print("\nSaved to", HOST_TMP + r"\trace_data.json")
