"""Query and analyze the current conversation trace from LangSmith."""
import json, sys
from collections import defaultdict
from datetime import timezone
from langsmith import Client
from uuid import UUID

HOST_TMP = r'D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp'
OUT = HOST_TMP + r'\trace_analysis_out.json'
LOG = HOST_TMP + r'\trace_analysis_out.log'

def log(msg):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(msg + '\n')

client = Client()
trace_id = UUID('01a01403-1fdb-7d43-aa43-b715be286daa')

# Fetch runs in pages using start_time filter
all_runs = []
last_start = None
page = 0

log('Fetching runs...')
while True:
    page += 1
    filter_str = f'and(eq(trace_id, "{trace_id}"))'
    batch = list(client.list_runs(
        project_name='nl2sql',
        filter=filter_str,
        limit=100,
    ))
    log(f'Page {page}: got {len(batch)} runs')
    if not batch:
        break
    all_runs.extend(batch)
    if len(batch) < 100:
        break
    if page > 5:
        log('Max pages reached')
        break

log(f'Total runs: {len(all_runs)}')

# Analyze
def dur(r):
    if r.start_time and r.end_time:
        return (r.end_time - r.start_time).total_seconds()
    return 0.0

# Group LLM calls
llm_calls = []
tool_calls = defaultdict(lambda: {'count': 0, 'total_dur': 0.0})

for r in all_runs:
    d = dur(r)
    if r.run_type == 'llm':
        llm_calls.append({
            'name': r.name,
            'start': r.start_time.isoformat() if r.start_time else None,
            'dur': round(d, 1),
        })
    elif r.run_type == 'tool':
        tool_calls[r.name]['count'] += 1
        tool_calls[r.name]['total_dur'] += d

# Serialize
result = {
    'trace_id': str(trace_id),
    'total_runs': len(all_runs),
    'total_dur': round(sum(dur(r) for r in all_runs), 1),
    'llm_calls': llm_calls,
    'tool_calls': {k: {'count': v['count'], 'total_dur': round(v['total_dur'], 1)} for k, v in sorted(tool_calls.items(), key=lambda x: -x[1]['total_dur'])},
}

with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

log('DONE')
log(json.dumps(result, ensure_ascii=False, indent=2))