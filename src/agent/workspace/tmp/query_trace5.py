# -*- coding: utf-8 -*-
"""查找最近一次完整的数据查询 trace（nl2sql 子智能体），分析各步骤耗时"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 查询最近的 runs，找到根节点（parent_run_id 为 None 的完整 trace）
# 分页查询更多，找到最近一次完整 trace
all_runs = []
cursor = None
for _ in range(5):  # 最多查 500 条
    batch = list(client.list_runs(
        project_name="nl2sql",
        limit=100,
        order_by="-start_time",
    ))
    if not batch:
        break
    all_runs.extend(batch)
    # 简单去重后判断是否已覆盖
    break  # 先只查一批

# 找根节点
roots = [r for r in all_runs if r.parent_run_id is None]
print("total runs:", len(all_runs))
print("root runs:", len(roots))

# 打印所有根节点信息
for rt in roots:
    dur = None
    if rt.start_time and rt.end_time:
        dur = round((rt.end_time - rt.start_time).total_seconds(), 3)
    print(f"  ROOT: {rt.name} | {rt.run_type} | start={rt.start_time} | dur={dur} | id={rt.id}")

# 取最近一个根节点
if roots:
    root = roots[0]
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
