# -*- coding: utf-8 -*-
import sys, asyncio, os
sys.path.insert(0, "D:/code_work_space/llm/nl2sql/src")
out = open("D:/code_work_space/llm/nl2sql/src/agent/tmp/_tl.txt", "w", encoding="utf-8")
sys.stdout = out
from langgraph_sdk import get_client
from agent.subagents.sync_subagent_todos import _locked_update_state, _SYNC_WRITE_LOCK

TID = "019fd59f-63b0-7d20-bc11-830dc56e224f"
async def main():
    client = get_client(url=os.getenv("LANGGRAPH_API_URL", "http://localhost:2026"))
    # 用带锁的 _locked_update_state 并发写两个 key（模拟 sync 线程）
    await asyncio.gather(
        _locked_update_state(client, TID, {"query_headers": {"LCK1": {"content":"A","status":"query","task_id":"LCK1"}}}),
        _locked_update_state(client, TID, {"query_headers": {"LCK2": {"content":"B","status":"query","task_id":"LCK2"}}}),
    )
    vals = (await client.threads.get_state(TID))["values"]
    qh = vals.get('query_headers', {})
    print("带锁并发写 LCK1+LCK2 后:")
    print("  LCK1:", "LCK1" in qh, "| LCK2:", "LCK2" in qh)

asyncio.run(main())
out.close()
