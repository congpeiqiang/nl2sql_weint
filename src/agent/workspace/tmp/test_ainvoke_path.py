import sys, asyncio, traceback
sys.stdout.reconfigure(encoding='utf-8')
from agent.tools.mcp_tool import main_tools

t = [x for x in main_tools if x.name == 'generate_echarts'][0]
opt_str = '{"title":{"text":"test"},"xAxis":{"data":["A","B"]},"yAxis":{},"series":[{"type":"bar","data":[1,2]}]}'

out = []
# 检查 ainvoke 走哪个方法
out.append('_arun code name: %s' % t._arun.__code__.co_name)
out.append('_run code name: %s' % t._run.__code__.co_name)

# 检查 ainvoke 是否调用 _arun
import inspect
out.append('ainvoke source:')
try:
    out.append(inspect.getsource(t.ainvoke))
except Exception as e:
    out.append('  cannot get source: %s' % e)

with open('workspace/tmp/tools_out18.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('DONE')
