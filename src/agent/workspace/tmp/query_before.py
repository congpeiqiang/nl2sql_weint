# -*- coding: utf-8 -*-
"""查询 nl2sql project 中当前会话之前最近一次完整 trace"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 当前会话开始时间约 2026-08-07 02:26，查询之前的 runs
# 用 end_time 过滤
from datetime import datetime, timezone
cutoff = datetime(2026, 8, 7, 2, 26, 0, tzinfo=timezone.utc)

runs = list(client.list_runs(
    project_name="nl2sql",
    limit=100,
    order_by="-start_time",
    end_time=cutoff,
))
print("runs before cutoff:", len(runs))

# 找根节点
roots = [r for r in runs if r.parent_run_id is None]
print("root runs:", len(roots))
for rt in roots:
    dur = None
    if rt.start_time and rt.end_time:
        dur = round((rt.end_time - rt.start_time).total_seconds(), 3)
    print(f"  ROOT: {rt.name} | {rt.run_type} | start={rt.start_time} | dur={dur} | id={rt.id}")

# 打印最近的 30 个 run
print("\n=== RECENT 30 RUNS (before cutoff) ===")
for r in runs[:30]:
    dur = None
    if r.start_time and r.end_time:
        dur = round((r.end_time - r.start_time).total_seconds(), 3)
    print(f"{str(dur):>10}  {r.run_type:<12} {r.name:<45} parent={str(r.parent_run_id)[:8] if r.parent_run_id else 'ROOT'}")
