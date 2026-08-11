# -*- coding: utf-8 -*-
"""获取最新查询任务（02:09:58）的完整信息"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"
TRACE_ID = "019fd9fb-fc87-7aa1-802d-f10e73c1bb85"

out = []
trace_runs = list(client.list_runs(
    project_name="nl2sql",
    trace_id=TRACE_ID,
    limit=100,
))

# 根节点信息
for r in trace_runs:
    if r.name == "chat_agent" and r.parent_run_id is None:
        out.append("=== 最新查询任务: chat_agent ===")
        out.append("trace_id: %s" % r.id)
        out.append("start: %s" % r.start_time)
        out.append("end: %s" % r.end_time)
        if r.inputs:
            msgs = r.inputs.get("messages", [])
            for m in msgs:
                out.append("  [%s] %s" % (m.get("type"), str(m.get("content", ""))[:500]))
        break

# 列出所有工具调用
out.append("")
out.append("=== 工具调用 ===")
for r in trace_runs:
    if r.run_type == "tool":
        dur = None
        if r.start_time and r.end_time:
            dur = round((r.end_time - r.start_time).total_seconds(), 3)
        out.append("  %s | start=%s | dur=%s" % (r.name, r.start_time, dur))

with open(HOST_TMP + r"\latest_query_info.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("done")
