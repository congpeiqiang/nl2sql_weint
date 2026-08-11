import json
d=json.load(open('t_dump.json',encoding='utf-8'))
vals=d.get('values',{})
msgs=vals.get('messages',[])
for i,m in enumerate(msgs):
    if not isinstance(m,dict): continue
    if m.get('tool_call_id')=='call_00_Qde3hXCc7t1EVMbOq8JT4904':
        c=str(m.get('content',''))
        print(c[0:1400])
        print('......[LINE 76-140]......')
        print(c[1400:2800])
