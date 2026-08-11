import sys, asyncio, traceback
sys.stdout.reconfigure(encoding='utf-8')
from agent.tools.mcp_tool import main_tools

t = [x for x in main_tools if x.name == 'generate_echarts'][0]

out = []
out.append('has coroutine: %s' % hasattr(t, 'coroutine'))
if hasattr(t, 'coroutine'):
    out.append('coroutine: %s' % t.coroutine)
    out.append('coroutine code name: %s' % t.coroutine.__code__.co_name if hasattr(t.coroutine, '__code__') else 'N/A')

# 检查 _arun 是否被 ainvoke 调用
# 检查 BaseTool.arun 的实现
import inspect
from langchain_core.tools import BaseTool
out.append('--- BaseTool.arun source ---')
try:
    out.append(inspect.getsource(BaseTool.arun))
except Exception as e:
    out.append('  cannot get: %s' % e)

with open('workspace/tmp/tools_out20.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('DONE')
