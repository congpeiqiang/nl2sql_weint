import json, sys
for sub in ["019fdbec-15fe-7ac2-87c3-b75bef3f869f","019fdbec-f1a3-7ec0-9bea-9a07787ae437"]:
    try:
        d=json.load(open(f'sub_dump_{sub}.json',encoding='utf-8'))
    except Exception as e:
        sys.stdout.write(f'{sub}: parse fail {e}\n'); continue
    vals=d.get('values',{})
    nxt=d.get('next',[])
    sys.stdout.write(f'=== {sub} ===\n')
    sys.stdout.write(f'  next: {nxt}\n')
    sys.stdout.write(f'  values keys: {list(vals.keys())}\n')
    sys.stdout.write(f'  checkpoint_id: {d.get("checkpoint_id")}\n')
    # run status hint
    for k in ['active_queries','async_tasks','todo_list','query_headers']:
        if k in vals:
            sys.stdout.write(f'  {k}: {json.dumps(vals[k],ensure_ascii=False)[:400]}\n')
    sys.stdout.write('\n')
