import sys, traceback, json

LOG = r'D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\trace_tree.log'
OUT = r'D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\trace_tree.json'
def log(msg):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(msg + '\n')

try:
    from langsmith import Client
    client = Client()
    # 获取更多 runs，按 trace_id 分组
    runs = list(client.list_runs(project_name='nl2sql', limit=100, order_by='start_time'))
    log(f'Total runs fetched: {len(runs)}')

    # 按 trace_id 分组
    traces = {}
    for r in runs:
        tid = r.trace_id
        traces.setdefault(str(tid), []).append(r)

    # 汇总每个 trace 的起止时间和 run 数
    summ = []
    for tid, rs in traces.items():
        times = sorted([(r.start_time, r.end_time) for r in rs if r.start_time and r.end_time])
        if times:
            start = times[0][0]
            end = max(t[1] for t in times)
            dur = (end - start).total_seconds()
        else:
            dur = 0
        summ.append((start, dur, len(rs), tid))
    summ.sort(key=lambda x: x[0], reverse=True)

    with open(OUT, 'w', encoding='utf-8') as f:
        for start, dur, n, tid in summ:
            f.write(f'{start.strftime("%H:%M:%S")} | dur={dur:7.1f}s | runs={n:3d} | {tid}\n')
    log('DONE')
except Exception as e:
    log('ERROR: ' + repr(e))
    log(traceback.format_exc())