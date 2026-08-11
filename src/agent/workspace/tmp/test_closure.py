import sys, asyncio, traceback
sys.stdout.reconfigure(encoding='utf-8')
from agent.tools.mcp_tool import main_tools

t = [x for x in main_tools if x.name == 'generate_echarts'][0]
opt_str = '{"title":{"text":"test"},"xAxis":{"data":["A","B"]},"yAxis":{},"series":[{"type":"bar","data":[1,2]}]}'

out = []
# 检查 _arun 的闭包，看 is_chart 和 original_arun
closure = t._arun.__closure__
out.append('_arun closure cells: %d' % len(closure))
for i, cell in enumerate(closure):
    try:
        val = cell.cell_contents
        out.append('  cell[%d]: type=%s, preview=%s' % (i, type(val).__name__, str(val)[:100]))
    except Exception as e:
        out.append('  cell[%d]: <empty> %s' % (i, e))

with open('workspace/tmp/tools_out21.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('DONE')
