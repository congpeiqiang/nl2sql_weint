# -*- coding: utf-8 -*-
"""分页查询，找到最近一次完整的数据查询 trace"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 分页查询最近的 runs，找到根节点
all_runs = []
seen_ids = set()
for page in range(10):  # 最多查 10 页 = 1000 条
    batch = list(client.list_runs(
        project_name="nl2sql",
        limit=100,
        order_by="-start_time",
        offset=page * 100,
    ))
    if not batch:
        break
    new = [r for r in batch if str(r.id) not in seen_ids]
    for r in new:
        seen_ids.add(str(r.id))
    all_runs.extend(new)
    print(f"page {page}: fetched {len(batch)}, new {len(new)}")

print("\ntotal unique runs:", len(all_runs))

# 找根节点
roots = [r for r in all_runs if r.parent_run_id is None]
print("root runs found:", len(roots))

# 打印所有根节点（按时间倒序）
roots_sorted = sorted(roots, key=lambda r: r.start_time or 0, reverse=True)
for rt in roots_sorted[:10]:
    dur = None
    if rt.start_time and rt.end_time:
        dur = round((rt.end_time - rt.start_time).total_seconds(), 3)
    print(f"  ROOT: {rt.name} | {rt.run_type} | start={rt.start_time} | dur={dur} | id={rt.id}")

# 取最近一个根节点
if roots_sorted:
    root = roots_sorted[0]
    print("\n=== SELECTED ROOT ===")
    print("name:", root.name)
    print("id:", root.id)
    print("start:", root.start_time)
    print("end:", root.end_time)
    if root.start_time and root.end_time:
        print("total_duration_s:", round((root.end_time - root.start_time).total_seconds(), 3))

    # 获取该 trace 的所有 runs
    runs = list(client.list_runs(
        project_name="nl2sql",
        trace_id=root.id,
        limit=100,
    ))
    print("runs in trace:", len(runs))

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

    rows_sorted = sorted(run_map.values(), key=lambda x: (x["duration_s"] is None, -(x["duration_s"] or 0)))
    print("\n=== ALL RUNS SORTED BY DURATION (desc) ===")
    for r in rows_sorted:
        print(f"{str(r['duration_s']):>10}  {r['run_type']:<12} {r['name']}")

    with open(HOST_TMP + r"\trace_data.json", "w", encoding="utf-8") as f:
        json.dump({"trace_id": str(root.id), "root_name": root.name, "runs": rows_sorted}, f, ensure_ascii=False, indent=2)
    print("\nSaved to", HOST_TMP + r"\trace_data.json")
