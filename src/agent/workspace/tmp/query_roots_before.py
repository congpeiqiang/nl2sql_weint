# -*- coding: utf-8 -*-
"""查询 03:19:57 之前的所有根节点，找到最近的数据查询任务"""
import json
from langsmith import Client
from datetime import datetime, timezone

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 查询 03:19:57 之前的 runs
cutoff = datetime(2026, 8, 7, 3, 19, 57, tzinfo=timezone.utc)
runs = list(client.list_runs(
    project_name="nl2sql",
    limit=100,
    order_by="-start_time",
    end_time=cutoff,
))
print("runs before 03:19:57:", len(runs))

# 找根节点
roots = [r for r in runs if r.parent_run_id is None]
print("root runs:", len(roots))
for rt in roots:
    dur = None
    if rt.start_time and rt.end_time:
        dur = round((rt.end_time - rt.start_time).total_seconds(), 3)
    print("  ROOT: %s | %s | start=%s | dur=%s | id=%s" % (rt.name, rt.run_type, rt.start_time, dur, rt.id))
