"""Kimi（Moonshot）OpenAI 兼容适配：捕获 reasoning_content（深度思考）。

Kimi（kimi-k2.6 / kimi-k2.7-code 等）经阿里云百炼统一端点（OpenAI 兼容）提供，
思考型模型把深度思考内容放在 `reasoning_content` 字段，与 DeepSeek / Qwen / GLM 一致。
langchain_openai.ChatOpenAI 会丢弃该非标准字段，故这里继承 BaseChatOpenAI 并在两条
路径上补回 `additional_kwargs["reasoning_content"]`，供前端「深度思考」折叠块渲染：

  - 流式：`_convert_chunk_to_generation_chunk`（delta.reasoning_content）
  - 非流式：`_create_chat_result`（message.reasoning_content）

思考开关由 create_model 经 extra_body 透传（实测 2026-08-17，kimi-k2.6/k2.7-code）：
  - enable_thinking=true  → 返回 reasoning_content（102 字量级）
  - enable_thinking=false → kimi-k2.6 无 reasoning_content；kimi-k2.7-code 默认思考、
    false 关不彻底（仍返回 reasoning_content，属模型侧特性，非本类可控）
  - thinking.type=enabled → 亦生效（与 enable_thinking 等价，本工程统一用 enable_thinking）

做法参照 langchain_qwq.ChatQwen / agent.llms.glm.ChatGLM（同为 BaseChatOpenAI 子类）。
子类覆写，不改动 langchain 源码，随 langchain-openai 包升级安全。

注意：BaseChatOpenAI 必须从 `langchain_openai.chat_models.base` 导入，
`from langchain_openai import BaseChatOpenAI` 会 ImportError。
"""
from typing import Any, Dict, Optional, Type, Union

import openai
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai.chat_models.base import BaseChatOpenAI


class ChatKimi(BaseChatOpenAI):
    """Kimi（Moonshot）OpenAI-compatible 模型，捕获 reasoning_content。"""

    @property
    def _llm_type(self) -> str:
        return "chat-kimi"

    def _create_chat_result(
        self,
        response: Union[Dict, openai.BaseModel],
        generation_info: Optional[Dict] = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        if not result.generations:
            return result

        reasoning: Optional[str] = None
        # 非流式：从响应 message 读 reasoning_content
        choices = (
            getattr(response, "choices", None)
            or (response or {}).get("choices", [])
        )
        if choices:
            raw = choices[0]
            message = raw.get("message", {}) if isinstance(raw, dict) else raw.message
            if isinstance(message, dict):
                reasoning = message.get("reasoning_content")
                if not reasoning:
                    reasoning = message.get("reasoning")
            else:
                reasoning = getattr(message, "reasoning_content", None)
                if not reasoning:
                    model_extra = getattr(message, "model_extra", None) or {}
                    reasoning = model_extra.get("reasoning")

        if reasoning and result.generations[0].message.additional_kwargs is not None:
            result.generations[0].message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: Type,
        base_generation_info: Optional[Dict],
    ) -> Optional[ChatGenerationChunk]:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation_chunk and (choices := chunk.get("choices")):
            top = choices[0]
            if isinstance(generation_chunk.message, AIMessageChunk):
                delta = top.get("delta", {}) or {}
                if reasoning_content := delta.get("reasoning_content"):
                    generation_chunk.message.additional_kwargs["reasoning_content"] = (
                        reasoning_content
                    )
                # OpenRouter 等中转兜底
                elif reasoning := delta.get("reasoning"):
                    generation_chunk.message.additional_kwargs["reasoning_content"] = (
                        reasoning
                    )
        return generation_chunk
