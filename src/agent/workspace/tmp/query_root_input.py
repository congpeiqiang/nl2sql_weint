# -*- coding: utf-8 -*-
"""获取 nl2sql_agent 根节点的输入（用户提问内容）"""
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

# 找根节点 nl2sql_agent
for r in trace_runs:
    if r.name == "nl2sql_agent" and r.parent_run_id is None:
        out.append("=== ROOT: nl2sql_agent ===")
        out.append("id: %s" % r.id)
        out.append("start: %s" % r.start_time)
        out.append("end: %s" % r.end_time)
        if r.start_time and r.end_time:
            out.append("total_duration_s: %.3f" % ((r.end_time - r.start_time).total_seconds()))
        if r.inputs:
            out.append("inputs: " + json.dumps(r.inputs, ensure_ascii=False, default=str)[:3000])
        break

with open(HOST_TMP + r"\root_input.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("done")
