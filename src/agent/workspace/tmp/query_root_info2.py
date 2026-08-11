# -*- coding: utf-8 -*-
"""获取数据查询 trace 的根节点信息和输入内容，写入文件"""
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

roots = [r for r in trace_runs if r.parent_run_id is None]
out.append("roots: %d" % len(roots))
for rt in roots:
    out.append("  ROOT: %s %s %s" % (rt.name, rt.run_type, rt.id))
    out.append("  start: %s end: %s" % (rt.start_time, rt.end_time))
    if rt.start_time and rt.end_time:
        out.append("  total_duration_s: %.3f" % ((rt.end_time - rt.start_time).total_seconds()))
    if rt.inputs:
        out.append("  inputs: " + json.dumps(rt.inputs, ensure_ascii=False, default=str)[:3000])

if not roots:
    ids = {str(r.id) for r in trace_runs}
    top = [r for r in trace_runs if r.parent_run_id is None or str(r.parent_run_id) not in ids]
    out.append("top-level runs: %d" % len(top))
    for rt in top:
        out.append("  TOP: %s %s %s" % (rt.name, rt.run_type, rt.id))
        out.append("  start: %s end: %s" % (rt.start_time, rt.end_time))
        if rt.start_time and rt.end_time:
            out.append("  total_duration_s: %.3f" % ((rt.end_time - rt.start_time).total_seconds()))
        if rt.inputs:
            out.append("  inputs: " + json.dumps(rt.inputs, ensure_ascii=False, default=str)[:3000])

with open(HOST_TMP + r"\root_info.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("done")
