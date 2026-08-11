# -*- coding: utf-8 -*-
"""获取数据查询 trace 的根节点信息和输入内容"""
import json
from langsmith import Client
from datetime import datetime, timezone

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"
TRACE_ID = "019fd9fc-2855-7352-81ed-4b79ecfeffb4"

# 获取该 trace 的所有 runs
trace_runs = list(client.list_runs(
    project_name="nl2sql",
    trace_id=TRACE_ID,
    limit=100,
))

# 找根节点（parent_run_id 为 None）
roots = [r for r in trace_runs if r.parent_run_id is None]
print("roots:", len(roots))
for rt in roots:
    print("  ROOT:", rt.name, rt.run_type, rt.id)
    print("  start:", rt.start_time, "end:", rt.end_time)
    if rt.start_time and rt.end_time:
        print("  total_duration_s:", round((rt.end_time - rt.start_time).total_seconds(), 3))
    # 打印输入（查询内容）
    if rt.inputs:
        print("  inputs:", json.dumps(rt.inputs, ensure_ascii=False, default=str)[:2000])

# 如果没有根节点，找 trace 中最顶层的 run
if not roots:
    # 找 parent 不在本 trace 中的 run（即顶层）
    ids = {str(r.id) for r in trace_runs}
    top = [r for r in trace_runs if r.parent_run_id is None or str(r.parent_run_id) not in ids]
    print("\ntop-level runs:", len(top))
    for rt in top:
        print("  TOP:", rt.name, rt.run_type, rt.id)
        print("  start:", rt.start_time, "end:", rt.end_time)
        if rt.start_time and rt.end_time:
            print("  total_duration_s:", round((rt.end_time - rt.start_time).total_seconds(), 3))
        if rt.inputs:
            print("  inputs:", json.dumps(rt.inputs, ensure_ascii=False, default=str)[:2000])
