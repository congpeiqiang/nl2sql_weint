"""
@File    :  model.py
@Author  :  CongPeiQiang
@Time    :  2026/7/19 11:04
@Desc    :  模型工厂 — 自动适配 DeepSeek / Qwen / GLM (OpenAI-compatible)
"""
import logging
from langchain_core.language_models import ModelProfile

logger = logging.getLogger(__name__)


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


def _resolve_llm_config(route: str | None = None, model_name: str | None = None) -> tuple[str, str, str]:
    """解析本次调用使用的 (api_key, base_url, model)。

    优先读运行时模型配置 store（P1-8，model_config.json，前端可 CRUD、免重启生效）：
      - route 指定 provider → 用之；
      - 否则 active provider → 否则第一个 provider；
      - model_name（前端显式选模型）在该 provider 模型列表里 → 覆盖默认模型；
    store 无可用配置（空/缺字段/异常）时返回空串三元组（不调用 LLM）。
    不再回退 .env 的 LLM_* —— 模型配置唯一来源是前端 CRUD 的 model_config.json。
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
            if cfg.api_key and cfg.base_url and resolved:
                return cfg.api_key, cfg.base_url, resolved
            logger.warning("[model] provider '%s' 配置不完整，无可用模型", cfg.name)
    except Exception as e:  # noqa: BLE001
        logger.warning("[model] 读取模型配置 store 失败，无可用模型: %s", e)
    return "", "", ""


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
    api_key, base_url, resolved_model = _resolve_llm_config(route, model_name)
    if not (api_key and base_url and resolved_model):
        logger.error(
            "[model] 无可用模型配置（api_key/base_url/model 缺失），拒绝创建模型；"
            "请在前端配置模型。route=%s, model_name=%s", route, model_name,
        )
        return None
    provider = _detect_provider(resolved_model, base_url)
    logger.info(
        "[model] provider=%s, model=%s, base_url=%s, enable_thinking=%s, route=%s, model_name=%s",
        provider, resolved_model, base_url, enable_thinking, route, model_name,
    )

    common_kwargs = dict(
        api_key=api_key,
        base_url=base_url,
        model=resolved_model,
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
