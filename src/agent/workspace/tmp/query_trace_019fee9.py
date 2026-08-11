# -*- coding: utf-8 -*-
"""查询 nl2sql 项目中所有以 019fee9 开头的 trace_id 的 runs"""
import json
from langsmith import Client
from datetime import datetime, timezone

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 查询今天的所有 runs（分页获取更多）
start = datetime(2026, 8, 11, 0, 0, 0, tzinfo=timezone.utc)
all_runs = []
cursor = None
for _ in range(10):  # 最多 10 页
    batch = list(client.list_runs(
        project_name="nl2sql",
        limit=100,
        order_by="-start_time",
        start_time=start,
    ))
    if not batch:
        break
    all_runs.extend(batch)
    # 用最后一条的 start_time 作为游标继续
    last_start = batch[-1].start_time
    if last_start:
        start = last_start
    else:
        break

# 收集以 019fee9 开头的 trace_id
target_traces = set()
for r in all_runs:
    if r.trace_id and str(r.trace_id).startswith("019fee9"):
        target_traces.add(str(r.trace_id))

print("traces starting with 019fee9:", sorted(target_traces))

with open(HOST_TMP + r"\trace_019fee9.json", "w", encoding="utf-8") as f:
    json.dump({"traces": sorted(target_traces), "total_runs": len(all_runs)}, f, ensure_ascii=False, indent=2)
print("DONE total_runs:", len(all_runs))
