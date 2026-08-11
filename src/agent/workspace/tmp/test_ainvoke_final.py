import sys, asyncio, traceback
sys.stdout.reconfigure(encoding='utf-8')
from agent.tools.mcp_tool import main_tools

t = [x for x in main_tools if x.name == 'generate_echarts'][0]
# 与对话中调用完全一致的参数（JSON 字符串 echartsOption）
opt_str = '{"backgroundColor":"#fff","title":{"text":"随机数据柱状图","left":"center"},"tooltip":{},"xAxis":{"type":"category","data":["类别A","类别B","类别C","类别D","类别E"]},"yAxis":{"type":"value"},"series":[{"name":"数值","type":"bar","data":[42,78,35,91,56]}]}'

out = []
try:
    r = asyncio.run(t.ainvoke({'echartsOption': opt_str, 'width': 800, 'height': 500, 'outputType': 'svg'}))
    out.append('type: %s' % type(r).__name__)
    out.append('preview: %s' % str(r)[:300])
except Exception as e:
    out.append('EXC: %s: %s' % (type(e).__name__, e))
    out.append(traceback.format_exc())

with open('workspace/tmp/tools_out25.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('DONE')
