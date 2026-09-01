# -*- coding: utf-8 -*-
"""查询 LangSmith trace 的每个步骤耗时"""
import os, json
from dotenv import load_dotenv

# 加载 .env
load_dotenv(r"D:\code_work_space\llm\nl2sql\.env")
print(f"API_KEY: {bool(os.environ.get('LANGSMITH_API_KEY'))}")

from langsmith import Client

client = Client()

trace_id = "01a021e9-5aa8-7342-bbc9-73cc69afd22e"

# 获取该 trace 下的所有 run
runs = list(client.list_runs(
    project_name="nl2sql",
    trace_ids=[trace_id],
))

print(f"\n总共 {len(runs)} 个 run\n")

# 按开始时间排序
runs_sorted = sorted(runs, key=lambda r: r.start_time or "")

# 构建父子树结构
run_map = {}
for r in runs_sorted:
    dur = (r.end_time - r.start_time).total_seconds() if r.start_time and r.end_time else None
    run_map[str(r.id)] = {
        "id": str(r.id),
        "name": r.name,
        "run_type": r.run_type,
        "start": r.start_time.isoformat() if r.start_time else None,
        "end": r.end_time.isoformat() if r.end_time else None,
        "duration_s": round(dur, 2) if dur else None,
        "parent_id": str(r.parent_run_id) if r.parent_run_id else None,
        "children": [],
        "inputs": str(r.inputs)[:200] if r.inputs else None,
        "outputs": str(r.outputs)[:200] if r.outputs else None,
        "error": str(r.error)[:200] if r.error else None,
        "tags": r.tags if r.tags else None,
        "total_tokens": r.total_tokens if r.total_tokens else None,
    }

# 构建树
roots = []
for rid, info in run_map.items():
    pid = info["parent_id"]
    if pid and pid in run_map:
        run_map[pid]["children"].append(info)
    else:
        roots.append(info)

# 打印树
def print_tree(node, indent=0, is_last=True, prefix=""):
    dur = node["duration_s"]
    dur_str = f" [{dur:.1f}s]" if dur is not None else " [N/A]"
    err = " ❌" if node.get("error") else ""
    tokens = f" (tokens: {node['total_tokens']})" if node.get("total_tokens") else ""

    conn = "└─ " if is_last else "├─ "
    print(f"{prefix}{conn}{node['name']} ({node['run_type']}){dur_str}{err}{tokens}")

    child_prefix = prefix + ("   " if is_last else "│  ")
    children = node["children"]
    for i, child in enumerate(children):
        print_tree(child, indent + 1, i == len(children) - 1, child_prefix)

for root in roots:
    print_tree(root)

# 按类型统计耗时
print("\n\n=== 按执行顺序的详细耗时 ===")
print(f"{'序号':<4} {'步骤名称':<60} {'类型':<10} {'耗时(s)':<10} {'开始时间':<30}")
print("-" * 120)

seq = 0
for r in runs_sorted:
    seq += 1
    dur = r.duration.total_seconds() if r.start_time and r.end_time and hasattr(r, 'duration') else None
    if dur is None:
        dur = (r.end_time - r.start_time).total_seconds() if r.start_time and r.end_time else None
    dur_str = f"{dur:.2f}" if dur is not None else "N/A"
    start_str = r.start_time.strftime("%H:%M:%S.%f")[:-3] if r.start_time else "N/A"
    flag = " ⚠️ SLOW" if dur and dur > 5.0 else ""
    print(f"{seq:<4} {r.name:<60} {r.run_type:<10} {dur_str:<10} {start_str:<30}{flag}")

# 汇总
print("\n\n=== 耗时 TOP 20 ===")
with_dur = [(r, (r.end_time - r.start_time).total_seconds()) for r in runs_sorted if r.start_time and r.end_time]
with_dur.sort(key=lambda x: x[1], reverse=True)
for i, (r, dur) in enumerate(with_dur[:20]):
    print(f"{i+1:<3} {r.name:<60} {r.run_type:<10} {dur:.2f}s")