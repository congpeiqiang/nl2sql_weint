import sys
sys.stdout.reconfigure(encoding='utf-8')
from agent.utils.path_resolver import _sanitize_echarts_result

# 模拟 echarts-mcp 返回的 list 格式
result = [{'type': 'text', 'text': '<svg width="400" height="300"><rect width="400" height="300" fill="red"/></svg>'}]

r = _sanitize_echarts_result(result)
out = []
out.append('type: %s' % type(r).__name__)
out.append('preview: %s' % str(r)[:300])

with open('workspace/tmp/tools_out15.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('DONE')
