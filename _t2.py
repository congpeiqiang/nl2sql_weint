import json
d=json.load(open('t_dump.json',encoding='utf-8'))
vals=d.get('values',{})
msgs=vals.get('messages',[])
print('total messages:', len(msgs))
for i,m in enumerate(msgs):
    if not isinstance(m,dict): 
        print(i,'[non-dict]',str(m)[:100]); continue
    role=m.get('role') or m.get('type')
    name=m.get('name')
    content=str(m.get('content',''))
    tcs=m.get('tool_calls')
    tcid=m.get('tool_call_id')
    # 只打印 AI 工具调用、tool 结果、和文本内容的概要
    if role in ('ai','assistant'):
        line=f'[{i}]{role}'
        if tcs: line += f' tool_calls={json.dumps([tc.get("name") for tc in tcs],ensure_ascii=False)}'
        if content: line += f' text={content[:200]}'
        print(line)
    elif role in ('tool','function'):
        print(f'[{i}]{role} call_id={tcid} content={content[:300]}')
    elif content:
        print(f'[{i}]{role} content={content[:200]}')
