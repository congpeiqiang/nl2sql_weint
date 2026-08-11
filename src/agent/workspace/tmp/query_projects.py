# -*- coding: utf-8 -*-
"""查询各 project 最近活动，找到最近一次数据查询 trace"""
from langsmith import Client

client = Client()

projects_to_check = [
    "nl2sql",
    "studio::nl2sql_agent::9ac9c340",
    "studio::nl2sql_agent::aa79fbc2",
    "pg::gpt-5.6-terra::6ac5570e",
]

for proj in projects_to_check:
    print(f"\n===== PROJECT: {proj} =====")
    try:
        runs = list(client.list_runs(
            project_name=proj,
            limit=5,
            order_by="-start_time",
        ))
        for r in runs:
            dur = None
            if r.start_time and r.end_time:
                dur = round((r.end_time - r.start_time).total_seconds(), 3)
            print(f"  {r.name} | {r.run_type} | start={r.start_time} | dur={dur} | parent={str(r.parent_run_id)[:8] if r.parent_run_id else 'ROOT'} | id={r.id}")
    except Exception as e:
        print("  ERROR:", e)
