import sys, asyncio, traceback
sys.stdout.reconfigure(encoding='utf-8')
from agent.tools.mcp_tool import main_tools

t = [x for x in main_tools if x.name == 'generate_echarts'][0]
opt = {'title': {'text': 'test'}, 'xAxis': {'data': ['A', 'B']}, 'yAxis': {}, 'series': [{'type': 'bar', 'data': [1, 2]}]}

out = []
# 尝试通过 invoke 调用，捕获 ToolException
try:
    r = t.invoke({'echartsOption': opt, 'width': 400, 'height': 300, 'outputType': 'svg'})
    out.append('invoke type: %s' % type(r).__name__)
    out.append('invoke preview: %s' % str(r)[:500])
except Exception as e:
    out.append('invoke EXC: %s: %s' % (type(e).__name__, e))
    out.append(traceback.format_exc())

with open('workspace/tmp/tools_out11.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('DONE')
