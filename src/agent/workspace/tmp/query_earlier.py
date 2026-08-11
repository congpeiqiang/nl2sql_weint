# -*- coding: utf-8 -*-
"""查询 nl2sql project 中更早的 runs（当前会话之前）"""
from langsmith import Client
from datetime import datetime, timezone

client = Client()

# 查询 02:20 之前的 runs
cutoff = datetime(2026, 8, 7, 2, 20, 0, tzinfo=timezone.utc)

runs = list(client.list_runs(
    project_name="nl2sql",
    limit=100,
    order_by="-start_time",
    start_time=None,
    end_time=cutoff,
))
print("runs before 02:20:", len(runs))

# 找根节点
roots = [r for r in runs if r.parent_run_id is None]
print("root runs:", len(roots))
for rt in roots:
    dur = None
    if rt.start_time and rt.end_time:
        dur = round((rt.end_time - rt.start_time).total_seconds(), 3)
    print(f"  ROOT: {rt.name} | {rt.run_type} | start={rt.start_time} | dur={dur} | id={rt.id}")

# 打印最近的 30 个 run
print("\n=== RECENT 30 RUNS (before 02:20) ===")
for r in runs[:30]:
    dur = None
    if r.start_time and r.end_time:
        dur = round((r.end_time - r.start_time).total_seconds(), 3)
    print(f"{str(dur):>10}  {r.run_type:<12} {r.name:<45} parent={str(r.parent_run_id)[:8] if r.parent_run_id else 'ROOT'} start={r.start_time}")
