# -*- coding: utf-8 -*-
import sys, asyncio, os
sys.path.insert(0, "D:/code_work_space/llm/nl2sql/src")
out = open("D:/code_work_space/llm/nl2sql/src/agent/tmp/_st.txt", "w", encoding="utf-8")
sys.stdout = out
from langgraph_sdk import get_client

TID = "019fd620-d980-7910-be44-ed33c6dfb6e8"
async def main():
    import os as _os
    client = get_client(url=_os.getenv("LANGGRAPH_API_URL", "http://localhost:2026"))
    # 直接写一个纯测试 key（as_node=__start__）
    try:
        await client.threads.update_state(TID, {"query_headers": {"PURE": {"content":"纯测试","status":"query","task_id":"PURE"}}}, as_node="__start__")
        print("写入 PURE 调用成功")
    except Exception as e:
        print("写入失败:", str(e)[:120])
    vals = (await client.threads.get_state(TID))["values"]
    qh = vals.get('query_headers', {})
    print("query_headers keys:", [k[:10] for k in qh])
    print("含 PURE:", "PURE" in qh)

asyncio.run(main())
out.close()
