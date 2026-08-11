import json
d=json.load(open('t_dump.json',encoding='utf-8'))
vals=d.get('values',{})
print('values keys:', list(vals.keys()))
print('next:', d.get('next'))
print('checkpoint_id:', d.get('checkpoint_id'))
for k in ['query_header','query_headers','subagent_steps','subagent_steps_map','query_active','active_queries','async_tasks','todos']:
    if k in vals:
        import sys
        sys.stdout.write('\n=== %s ===\n' % k)
        sys.stdout.write(json.dumps(vals[k],ensure_ascii=False)[:2500]+'\n')
