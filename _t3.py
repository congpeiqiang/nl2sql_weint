import json
d=json.load(open('t_dump.json',encoding='utf-8'))
vals=d.get('values',{})
msgs=vals.get('messages',[])
# 完整打印 chart 工具结果和 report-export skill 内容
for i,m in enumerate(msgs):
    if not isinstance(m,dict): continue
    tcid=m.get('tool_call_id')
    if tcid in ('call_01_T4CXiexW2D3KAQzFGRt34135','call_00_Qde3hXCc7t1EVMbOq8JT4904'):
        print('==== msg',i,'====')
        print(str(m.get('content',''))[:3000])
        print()
