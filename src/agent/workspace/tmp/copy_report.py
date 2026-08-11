# -*- coding: utf-8 -*-
import io

content = io.open(r'D:\code_work_space\llm\nl2sql\src\agent\workspace\report\SQL性能优化技能调研报告_2026-08-03.md', encoding='utf-8').read()

target = r'D:\code_work_space\llm\nl2sql\docs\agent优化记录\SQL性能优化技能调研报告_2026-08-03.md'
io.open(target, 'w', encoding='utf-8').write(content)
print('DONE')
