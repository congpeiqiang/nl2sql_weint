"""
@File    :  model.py
@Author  :  CongPeiQiang
@Time    :  2026/7/19 11:04
@Desc    :  模型工厂 — 自动适配 DeepSeek / Qwen / GLM (OpenAI-compatible)
"""
import logging
from langchain_core.language_models import ModelProfile

logger = logging.getLogger(__name__)

# 已知模型的上下文窗口（token 数）。用于自动填充 model_config 的 context_window，
# 以及 create_model 的 ModelProfile。用户可在模型配置中覆盖这些值。
# 数据来源：各模型官方文档/API 文档。
KNOWN_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # DeepSeek 系列
    "deepseek-chat": 128_000,       # DeepSeek V3
    "deepseek-reasoner": 128_000,   # DeepSeek R1
    "deepseek-v3": 128_000,
    "deepseek-r1": 128_000,
    # Qwen 系列（通义千问）
    "qwen3-235b-a22b": 131_072,
    "qwen3-32b": 131_072,
    "qwen3-235b-a22b-thinking": 131_072,
    "qwen-max": 32_768,
    "qwen-plus": 131_072,
    "qwen-turbo": 1_000_000,
    "qwen3-30b-a3b": 131_072,
    "qwen3-14b": 131_072,
    "qwen3-8b": 131_072,
    "qwen3-4b": 131_072,
    "qwen3-1.7b": 131_072,
    "qwen3-0.6b": 131_072,
    # GLM 系列（智谱）
    "glm-4": 128_000,
    "glm-4-plus": 128_000,
    "glm-4-flash": 128_000,
    "glm-4-air": 128_000,
    "glm-4-long": 1_000_000,
    "glm-4-airx": 128_000,
    "glm-4-flashx": 128_000,
    # Kimi 系列（月之暗面）
    "kimi-k2": 128_000,
    "kimi-k2.6": 128_000,
    "kimi-k2.7-code": 128_000,
    "kimi-moonshot-v1": 128_000,
    # OpenAI 系列
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    # Anthropic 系列
    "claude-3-5-sonnet": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-haiku": 200_000,
    "claude-3-sonnet": 200_000,
    # 豆包系列
    "doubao-pro-32k": 32_768,
    "doubao-pro-128k": 128_000,
    "doubao-lite-32k": 32_768,
    "doubao-lite-128k": 128_000,
}

# 模糊匹配：模型 ID 包含这些关键词时，匹配对应窗口
_FUZZY_WINDOW_PATTERNS: list[tuple[str, int]] = [
    ("deepseek", 128_000),
    ("qwen", 131_072),
    ("glm", 128_000),
    ("kimi", 128_000),
    ("moonshot", 128_000),
    ("gpt-4o", 128_000),
    ("gpt-4", 8_192),
    ("gpt-3.5", 16_385),
    ("claude", 200_000),
    ("doubao-pro", 128_000),
    ("doubao-lite", 128_000),
    ("doubao", 128_000),
]

# 已知模型的最大输出 token 数。用于自动填充 model_config 的 max_tokens。
# 数据来源：各模型官方文档/API 文档。
KNOWN_MODEL_MAX_TOKENS: dict[str, int] = {
    # DeepSeek 系列
    "deepseek-chat": 8_192,
    "deepseek-reasoner": 8_192,
    "deepseek-v3": 8_192,
    "deepseek-r1": 8_192,
    # Qwen 系列
    "qwen3-235b-a22b": 8_192,
    "qwen3-32b": 8_192,
    "qwen3-235b-a22b-thinking": 8_192,
    "qwen-max": 8_192,
    "qwen-plus": 8_192,
    "qwen-turbo": 8_192,
    "qwen3-30b-a3b": 8_192,
    "qwen3-14b": 8_192,
    "qwen3-8b": 8_192,
    "qwen3-4b": 8_192,
    "qwen3-1.7b": 8_192,
    "qwen3-0.6b": 8_192,
    # GLM 系列
    "glm-4": 4_096,
    "glm-4-plus": 4_096,
    "glm-4-flash": 4_096,
    "glm-4-air": 4_096,
    "glm-4-long": 4_096,
    "glm-4-airx": 4_096,
    "glm-4-flashx": 4_096,
    # Kimi 系列
    "kimi-k2": 8_192,
    "kimi-k2.6": 8_192,
    "kimi-k2.7-code": 8_192,
    "kimi-moonshot-v1": 8_192,
    # OpenAI 系列
    "gpt-4o": 16_384,
    "gpt-4o-mini": 16_384,
    "gpt-4-turbo": 4_096,
    "gpt-4": 4_096,
    "gpt-3.5-turbo": 4_096,
    # Anthropic 系列
    "claude-3-5-sonnet": 8_192,
    "claude-3-opus": 4_096,
    "claude-3-haiku": 4_096,
    "claude-3-sonnet": 4_096,
    # 豆包系列
    "doubao-pro-32k": 4_096,
    "doubao-pro-128k": 4_096,
    "doubao-lite-32k": 4_096,
    "doubao-lite-128k": 4_096,
}

# 模糊匹配：模型 ID 包含这些关键词时，匹配对应 max_tokens
_FUZZY_MAX_TOKENS_PATTERNS: list[tuple[str, int]] = [
    ("deepseek", 8_192),
    ("qwen", 8_192),
    ("glm", 4_096),
    ("kimi", 8_192),
    ("moonshot", 8_192),
    ("gpt-4o", 16_384),
    ("gpt-4", 4_096),
    ("gpt-3.5", 4_096),
    ("claude", 8_192),
    ("doubao", 4_096),
]


def resolve_max_tokens(model_id: str, user_override: int | None = None) -> int | None:
    """解析模型的最大输出 token 数。

    优先级：
    1. user_override（用户在模型配置中显式设置的值）
    2. KNOWN_MODEL_MAX_TOKENS 精确匹配
    3. _FUZZY_MAX_TOKENS_PATTERNS 模糊匹配（模型 ID 包含关键词）
    4. 返回 None（不设 max_tokens，由模型自行决定）

    Args:
        model_id: 模型 ID（如 "deepseek-chat"）
        user_override: 用户在模型配置中覆盖的值（None 表示未设置）

    Returns:
        最大输出 token 数，None 表示不设置
    """
    if user_override is not None and user_override > 0:
        return user_override

    model_lower = (model_id or "").lower()

    # 精确匹配
    if model_lower in KNOWN_MODEL_MAX_TOKENS:
        return KNOWN_MODEL_MAX_TOKENS[model_lower]

    # 模糊匹配
    for keyword, mt in _FUZZY_MAX_TOKENS_PATTERNS:
        if keyword in model_lower:
            return mt

    return None


def resolve_context_window(model_id: str, user_override: int | None = None) -> int:
    """解析模型的上下文窗口大小。

    优先级：
    1. user_override（用户在模型配置中显式设置的值）
    2. KNOWN_MODEL_CONTEXT_WINDOWS 精确匹配
    3. _FUZZY_WINDOW_PATTERNS 模糊匹配（模型 ID 包含关键词）
    4. 默认 120000

    Args:
        model_id: 模型 ID（如 "deepseek-chat"）
        user_override: 用户在模型配置中覆盖的值（None 表示未设置）

    Returns:
        上下文窗口 token 数
    """
    if user_override is not None and user_override > 0:
        return user_override

    model_lower = (model_id or "").lower()

    # 精确匹配
    if model_lower in KNOWN_MODEL_CONTEXT_WINDOWS:
        return KNOWN_MODEL_CONTEXT_WINDOWS[model_lower]

    # 模糊匹配
    for keyword, window in _FUZZY_WINDOW_PATTERNS:
        if keyword in model_lower:
            return window

    return 120_000


def resolve_context_window_source(model_id: str) -> str:
    """判断 context_window 静态推断来源：'known'（已知表/模糊关键词命中）| 'default'（通用默认 120000）。

    供模型配置探活接口标注来源；只判断来源，数值仍用 resolve_context_window。
    """
    model_lower = (model_id or "").lower()
    if model_lower in KNOWN_MODEL_CONTEXT_WINDOWS:
        return "known"
    for keyword, _ in _FUZZY_WINDOW_PATTERNS:
        if keyword in model_lower:
            return "known"
    return "default"


def resolve_max_tokens_source(model_id: str) -> str:
    """判断 max_tokens 静态推断来源：'known'（已知表/模糊关键词命中）| 'none'（未知，不设置）。

    供模型配置探活接口标注来源；只判断来源，数值仍用 resolve_max_tokens。
    """
    model_lower = (model_id or "").lower()
    if model_lower in KNOWN_MODEL_MAX_TOKENS:
        return "known"
    for keyword, _ in _FUZZY_MAX_TOKENS_PATTERNS:
        if keyword in model_lower:
            return "known"
    return "none"


def _detect_provider(model_name: str, base_url: str) -> str:
    """根据模型名和 base_url 判断供应商。"""
    model_lower = (model_name or "").lower()
    url_lower = (base_url or "").lower()

    if "deepseek" in model_lower or "deepseek" in url_lower:
        return "deepseek"
    if "kimi" in model_lower or "moonshot" in url_lower:
        return "kimi"
    if "qwen" in model_lower or "aliyuncs" in url_lower or "dashscope" in url_lower:
        return "qwen"
    if "glm" in model_lower or "bigmodel" in url_lower or "zhipu" in url_lower:
        return "glm"
    # 默认走 OpenAI-compatible
    return "openai_compat"


def _first_model_id(cfg) -> str:
    """从逐模型对象列表取第一个非空模型 id（兼容旧 string 列表）。"""
    for m in cfg.models or []:
        if isinstance(m, dict):
            mid = m.get("id")
            if mid:
                return str(mid)
        elif isinstance(m, str) and m:
            return m
    return ""


def _resolve_llm_config(route: str | None = None, model_name: str | None = None) -> tuple[str, str, str, int | None, int | None, float | None]:
    """解析本次调用使用的 (api_key, base_url, model, context_window_override, max_tokens_override, temperature_override)。

    优先读运行时模型配置 store（P1-8，model_config.json，前端可 CRUD、免重启生效）：
      - route 指定 provider → 用之；
      - 否则 active provider → 否则第一个 provider；
      - model_name（前端显式选模型）在该 provider 模型列表里 → 覆盖默认模型；
    store 无可用配置（空/缺字段/异常）时返回空串三元组（不调用 LLM）。
    不再回退 .env 的 LLM_* —— 模型配置唯一来源是前端 CRUD 的 model_config.json。

    Returns:
        (api_key, base_url, model, context_window_override, max_tokens_override, temperature_override)
        context_window_override: 模型配置中用户设置的值（None 表示未设置）
        max_tokens_override: 模型配置中用户设置的最大输出 token（None 表示未设置）
        temperature_override: 模型配置中用户设置的温度参数（None 表示未设置）
    """
    try:
        from agent.settings.model_config_store import get_store

        providers = get_store().get_all_decrypted()
        if providers:
            cfg = None
            if route:
                cfg = next((p for p in providers if p.name == route), None)
                if cfg is None:
                    logger.warning("[model] route '%s' 不存在，回退 active/默认", route)
            if cfg is None:
                active = get_store().get_active()
                cfg = next((p for p in providers if p.name == active), None) or providers[0]
            resolved = cfg.default_model or _first_model_id(cfg)
            if model_name:
                ids = {m.get("id") for m in cfg.models if isinstance(m, dict) and m.get("id")}
                if not ids or model_name in ids:
                    resolved = model_name
                else:
                    logger.warning(
                        "[model] model '%s' 不在 provider '%s' 模型列表，回退默认 %s",
                        model_name, cfg.name, resolved,
                    )
            # 查找当前模型在配置中的覆盖值
            cw_override = None
            mt_override = None
            temp_override = None
            for m in cfg.models:
                if isinstance(m, dict) and m.get("id") == resolved:
                    cw = m.get("context_window")
                    if cw is not None:
                        try:
                            cw_override = int(cw)
                        except (TypeError, ValueError):
                            pass
                    mt = m.get("max_tokens")
                    if mt is not None:
                        try:
                            mt_override = int(mt)
                        except (TypeError, ValueError):
                            pass
                    temp = m.get("temperature")
                    if temp is not None:
                        try:
                            temp_override = float(temp)
                        except (TypeError, ValueError):
                            pass
                    break
            if cfg.api_key and cfg.base_url and resolved:
                return cfg.api_key, cfg.base_url, resolved, cw_override, mt_override, temp_override
            logger.warning("[model] provider '%s' 配置不完整，无可用模型", cfg.name)
    except Exception as e:  # noqa: BLE001
        logger.warning("[model] 读取模型配置 store 失败，无可用模型: %s", e)
    return "", "", "", None, None, None


def create_model(
    enable_thinking: bool | None = None,
    route: str | None = None,
    model_name: str | None = None,
):
    """按模型配置 store 自动创建对应模型实例（无有效配置时返回 None）。

    支持:
      - DeepSeek: 使用 ChatDeepSeek (langchain_deepseek)
      - Qwen: 使用 langchain_qwq.ChatQwen（捕获 reasoning_content）
      - GLM: 使用 agent.llms.glm.ChatGLM（捕获 reasoning_content）
      - 其他 OpenAI-compatible: 使用 ChatOpenAI (langchain_openai)

    Args:
        enable_thinking: 是否开启模型思考（reasoning_content）。
            - None: 用供应商默认（当前两个模型默认思考开）；
            - True / False: 显式开启/关闭思考。由 ThinkingToggleMiddleware
              按前端 configurable.enable_thinking 每次调用传入。
        route: 模型配置 store 中的 provider 名（P1-9 composer 模型选择经
            configurable.llm_route 传入）；None 用 active provider。
        model_name: 显式指定的模型 id（P1-9 逐模型选择经 configurable.llm_model
            传入），覆盖 provider 的 default_model；None 用 default_model/第一个。
    """
    api_key, base_url, resolved_model, cw_override, mt_override, temp_override = _resolve_llm_config(route, model_name)
    if not (api_key and base_url and resolved_model):
        logger.error(
            "[model] 无可用模型配置（api_key/base_url/model 缺失），拒绝创建模型；"
            "请在前端配置模型。route=%s, model_name=%s", route, model_name,
        )
        return None
    provider = _detect_provider(resolved_model, base_url)
    # 解析上下文窗口：用户覆盖 > 已知模型匹配 > 默认 120000
    context_window = resolve_context_window(resolved_model, cw_override)
    # 解析最大输出 token：用户覆盖 > 已知模型匹配 > None（不设限制）
    max_tokens = resolve_max_tokens(resolved_model, mt_override)
    # 解析温度：用户配置 > 默认 0
    temperature = temp_override if temp_override is not None else 0.0
    logger.info(
        "[model] provider=%s, model=%s, base_url=%s, enable_thinking=%s, "
        "context_window=%d, max_tokens=%s, temperature=%s, route=%s, model_name=%s",
        provider, resolved_model, base_url, enable_thinking,
        context_window, max_tokens, temperature, route, model_name,
    )

    common_kwargs = dict(
        api_key=api_key,
        base_url=base_url,
        model=resolved_model,
        temperature=temperature,
        timeout=60,
        max_retries=3,
    )
    if max_tokens is not None:
        common_kwargs["max_tokens"] = max_tokens

    try:
        if provider == "deepseek":
            from langchain_deepseek import ChatDeepSeek
            # None 或 True → 思考开；False → 思考关（官方 thinking.type 开关，实测生效）
            thinking_type = "enabled" if enable_thinking is not False else "disabled"
            model = ChatDeepSeek(
                **common_kwargs,
                extra_body={"thinking": {"type": thinking_type}},
            )
        elif provider == "kimi":
            # Kimi（Moonshot，经阿里云百炼 OpenAI 兼容端点）：思考开关 enable_thinking，
            # reasoning_content 捕获（实测 2026-08-17 对 kimi-k2.6/k2.7-code 生效）。
            # enable_thinking：None → 不设 extra_body → 模型默认；True/False → 显式开/关。
            from agent.llms.kimi import ChatKimi
            extra_body = None if enable_thinking is None else {"enable_thinking": enable_thinking}
            model = ChatKimi(**common_kwargs, extra_body=extra_body)
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
        elif provider == "glm":
            # GLM：用自写 ChatGLM（BaseChatOpenAI 子类），在流式/非流式两条路径
            # 捕获 reasoning_content 到 additional_kwargs，供前端渲染"深度思考"折叠块。
            # 思考开关：GLM 与 DeepSeek 同用 thinking.type 参数（enabled/disabled），经 extra_body 透传。
            thinking_type = "enabled" if enable_thinking is not False else "disabled"
            from agent.llms.glm import ChatGLM
            model = ChatGLM(
                **common_kwargs,
                extra_body={"thinking": {"type": thinking_type}},
            )
        else:
            # 其他 OpenAI-compatible 模型（doubao 等），无思考捕获
            from langchain_openai import ChatOpenAI
            model = ChatOpenAI(**common_kwargs)

        model.profile = ModelProfile(max_input_tokens=context_window)
        return model

    except ImportError as e:
        logger.error("[model] 依赖缺失: %s", e)
        return None
    except Exception as e:
        logger.error("[model] 创建模型失败: %s", e)
        return None


# ── 模块级实例（供 main_agent.py / nl2sql_agent.py 导入）──
deepseek_model = create_model()
