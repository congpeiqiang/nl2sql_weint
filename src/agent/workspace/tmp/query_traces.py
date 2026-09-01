import sys, traceback

LOG = r'D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\trace_debug.log'
def log(msg):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(msg + '\n')

try:
    from langsmith import Client
    log('import langsmith OK')
    client = Client()
    log('client created')
    runs = list(client.list_runs(project_name='nl2sql', limit=50, order_by='start_time'))
    log(f'Total runs: {len(runs)}')
    for r in runs:
        dt = r.start_time.strftime('%H:%M:%S') if r.start_time else 'N/A'
        dur = f'{(r.end_time - r.start_time).total_seconds():.1f}s' if r.start_time and r.end_time else 'N/A'
        name = r.name or 'unnamed'
        pt = r.parent_run_id or 'ROOT'
        log(f'{dt} | {dur:>8s} | {r.run_type:8s} | parent={str(pt)[:8]:8s} | {name[:80]}')
    log('DONE')
except Exception as e:
    log('ERROR: ' + repr(e))
    log(traceback.format_exc())