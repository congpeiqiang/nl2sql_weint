# -*- coding: utf-8 -*-
"""查询主智能体侧委派 nl2sql 的 trace，找到用户提问时间"""
import json
from langsmith import Client
from datetime import datetime, timezone

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 查询 02:10 之前的 runs，找到主智能体委派 nl2sql 的 trace
cutoff = datetime(2026, 8, 7, 2, 10, 52, tzinfo=timezone.utc)
runs = list(client.list_runs(
    project_name="nl2sql",
    limit=100,
    order_by="-start_time",
    end_time=cutoff,
))
print("runs before 02:10:52:", len(runs))

# 找根节点
roots = [r for r in runs if r.parent_run_id is None]
print("root runs:", len(roots))
for rt in roots:
    dur = None
    if rt.start_time and rt.end_time:
        dur = round((rt.end_time - rt.start_time).total_seconds(), 3)
    print("  ROOT: %s | %s | start=%s | dur=%s | id=%s" % (rt.name, rt.run_type, rt.start_time, dur, rt.id))

# 打印最近的 20 个 run
print("\n=== RECENT 20 RUNS (before 02:10:52) ===")
for r in runs[:20]:
    dur = None
    if r.start_time and r.end_time:
        dur = round((r.end_time - r.start_time).total_seconds(), 3)
    print("  %s | %s | start=%s | dur=%s | parent=%s" % (r.name, r.run_type, r.start_time, dur, str(r.parent_run_id)[:8] if r.parent_run_id else 'ROOT'))
