# -*- coding: utf-8 -*-
"""列出所有 project 及最近活动"""
from langsmith import Client

client = Client()
projects = client.list_projects()
for p in projects:
    print(f"project: {p.name} | id={p.id}")
