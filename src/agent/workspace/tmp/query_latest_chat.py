# -*- coding: utf-8 -*-
"""获取最新 chat_agent 根节点的输入"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"
TRACE_ID = "019fda3c-0fc4-77d0-8bd3-c9de388380be"

out = []
trace_runs = list(client.list_runs(
    project_name="nl2sql",
    trace_id=TRACE_ID,
    limit=100,
))

for r in trace_runs:
    if r.name == "chat_agent" and r.parent_run_id is None:
        out.append("=== ROOT: chat_agent ===")
        out.append("id: %s" % r.id)
        out.append("start: %s" % r.start_time)
        out.append("end: %s" % r.end_time)
        if r.inputs:
            msgs = r.inputs.get("messages", [])
            out.append("num_messages: %d" % len(msgs))
            for m in msgs:
                content = m.get("content", "")
                out.append("  [%s] %s" % (m.get("type"), str(content)[:500]))
        break

with open(HOST_TMP + r"\latest_chat_input.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("done")
