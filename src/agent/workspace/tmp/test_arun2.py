import sys, asyncio, traceback
sys.stdout.reconfigure(encoding='utf-8')
from agent.tools.mcp_tool import main_tools

t = [x for x in main_tools if x.name == 'generate_echarts'][0]
opt = {'title': {'text': 'test'}, 'xAxis': {'data': ['A', 'B']}, 'yAxis': {}, 'series': [{'type': 'bar', 'data': [1, 2]}]}

out = []
try:
    # 直接调用原始 _arun，绕过 wrap_tool 的错误捕获
    r = asyncio.run(t._arun({'echartsOption': opt, 'width': 400, 'height': 300, 'outputType': 'svg'}, config={}))
    out.append('type: %s' % type(r).__name__)
    out.append('preview: %s' % str(r)[:500])
except Exception as e:
    out.append('EXC: %s: %s' % (type(e).__name__, e))
    out.append(traceback.format_exc())

with open('workspace/tmp/tools_out10.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('DONE')
