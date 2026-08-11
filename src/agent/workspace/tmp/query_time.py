# -*- coding: utf-8 -*-
"""获取数据查询 trace 的根节点时间信息"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"
TRACE_ID = "019fd9fc-2855-7352-81ed-4b79ecfeffb4"

out = []
trace_runs = list(client.list_runs(
    project_name="nl2sql",
    trace_id=TRACE_ID,
    limit=100,
))

# 找最早开始的 run（即用户提问的起点）
runs_sorted = sorted(trace_runs, key=lambda r: r.start_time or 0)
out.append("=== 最早开始的 runs（用户提问起点）===")
for r in runs_sorted[:5]:
    out.append("  %s | %s | start=%s | end=%s" % (r.name, r.run_type, r.start_time, r.end_time))

out.append("")
out.append("=== 最晚结束的 runs（查询完成）===")
runs_sorted_end = sorted(trace_runs, key=lambda r: r.end_time or 0, reverse=True)
for r in runs_sorted_end[:5]:
    out.append("  %s | %s | start=%s | end=%s" % (r.name, r.run_type, r.start_time, r.end_time))

# 找根节点（parent 不在本 trace 中的顶层 run）
ids = {str(r.id) for r in trace_runs}
top = [r for r in trace_runs if r.parent_run_id is None or str(r.parent_run_id) not in ids]
out.append("")
out.append("=== 顶层 runs（根节点）===")
for rt in sorted(top, key=lambda r: r.start_time or 0):
    out.append("  %s | %s | start=%s | end=%s" % (rt.name, rt.run_type, rt.start_time, rt.end_time))

with open(HOST_TMP + r"\time_info.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("done")
