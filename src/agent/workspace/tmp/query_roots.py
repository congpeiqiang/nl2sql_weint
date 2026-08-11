# -*- coding: utf-8 -*-
"""用 filter 查询 nl2sql project 的根节点 runs"""
from langsmith import Client

client = Client()

# 尝试用 filter 查询根节点（parent_run_id 为 null）
# LangSmith filter 语法：'and(eq(parent_run_id, null))' 或类似
try:
    runs = list(client.list_runs(
        project_name="nl2sql",
        filter='and(eq(parent_run_id, "null"))',
        limit=20,
        order_by="-start_time",
    ))
    print("filter eq null runs:", len(runs))
    for r in runs:
        dur = None
        if r.start_time and r.end_time:
            dur = round((r.end_time - r.start_time).total_seconds(), 3)
        print(f"  {r.name} | {r.run_type} | start={r.start_time} | dur={dur} | id={r.id}")
except Exception as e:
    print("filter eq null failed:", e)

# 尝试另一种 filter
try:
    runs2 = list(client.list_runs(
        project_name="nl2sql",
        filter='and(missing(parent_run_id))',
        limit=20,
        order_by="-start_time",
    ))
    print("\nfilter missing parent runs:", len(runs2))
    for r in runs2:
        dur = None
        if r.start_time and r.end_time:
            dur = round((r.end_time - r.start_time).total_seconds(), 3)
        print(f"  {r.name} | {r.run_type} | start={r.start_time} | dur={dur} | id={r.id}")
except Exception as e:
    print("filter missing parent failed:", e)
