# -*- coding: utf-8 -*-
"""分析 trace 019fee9a 各阶段耗时，写入文件"""
import json
from collections import defaultdict

HOST_TMP = r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp"
with open(HOST_TMP + r"\trace_019fee9a_full.json", "r", encoding="utf-8") as f:
    runs = json.load(f)

lines = []

runs_with_dur = [r for r in runs if r["duration_s"] is not None]
runs_with_dur.sort(key=lambda x: -x["duration_s"])

lines.append("=== TOP 20 最耗时的 runs ===")
for r in runs_with_dur[:20]:
    lines.append(f"{r['duration_s']:>10.3f}s  {r['run_type']:<12} {r['name']}  parent={r['parent_id']}")

type_total = defaultdict(float)
type_count = defaultdict(int)
for r in runs_with_dur:
    type_total[r["run_type"]] += r["duration_s"]
    type_count[r["run_type"]] += 1

lines.append("\n=== 各 run_type 总耗时 ===")
for t, total in sorted(type_total.items(), key=lambda x: -x[1]):
    lines.append(f"{total:>10.3f}s  {t:<12} count={type_count[t]}")

lines.append("\n=== 工具调用耗时 ===")
tool_runs = [r for r in runs if r["run_type"] == "tool"]
for r in sorted(tool_runs, key=lambda x: -(x["duration_s"] or 0)):
    lines.append(f"{str(r['duration_s']):>10}  {r['name']}  start={r['start']}")

with open(HOST_TMP + r"\trace_019fee9a_analysis.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("DONE")
