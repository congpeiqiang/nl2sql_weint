# -*- coding: utf-8 -*-
"""验证 create_model() 的实际耗时 —— 证实 ThinkingToggleMiddleware 的 9.3s 空白"""
import sys, time, os

# 设置模块搜索路径
sys.path.insert(0, r"D:\code_work_space\llm\nl2sql\src")
os.chdir(r"D:\code_work_space\llm\nl2sql")

t0 = time.time()
from agent.settings.model_config_store import get_store
t1 = time.time()
print(f"[1] import model_config_store:            {t1-t0:.3f}s")

_ = get_store()
t2 = time.time()
print(f"[2] get_store() 第一次:                   {t2-t1:.3f}s")

providers = get_store().get_all_decrypted()
t3 = time.time()
print(f"[3] get_all_decrypted():                  {t3-t2:.3f}s  ({[p.name for p in providers]})")

from agent.llms.model import create_model, _resolve_llm_config
t4 = time.time()
print(f"[4] import agent.llms.model:              {t4-t3:.3f}s")

api_key, base_url, model, cw_override, mt_override, temp_override = _resolve_llm_config()
t5 = time.time()
print(f"[5] _resolve_llm_config():                {t5-t4:.3f}s  (model={model})")

# 模拟 ThinkingToggleMiddleware._maybe_swap 的完整调用
for i in range(3):
    ts = time.time()
    m = create_model(enable_thinking=True)
    te = time.time()
    print(f"[6] create_model(enable_thinking=True) 第{i+1}次: {te-ts:.3f}s  (type={type(m).__name__})")

# 单独测 ChatDeepSeek 构造
from langchain_deepseek import ChatDeepSeek
t6 = time.time()
print(f"[7] import langchain_deepseek:            {t6-t5:.3f}s")

t7 = time.time()
m2 = ChatDeepSeek(
    api_key=api_key, base_url=base_url, model=model,
    temperature=0, timeout=60, max_retries=3,
    extra_body={"thinking": {"type": "enabled"}},
)
t8 = time.time()
print(f"[8] ChatDeepSeek 构造:                    {t8-t7:.3f}s")