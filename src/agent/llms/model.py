"""
@File    :  model.py
@Author  :  CongPeiQiang
@Time    :  2026/7/19 11:04
@Desc    :  模型工厂 — 自动适配 DeepSeek / Qwen (OpenAI-compatible)
"""
import logging
from langchain_core.language_models import ModelProfile

from agent.settings.setting import settings

logger = logging.getLogger(__name__)


def _detect_provider(model_name: str, base_url: str) -> str:
    """根据模型名和 base_url 判断供应商。"""
    model_lower = (model_name or "").lower()
    url_lower = (base_url or "").lower()

    if "deepseek" in model_lower or "deepseek" in url_lower:
        return "deepseek"
    if "qwen" in model_lower or "aliyuncs" in url_lower or "dashscope" in url_lower:
        return "qwen"
    # 默认走 OpenAI-compatible
    return "openai_compat"


def create_model(enable_thinking: bool | None = None):
    """根据 .env 配置自动创建对应模型实例。

    支持:
      - DeepSeek: 使用 ChatDeepSeek (langchain_deepseek)
      - Qwen / 其他 OpenAI-compatible: 使用 ChatOpenAI (langchain_openai)

    Args:
        enable_thinking: 是否开启模型思考（reasoning_content）。
            - None: 用供应商默认（当前两个模型默认思考开）；
            - True / False: 显式开启/关闭思考。由 ThinkingToggleMiddleware
              按前端 configurable.enable_thinking 每次调用传入。
    """
    provider = _detect_provider(settings.LLM_MODEL, settings.LLM_BASE_URL)
    logger.info(
        "[model] provider=%s, model=%s, base_url=%s, enable_thinking=%s",
        provider, settings.LLM_MODEL, settings.LLM_BASE_URL, enable_thinking,
    )

    common_kwargs = dict(
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        model=settings.LLM_MODEL,
        temperature=0,
        timeout=60,
        max_retries=3,
    )

    try:
        if provider == "deepseek":
            from langchain_deepseek import ChatDeepSeek
            # None 或 True → 思考开；False → 思考关（官方 thinking.type 开关，实测生效）
            thinking_type = "enabled" if enable_thinking is not False else "disabled"
            model = ChatDeepSeek(
                **common_kwargs,
                extra_body={"thinking": {"type": thinking_type}},
            )
        elif provider == "qwen":
            # Qwen：用 langchain_qwq.ChatQwen（BaseChatOpenAI 子类），
            # 在 _convert_chunk_to_generation_chunk 捕获 reasoning_content 到 additional_kwargs，
            # 供前端渲染"深度思考"折叠块。
            # enable_thinking：None → 不设 extra_body → Qwen3 默认思考；True/False 显式开/关（实测生效）。
            from langchain_qwq import ChatQwen
            model = ChatQwen(
                **common_kwargs,
                enable_thinking=enable_thinking,
            )
        else:
            # 其他 OpenAI-compatible 模型（doubao 等），无思考捕获
            from langchain_openai import ChatOpenAI
            model = ChatOpenAI(**common_kwargs)

        model.profile = ModelProfile(max_input_tokens=120000)
        return model

    except ImportError as e:
        logger.error("[model] 依赖缺失: %s", e)
        return None
    except Exception as e:
        logger.error("[model] 创建模型失败: %s", e)
        return None


# ── 模块级实例（供 main_agent.py / nl2sql_agent.py 导入）──
deepseek_model = create_model()
