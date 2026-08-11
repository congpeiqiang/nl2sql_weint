import re
from pathlib import Path

p = Path(r'D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\root_info.txt')
text = p.read_text(encoding='utf-8')

result = []

# Extract TOP lines with run types
result.append("=== TOP runs (name, run_type, id) ===")
tops = re.findall(r'TOP: (\S+) (\S+) (\S+)', text)
for name, rtype, rid in tops:
    result.append(f'name={name} run_type={rtype} id={rid}')

# Extract start/end times
result.append("\n=== Start/End times ===")
times = re.findall(r'start: (\S+) end: (\S+)', text)
for s, e in times:
    result.append(f'start={s} end={e}')

# Extract write_todos content
result.append("\n=== write_todos content ===")
todos = re.findall(r'"content": "([^"]*todos[^"]*)"', text)
for t in todos:
    result.append(t)

# Extract the task goal
result.append("\n=== Task goal ===")
goals = re.findall(r'【任务目标】([^\n]+)', text)
for g in goals:
    result.append(g)

# Extract tool call names from inputs (the actual tool calls made)
result.append("\n=== Tool calls in inputs ===")
tool_calls = re.findall(r'"name": "([^"]+)", "type": "tool_call"', text)
tc_counter = {}
for tc in tool_calls:
    tc_counter[tc] = tc_counter.get(tc, 0) + 1
for tc, count in sorted(tc_counter.items(), key=lambda x: -x[1]):
    result.append(f'{tc}: {count}')

out_path = Path(r'D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\root_info_analysis2.txt')
out_path.write_text('\n'.join(result), encoding='utf-8')
