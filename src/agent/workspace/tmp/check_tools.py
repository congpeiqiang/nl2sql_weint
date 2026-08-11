import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from agent.tools.mcp_tool import main_tools
print("=== main_tools ===")
for t in main_tools:
    print(f"  name={t.name}, type={type(t).__name__}")

# 检查 generate_echarts 是否被 wrap
from agent.utils.path_resolver import _is_chart_tool, _sanitize_chart_result
for t in main_tools:
    if "echarts" in t.name.lower() or "chart" in t.name.lower():
        print(f"\n=== {t.name} ===")
        print(f"  is_chart_tool: {_is_chart_tool(t)}")
        print(f"  has _run: {hasattr(t, '_run')}")
        print(f"  has _arun: {hasattr(t, '_arun')}")
