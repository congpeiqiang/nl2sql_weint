# -*- coding: utf-8 -*-
"""查看更早的 chat_agent 输入，确认哪些是查询任务"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

traces = [
    ("019fd9f4-ff4f-74f2-a80d-9fbcbee29ec5", "02:02:20"),
    ("019fd9e9-b3fd-7c10-8e44-2c1dd310342e", "01:50:00"),
]

out = []
for tid, label in traces:
    out.append("=== trace %s (%s) ===" % (tid, label))
    trace_runs = list(client.list_runs(
        project_name="nl2sql",
        trace_id=tid,
        limit=100,
    ))
    for r in trace_runs:
        if r.name == "chat_agent" and r.parent_run_id is None:
            out.append("start: %s | end: %s" % (r.start_time, r.end_time))
            if r.inputs:
                msgs = r.inputs.get("messages", [])
                for m in msgs:
                    out.append("  [%s] %s" % (m.get("type"), str(m.get("content", ""))[:300]))
            break
    out.append("")

with open(HOST_TMP + r"\earlier_chat_input.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("done")
