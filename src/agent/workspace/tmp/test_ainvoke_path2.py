import sys, asyncio, traceback
sys.stdout.reconfigure(encoding='utf-8')
from agent.tools.mcp_tool import main_tools
from agent.utils.path_resolver import _is_chart_tool

t = [x for x in main_tools if x.name == 'generate_echarts'][0]
opt_str = '{"title":{"text":"test"},"xAxis":{"data":["A","B"]},"yAxis":{},"series":[{"type":"bar","data":[1,2]}]}'

out = []
out.append('is_chart: %s' % _is_chart_tool(t))

# 手动模拟 wrapped_arun 的 sanitize 逻辑
async def test():
    # 直接调用原始 coroutine（绕过 wrapped_arun 的异常捕获）
    # 先看 _arun 的闭包
    import types
    # 调用 ainvoke
    r = await t.ainvoke({'echartsOption': opt_str, 'width': 400, 'height': 300, 'outputType': 'svg'})
    out.append('ainvoke type: %s' % type(r).__name__)
    out.append('ainvoke preview: %s' % str(r)[:200])
    # 手动 sanitize
    from agent.utils.path_resolver import _sanitize_chart_result
    r2 = _sanitize_chart_result(r, True)
    out.append('manual sanitize type: %s' % type(r2).__name__)
    out.append('manual sanitize preview: %s' % str(r2)[:200])

asyncio.run(test())

with open('workspace/tmp/tools_out19.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('DONE')
