# -*- coding: utf-8 -*-
"""查询 02:12:00 到 03:19:57 之间的根节点"""
import json
from langsmith import Client
from datetime import datetime, timezone

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 查询 02:12:00 之前的 runs（跳过当前会话和上一个查询）
cutoff = datetime(2026, 8, 7, 2, 12, 0, tzinfo=timezone.utc)
runs = list(client.list_runs(
    project_name="nl2sql",
    limit=100,
    order_by="-start_time",
    end_time=cutoff,
))
print("runs before 02:12:00:", len(runs))

roots = [r for r in runs if r.parent_run_id is None]
print("root runs:", len(roots))
for rt in roots:
    dur = None
    if rt.start_time and rt.end_time:
        dur = round((rt.end_time - rt.start_time).total_seconds(), 3)
    print("  ROOT: %s | %s | start=%s | dur=%s | id=%s" % (rt.name, rt.run_type, rt.start_time, dur, rt.id))
