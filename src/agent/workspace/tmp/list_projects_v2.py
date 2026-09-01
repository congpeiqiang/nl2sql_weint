# -*- coding: utf-8 -*-
import os, json
from langsmith import Client

client = Client()

projects = client.list_projects()
proj_list = []
for p in projects:
    proj_list.append({
        "name": p.name,
        "last_run_start_time": p.last_run_start_time.isoformat() if p.last_run_start_time else None,
    })

with open(r"D:\code_work_space\llm\nl2sql\src\agent\workspace\tmp\projects_list.json", "w", encoding="utf-8") as f:
    json.dump(proj_list, f, ensure_ascii=False, indent=2, default=str)

print("projects:", len(proj_list))
for p in proj_list:
    print(p["name"], "| last:", p["last_run_start_time"])
