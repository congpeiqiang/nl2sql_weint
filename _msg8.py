import json
d=json.load(open('t_dump.json',encoding='utf-8'))
msgs=d.get('values',{}).get('messages',[])
for i,m in enumerate(msgs):
    if not isinstance(m,dict): continue
    tcs=m.get('tool_calls')
    if tcs:
        for tc in tcs:
            if tc.get('name') in ('write_todos','check_async_task'):
                print(f'[msg {i}] {tc.get("name")} args={json.dumps(tc.get("args",{}),ensure_ascii=False)[:800]}')
    # 系统自动通知
    c=str(m.get('content',''))
    if '[系统自动通知]' in c:
        print(f'[msg {i}] 系统通知: {c[:300]}')
