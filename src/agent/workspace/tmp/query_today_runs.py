# -*- coding: utf-8 -*-
"""查询各项目今天(2026-08-11)的 runs，找到 019fee9a 任务"""
import json
from langsmith import Client
from datetime import datetime, timezone

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

projects = [
    "studio::nl2sql_agent::9ac9c340",
    "studio::nl2sql_agent::aa79fbc2",
    "nl2sql",
]

# 今天 00:00 UTC
start = datetime(2026, 8, 11, 0, 0, 0, tzinfo=timezone.utc)

out = {}
for proj in projects:
    runs = list(client.list_runs(
        project_name=proj,
        limit=100,
        order_by="-start_time",
        start_time=start,
    ))
    proj_runs = []
    for r in runs:
        dur = None
        if r.start_time and r.end_time:
            dur = round((r.end_time - r.start_time).total_seconds(), 3)
        proj_runs.append({
            "name": r.name,
            "run_type": r.run_type,
            "start": str(r.start_time),
            "duration_s": dur,
            "parent_id": str(r.parent_run_id)[:8] if r.parent_run_id else None,
            "trace_id": str(r.trace_id) if r.trace_id else None,
            "id": str(r.id),
        })
    out[proj] = proj_runs

with open(HOST_TMP + r"\today_runs.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("DONE")
