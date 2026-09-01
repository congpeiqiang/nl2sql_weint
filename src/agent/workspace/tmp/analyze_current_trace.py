"""Analyze the current conversation trace from traces.txt log file."""
import re, json
from collections import defaultdict

LOG = r'D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\trace_analysis.log'
TRACE_FILE = r'D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\traces.txt'

def log(msg):
    with open(LOG, 'w', encoding='utf-8') as f:
        f.write(msg + '\n')

with open(TRACE_FILE, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Parse: timestamp | duration | run_type | parent=xxx | name
pattern = r'(\d{2}:\d{2}:\d{2}) \|\s*([\d.]+s|N/A)\s*\|\s*(\w+)\s*\|\s*parent=(\w+)\s*\|\s*(.+)'

llm_calls = []
tool_calls = []
chain_calls = []

for line in lines:
    m = pattern.match(line.strip())
    if not m:
        continue
    ts, dur_str, rtype, parent, name = m.groups()
    if dur_str == 'N/A':
        dur = 0
    else:
        dur = float(dur_str.replace('s', ''))
    
    if rtype == 'llm':
        llm_calls.append({'ts': ts, 'dur': dur, 'name': name, 'parent': parent})
    elif rtype == 'tool':
        tool_calls.append({'ts': ts, 'dur': dur, 'name': name, 'parent': parent})

out = []
out.append('=== 本次对话各环节耗时分析 ===\n')

out.append('--- LLM 调用（ChatDeepSeek）---')
total_llm = 0
for c in llm_calls:
    total_llm += c['dur']
    out.append(f"  {c['ts']} | {c['dur']:.1f}s | {c['name']}")
out.append(f'  LLM 总耗时: {total_llm:.1f}s\n')

out.append('--- Tool 调用 ---')
by_tool = defaultdict(lambda: {'count': 0, 'total': 0.0})
for c in tool_calls:
    by_tool[c['name']]['count'] += 1
    by_tool[c['name']]['total'] += c['dur']
for name, info in sorted(by_tool.items(), key=lambda x: -x[1]['total']):
    out.append(f"  {name}: {info['count']}次, 总耗时 {info['total']:.1f}s")

out.append(f'\nTool 总耗时: {sum(c["dur"] for c in tool_calls):.1f}s\n')

# Group by parent (model turn)
out.append('--- 按 Model Turn 分组（每轮 LLM + 对应的 Tool 调用）---')
turns = defaultdict({'llm': 0, 'tools': []})
for c in llm_calls:
    turns[c['parent']]['llm'] = c['dur']
for c in tool_calls:
    turns[c['parent']]['tools'].append(c)

for parent, info in sorted(turns.items()):
    llm_dur = info['llm']
    tool_dur = sum(c['dur'] for c in info['tools'])
    tool_names = [f"{c['name']}({c['dur']:.1f}s)" for c in info['tools']]
    total = llm_dur + tool_dur
    out.append(f"  Turn parent={parent}: LLM={llm_dur:.1f}s + Tools={tool_dur:.1f}s = Total={total:.1f}s")
    for tn in tool_names:
        out.append(f"    - {tn}")

out.append('\n--- 耗时排名（从高到低）---')
all_items = []
for c in llm_calls:
    all_items.append((f"LLM/{c['name']}", c['dur']))
for name, info in by_tool.items():
    all_items.append((f"Tool/{name}(x{info['count']})", info['total']))
all_items.sort(key=lambda x: -x[1])
for item_name, dur in all_items:
    out.append(f"  {item_name}: {dur:.1f}s")

log('\n'.join(out))