# -*- coding: utf-8 -*-
"""查询 nl2sql 项目最近的 traces，分析各阶段耗时"""
import json
from langsmith import Client
from datetime import datetime, timezone

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 列出最近的 runs（根节点）
runs = list(client.list_runs(
    project_name="nl2sql",
    limit=20,
    order_by="-start_time",
))

out = []
for r in runs:
    dur = None
    if r.start_time and r.end_time:
        dur = round((r.end_time - r.start_time).total_seconds(), 3)
    out.append({
        "name": r.name,
        "run_type": r.run_type,
        "start": str(r.start_time),
        "duration_s": dur,
        "parent_id": str(r.parent_run_id)[:8] if r.parent_run_id else None,
        "trace_id": str(r.trace_id)[:8] if r.trace_id else None,
        "id": str(r.id),
    })

with open(HOST_TMP + r"\recent_runs.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("DONE", len(out))
