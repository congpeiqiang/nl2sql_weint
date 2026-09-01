# -*- coding: utf-8 -*-
"""诊断 ThinkingToggleMiddleware._maybe_swap 的耗时瓶颈"""
import time
import os
os.environ['LANGSMITH_API_KEY'] = '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'

t0 = time.time()
from agent.settings.model_config_store import get_store
t1 = time.time()
print(f"import model_config_store: {t1-t0:.3f}s")

providers = get_store().get_all_decrypted()
t2 = time.time()
print(f"get_all_decrypted: {t2-t1:.3f}s")
print(f"providers: {[p.name for p in providers]}")

from agent.llms.model import _resolve_llm_config
api_key, base_url, model, cw_override, mt_override, temp_override = _resolve_llm_config()
t3 = time.time()
print(f"_resolve_llm_config: {t3-t2:.3f}s")
print(f"model={model}, base_url={base_url}")

from langchain_deepseek import ChatDeepSeek
t4 = time.time()
print(f"import ChatDeepSeek: {t4-t3:.3f}s")

m = ChatDeepSeek(
    api_key=api_key,
    base_url=base_url,
    model=model,
    temperature=0,
    timeout=60,
    max_retries=3,
    extra_body={"thinking": {"type": "enabled"}},
)
t5 = time.time()
print(f"ChatDeepSeek constructor: {t5-t4:.3f}s")
print(f"Total: {t5-t0:.3f}s")
