import json
d=json.load(open('t_dump.json',encoding='utf-8'))
vals=d.get('values',{})
msgs=vals.get('messages',[])
# 完整打印 report-export skill 内容
for i,m in enumerate(msgs):
    if not isinstance(m,dict): continue
    if m.get('tool_call_id')=='call_00_Qde3hXCc7t1EVMbOq8JT4904':
        content=str(m.get('content',''))
        print(content[2800:5500])
