import sys, asyncio, traceback
sys.stdout.reconfigure(encoding='utf-8')
from langchain_mcp_adapters.client import MultiServerMCPClient

async def main():
    servers = {
        "mcp-server-echarts": {
            "transport": "stdio",
            "command": "mcp-echarts",
            "args": [],
            "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        }
    }
    out = []
    try:
        client = MultiServerMCPClient(servers)
        tools = await client.get_tools()
        out.append('tools: %s' % [t.name for t in tools])
        t = [x for x in tools if x.name == 'generate_echarts'][0]
        opt = {'title': {'text': 'test'}, 'xAxis': {'data': ['A', 'B']}, 'yAxis': {}, 'series': [{'type': 'bar', 'data': [1, 2]}]}
        try:
            r = await t.ainvoke({'echartsOption': opt, 'width': 400, 'height': 300, 'outputType': 'svg'})
            out.append('type: %s' % type(r).__name__)
            out.append('preview: %s' % str(r)[:500])
        except Exception as e:
            out.append('ainvoke EXC: %s: %s' % (type(e).__name__, e))
            out.append(traceback.format_exc())
    except Exception as e:
        out.append('MAIN EXC: %s: %s' % (type(e).__name__, e))
        out.append(traceback.format_exc())

    with open('workspace/tmp/tools_out12.txt', 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))

asyncio.run(main())
print('DONE')
