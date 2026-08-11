import sys, asyncio, traceback, logging
sys.stdout.reconfigure(encoding='utf-8')

# 配置日志到文件
logging.basicConfig(
    level=logging.WARNING,
    format='%(levelname)s %(name)s: %(message)s',
    handlers=[logging.FileHandler('workspace/tmp/arun_log.txt', encoding='utf-8')]
)
from agent.tools.mcp_tool import main_tools

t = [x for x in main_tools if x.name == 'generate_echarts'][0]
opt_str = '{"title":{"text":"test"},"xAxis":{"data":["A","B"]},"yAxis":{},"series":[{"type":"bar","data":[1,2]}]}'

out = []
try:
    r = asyncio.run(t.ainvoke({'echartsOption': opt_str, 'width': 400, 'height': 300, 'outputType': 'svg'}))
    out.append('ainvoke type: %s' % type(r).__name__)
    out.append('ainvoke preview: %s' % str(r)[:200])
except Exception as e:
    out.append('EXC: %s: %s' % (type(e).__name__, e))
    out.append(traceback.format_exc())

with open('workspace/tmp/tools_out23.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('DONE')
