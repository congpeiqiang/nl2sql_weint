# -*- coding: utf-8 -*-
import io
content = io.open(r'D:\code_work_space\llm\nl2sql\src\agent\nl2sql_agent.py', encoding='utf-8').read()
io.open(r'D:\code_work_space\llm\nl2sql\src\agent\workspace\nl2sql_agent_dump.txt', 'w', encoding='utf-8').write(content)
print('DONE')
