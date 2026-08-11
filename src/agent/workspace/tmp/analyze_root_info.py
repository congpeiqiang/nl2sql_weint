import re
from pathlib import Path

p = Path(r'D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\root_info.txt')
text = p.read_text(encoding='utf-8')

# Extract all tool call names
names = re.findall(r'"name": "([^"]+)"', text)
from collections import Counter
counter = Counter(names)

result = []
result.append("=== Tool call names and counts ===")
for name, count in counter.most_common():
    result.append(f'{name}: {count}')

# Extract SQL queries
result.append("\n=== SQL queries found ===")
sqls = re.findall(r'"sql": "([^"]+)"', text)
for sql in sqls:
    result.append(sql)

# Extract run types
result.append("\n=== Run types ===")
run_types = re.findall(r'run_type: ([^\s]+)', text)
rt_counter = Counter(run_types)
for rt, count in rt_counter.most_common():
    result.append(f'{rt}: {count}')

# Extract model names
result.append("\n=== Model names ===")
models = re.findall(r'"model_name": "([^"]+)"', text)
m_counter = Counter(models)
for m, count in m_counter.most_common():
    result.append(f'{m}: {count}')

# Extract total durations
result.append("\n=== Total durations (s) ===")
durations = re.findall(r'total_duration_s: ([\d.]+)', text)
for d in durations:
    result.append(d)

# Extract start/end times
result.append("\n=== Start/End times ===")
times = re.findall(r'start: ([^\s]+) end: ([^\s]+)', text)
for s, e in times:
    result.append(f'start={s} end={e}')

out_path = Path(r'D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\root_info_analysis.txt')
out_path.write_text('\n'.join(result), encoding='utf-8')
