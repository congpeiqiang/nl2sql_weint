# -*- coding: utf-8 -*-
import sys, asyncio, os
sys.path.insert(0, "D:/code_work_space/llm/nl2sql/src")
out = open("D:/code_work_space/llm/nl2sql/src/agent/tmp/_cnew.txt", "w", encoding="utf-8")
sys.stdout = out
from langgraph_sdk import get_client

TID = "13edf164-a29e-4fc3-9a19-e34dfe3aa13b"
async def main():
    import os as _os
    client = get_client(url=_os.getenv("LANGGRAPH_API_URL", "http://localhost:2026"))
    st = await client.threads.get_state(TID)
    vals = st.get('values') or {}
    at = vals.get('async_tasks') or {}
    # 找最新任务（2000-2023电视剧平均评分）
    latest = sorted(at.items(), key=lambda x: x[1].get('created_at',''))[-1]
    k, v = latest
    print("最新任务:", k[:12], "| status:", v.get('status'), "| created:", v.get('created_at'))
    # 子线程
    sub = await client.threads.get_state(k)
    sv = sub.get('values') or {}
    msgs = sv.get('messages') or []
    todos = sv.get('todos') or []
    print("子线程 values keys:", list(sv.keys()))
    print("子线程 todos:", len(todos), "| 消息:", len(msgs))
    # write_todos 调用
    wt = 0
    for m in msgs:
        for x in (m.get('tool_calls') or []):
            if x.get('name') == 'write_todos': wt += 1
    print("write_todos 调用:", wt)
    # 主 todos 图表/报告
    main_todos = vals.get('todos') or []
    print("主 todos 图表/报告条目:")
    for t in main_todos:
        if '渲染' in t.get('content','') or '生成' in t.get('content',''):
            print("  ", t.get('status'), '|', t.get('content',''))
    print("主 todos 委派条目:")
    for t in main_todos:
        if '委派' in t.get('content',''):
            print("  ", t.get('status'), '|', t.get('content','')[:30])

asyncio.run(main())
out.close()
