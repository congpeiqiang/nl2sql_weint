import json, sys
d=json.load(open('state_dump2.json',encoding='utf-8'))
vals=d.get('values',{})
def show(k):
    if k in vals:
        sys.stdout.write('=== %s ===\n' % k)
        sys.stdout.write(json.dumps(vals[k],ensure_ascii=False)[:2000]+'\n')
for k in ['query_header','query_headers','subagent_steps','subagent_steps_map','query_active','active_queries','async_tasks']:
    show(k)
# next / tasks (pending tool calls)
sys.stdout.write('=== next ===\n%s\n' % json.dumps(d.get('next'),ensure_ascii=False))
sys.stdout.write('=== tasks (pending) ===\n%s\n' % json.dumps(d.get('tasks'),ensure_ascii=False)[:2000])
