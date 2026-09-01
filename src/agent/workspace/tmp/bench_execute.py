# -*- coding: utf-8 -*-
"""execute 基准测试：验证 Python 子进程启动开销 vs 脚本实际执行时间"""
import sys, time, os, subprocess, json

sys.path.insert(0, r"D:\code_work_space\llm\nl2sql\src")
os.chdir(r"D:\code_work_space\llm\nl2sql")

# 1. 空 Python 进程启动开销
t0 = time.time()
for i in range(5):
    r = subprocess.run(
        [sys.executable, "-c", "pass"],
        capture_output=True, text=True, timeout=30,
    )
t1 = time.time()
avg_python_start = (t1 - t0) / 5
print(f"[1] Python 空进程启动（5次平均）: {avg_python_start:.3f}s/次")

# 2. 模拟 query_traces_by_id.py 的核心逻辑
# 测一下 langsmith 的 import 和 list_runs 开销
code = """
import os
os.environ['LANGSMITH_API_KEY'] = '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'
from langsmith import Client
c = Client()
runs = list(c.list_runs(project_name='nl2sql', filter='and(gte(start_time, "2026-08-21T00:00:00Z"))', limit=100))
print(f"total runs: {len(runs)}")
"""
t2 = time.time()
r = subprocess.run(
    [sys.executable, "-c", code],
    capture_output=True, text=True, timeout=120,
)
t3 = time.time()
print(f"\n[2] Python 启动 + LangSmith list_runs(limit=100): {t3-t2:.3f}s")
print(f"    stdout: {r.stdout[:200]}")
print(f"    stderr: {r.stderr[:200]}")

# 3. 复用进程 vs 新进程对比
# 先 import 一次，再测 list_runs
code2 = """
import os
os.environ['LANGSMITH_API_KEY'] = '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'
from langsmith import Client
c = Client()
for i in range(3):
    runs = list(c.list_runs(project_name='nl2sql', filter='and(gte(start_time, "2026-08-21T00:00:00Z"))', limit=100))
    print(f"run {i}: {len(runs)} runs")
"""
t4 = time.time()
r2 = subprocess.run(
    [sys.executable, "-c", code2],
    capture_output=True, text=True, timeout=120,
)
t5 = time.time()
print(f"\n[3] 单进程内 3 次 list_runs: {t5-t4:.3f}s")
print(f"    stdout: {r2.stdout[:300]}")