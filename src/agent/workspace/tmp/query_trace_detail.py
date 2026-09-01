import sys, traceback, json
from datetime import timezone

LOG = r'D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\trace_detail.log'
def log(msg):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(msg + '\n')

try:
    from langsmith import Client
    from uuid import UUID
    client = Client()
    trace_id = UUID('01a01403-1fdb-7d43-aa43-b715be286daa')
    runs = list(client.list_runs(project_name='nl2sql', trace_id=trace_id, limit=100))
    log(f'Runs in trace: {len(runs)}')

    if len(runs) == 100:
        # try pagination to get more
        runs2 = list(client.list_runs(project_name='nl2sql', trace_id=trace_id, limit=100, offset=100))
        runs.extend(runs2)
        log(f'After pagination: {len(runs)}')

    # Recursively build tree
    run_map = {}
    for r in runs:
        run_map[r.id] = r

    # Find root(s)
    roots = [r for r in runs if r.parent_run_id is None]
    log(f'Root runs: {len(roots)}')

    def get_duration(r):
        if r.start_time and r.end_time:
            return (r.end_time - r.start_time).total_seconds()
        return 0.0

    # Collect all children
    children_of = {}
    for r in runs:
        pid = r.parent_run_id
        if pid:
            children_of.setdefault(pid, []).append(r)

    # Summarize by type and name
    from collections import defaultdict
    by_type = defaultdict(lambda: defaultdict(float))
    for r in runs:
        if r.parent_run_id is None:
            # root - skip or include as "ROOT"
            continue
        dur = get_duration(r)
        key = f"{r.run_type}/{r.name}"
        by_type[r.run_type][r.name] += dur

    # Total time by run_type
    log('=== By run_type ===')
    for rtype, names in sorted(by_type.items()):
        total = sum(names.values())
        log(f'  {rtype}: {total:.1f}s total')
        for name, dur in sorted(names.items(), key=lambda x: -x[1])[:10]:
            log(f'    {name}: {dur:.1f}s')

    # LLM calls
    log('=== LLM Calls ===')
    llm_runs = [r for r in runs if r.run_type == 'llm' and r.name == 'ChatDeepSeek']
    total_llm = 0
    for r in llm_runs:
        dur = get_duration(r)
        total_llm += dur
        log(f'  {r.start_time.strftime("%H:%M:%S")} | {dur:.1f}s | {r.name}')
    log(f'Total LLM: {total_llm:.1f}s')

    # Tool calls by name
    log('=== Tool Calls ===')
    tool_runs = [r for r in runs if r.run_type == 'tool']
    by_tool = defaultdict(float)
    for r in tool_runs:
        dur = get_duration(r)
        by_tool[r.name] += dur
    for name, dur in sorted(by_tool.items(), key=lambda x: -x[1]):
        log(f'  {name}: {dur:.1f}s')

    # Middleware summary
    log('=== Middleware Chains ===')
    chain_runs = [r for r in runs if r.run_type == 'chain']
    by_chain = defaultdict(float)
    for r in chain_runs:
        dur = get_duration(r)
        by_chain[r.name] += dur
    for name, dur in sorted(by_chain.items(), key=lambda x: -x[1]):
        log(f'  {name}: {dur:.1f}s')

    log('DONE')
except Exception as e:
    log('ERROR: ' + repr(e))
    log(traceback.format_exc())