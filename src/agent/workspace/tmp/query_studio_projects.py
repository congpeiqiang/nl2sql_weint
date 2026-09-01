# -*- coding: utf-8 -*-
import os, json
from langsmith import Client

client = Client()

# 查询 studio 项目下的 runs
for proj in ["studio::nl2sql_agent::9ac9c340", "studio::nl2sql_agent::aa79fbc2"]:
    try:
        runs = client.list_runs(
            project_name=proj,
            filter='and(gte(start_time, "2026-08-21T00:00:00Z"))',
            limit=100,
        )
        traces = {}
        for r in runs:
            tid = str(r.trace_id)
            if tid not in traces:
                traces[tid] = {"count": 0, "names": set(), "start": None}
            traces[tid]["count"] += 1
            traces[tid]["names"].add(r.name)
            if r.start_time and (traces[tid]["start"] is None or r.start_time < traces[tid]["start"]):
                traces[tid]["start"] = r.start_time
        print(f"=== {proj} ===")
        print("total runs:", len(runs))
        for tid, info in traces.items():
            print(tid, "| runs:", info["count"], "| start:", info["start"].isoformat() if info["start"] else None)
    except Exception as e:
        print(f"=== {proj} === ERROR: {e}")
