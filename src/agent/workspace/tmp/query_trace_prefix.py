# -*- coding: utf-8 -*-
"""用 trace_id 前缀查询 nl2sql 项目中 019fee9a 的 runs"""
import json
from langsmith import Client

client = Client()
HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"

# 尝试直接按 trace_id 查询（需要完整 trace_id，先尝试用前缀匹配）
# 先列出 nl2sql 项目最近的 trace 根节点
runs = list(client.list_runs(
    project_name="nl2sql",
    limit=100,
    order_by="-start_time",
))

# 收集所有 trace_id
trace_ids = set()
for r in runs:
    if r.trace_id:
        trace_ids.add(str(r.trace_id))

# 找以 019fee9a 开头的
matches = [t for t in trace_ids if t.startswith("019fee9a")]
print("trace_ids starting with 019fee9a:", matches)

# 也打印所有 trace_id 前缀供参考
prefixes = sorted(set(t[:13] for t in trace_ids))
print("all trace prefixes:", prefixes)

with open(HOST_TMP + r"\trace_prefixes.json", "w", encoding="utf-8") as f:
    json.dump({"matches": matches, "all_prefixes": prefixes}, f, ensure_ascii=False, indent=2)
print("DONE")
