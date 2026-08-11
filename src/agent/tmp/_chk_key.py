# -*- coding: utf-8 -*-
import sys, asyncio, os
sys.path.insert(0, "D:/code_work_space/llm/nl2sql/src")
out = open("D:/code_work_space/llm/nl2sql/src/agent/tmp/_ck.txt", "w", encoding="utf-8")
sys.stdout = out
from langgraph_sdk import get_client
from agent.subagents.sync_subagent_todos import _sync_update_state

TID = "019fd620-d980-7910-be44-ed33c6dfb6e8"
TASK1 = "019fd621-07c8-7bb1-9d86-a0d1f3c0b000"  # 完整 task_id 待确认

async def main():
    import os as _os, asyncio as aio
    client = get_client(url=_os.getenv("LANGGRAPH_API_URL", "http://localhost:2026"))
    # 拿任务1完整 task_id
    st = await client.threads.get_state(TID)
    at = (st['values'] or {}).get('async_tasks', {})
    t1 = [k for k in at if k.startswith('019fd621-07c')][0]
    print("任务1完整 key:", t1)
    # 写两个 key：任务1 真实 key + 一个测试 key
    await aio.to_thread(_sync_update_state, TID, {"query_headers": {
        t1: {"content":"📋 测试1","status":"query","task_id":t1},
        "TESTKEY99": {"content":"测试99","status":"query","task_id":"TESTKEY99"},
    }})
    vals = (await client.threads.get_state(TID))["values"]
    qh = vals.get('query_headers', {})
    print("写入后含任务1:", t1 in qh, "| 含TESTKEY99:", "TESTKEY99" in qh)
    print("query_headers keys:", [k[:12] for k in qh])

asyncio.run(main())
out.close()
