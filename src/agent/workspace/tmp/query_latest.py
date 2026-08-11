# -*- coding: utf-8 -*-
"""查询 nl2sql 项目最新的查询任务 trace"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 查询 nl2sql 项目最新的 runs
runs = list(client.list_runs(
    project_name="nl2sql",
    limit=100,
    order_by="-start_time",
))
print("total runs fetched:", len(runs))

# 找根节点（chat_agent 或 nl2sql_agent）
roots = [r for r in runs if r.parent_run_id is None]
print("root runs:", len(roots))
for rt in roots:
    dur = None
    if rt.start_time and rt.end_time:
        dur = round((rt.end_time - rt.start_time).total_seconds(), 3)
    print("  ROOT: %s | %s | start=%s | dur=%s | id=%s" % (rt.name, rt.run_type, rt.start_time, dur, rt.id))

# 打印最近的 40 个 run
print("\n=== RECENT 40 RUNS ===")
for r in runs[:40]:
    dur = None
    if r.start_time and r.end_time:
        dur = round((r.end_time - r.start_time).total_seconds(), 3)
    print("  %s | %s | start=%s | dur=%s | parent=%s" % (r.name, r.run_type, r.start_time, dur, str(r.parent_run_id)[:8] if r.parent_run_id else 'ROOT'))
