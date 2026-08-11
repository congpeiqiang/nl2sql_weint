# -*- coding: utf-8 -*-
"""分析最近的 runs，找到根 trace"""
import json
from collections import Counter
from langsmith import Client

client = Client()

# 查询最近的 runs
runs = list(client.list_runs(
    project_name="nl2sql",
    limit=100,
    order_by="-start_time",
))
print("total runs fetched:", len(runs))

# 统计 parent_run_id 分布
parents = Counter()
for r in runs:
    if r.parent_run_id is None:
        parents["ROOT"] += 1
    else:
        parents[str(r.parent_run_id)] += 1

print("\n=== PARENT DISTRIBUTION (top 15) ===")
for pid, cnt in parents.most_common(15):
    print(f"{cnt:>3}  {pid}")

# 打印最近的 20 个 run 的基本信息
print("\n=== RECENT 20 RUNS ===")
for r in runs[:20]:
    dur = None
    if r.start_time and r.end_time:
        dur = round((r.end_time - r.start_time).total_seconds(), 3)
    print(f"{str(dur):>10}  {r.run_type:<12} {r.name:<40} parent={str(r.parent_run_id)[:8] if r.parent_run_id else 'ROOT'}")
