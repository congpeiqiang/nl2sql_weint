# GLM（智谱 BigModel）深度思考接入方案

## 目标

让后端在识别到 GLM（智谱）模型时，捕获其「深度思考」内容（`reasoning_content`），
与 DeepSeek / Qwen 一样经 `additional_kwargs.reasoning_content` 传给前端，渲染成可折叠的
「深度思考」块。

## 现状与根因

- 当前 `_detect_provider` 只识别 `deepseek` / `qwen`，其余一律 `openai_compat` → 走 `ChatOpenAI`。
- `langchain_openai.ChatOpenAI` 会丢弃非标准的 `reasoning_content` 字段（其 docstring 明示），
  所以 GLM 的思考内容被吞掉，前端拿不到。
- GLM 的 OpenAI 兼容接口（`https://open.bigmodel.cn/api/paas/v4`）对思考型模型
  （glm-4.5 / glm-4.6 / glm-4.5-air 等）同样用 `reasoning_content` 返回思考内容，字段名与
  DeepSeek / Qwen 一致。

## 方案

写一个 `ChatGLM(BaseChatOpenAI)` 子类，仅覆写两条捕获点，把 `reasoning_content` 补回
`additional_kwargs`。这是**子类覆写**，不改动 langchain 源码，随 langchain-openai 包升级安全。

- 流式：`_convert_chunk_to_generation_chunk` 读 `chunk.choices[0].delta.reasoning_content`
- 非流式：`_create_chat_result` 读 `response.choices[0].message.reasoning_content`

参照 `langchain_qwq.ChatQwen` 的捕获方式（同是 `BaseChatOpenAI` 子类）。

### 关键点

- **导入路径必须是** `from langchain_openai.chat_models.base import BaseChatOpenAI`
  （`from langchain_openai import BaseChatOpenAI` 会 `ImportError`）。
- **思考开关**：GLM 与 DeepSeek 同用 `thinking.type`（`enabled`/`disabled`，默认 enabled），
  经 `extra_body={"thinking": {"type": ...}}` 透传即可让前端开关生效。

## 改动

### 1. 新增 `src/agent/llms/glm.py`

`ChatGLM(BaseChatOpenAI)` 子类，覆写 `_llm_type` / `_convert_chunk_to_generation_chunk` /
`_create_chat_result`。

### 2. 修改 `src/agent/llms/model.py`

- `_detect_provider`：在 qwen 分支后新增
  `if "glm" in model_lower or "bigmodel" in url_lower or "zhipu" in url_lower: return "glm"`。
- `create_model`：在 qwen 分支后新增 glm 分支，`ChatGLM(**common_kwargs, extra_body={"thinking": {"type": thinking_type}})`，
  其中 `thinking_type = "enabled" if enable_thinking is not False else "disabled"`（与 DeepSeek 分支一致）。

## 验证

1. `./.venv/Scripts/python.exe -m py_compile src/agent/llms/glm.py src/agent/llms/model.py`
2. 冒烟：`from agent.llms.glm import ChatGLM` + 用真实 GLM key/base_url/model 实例化。
3. 重启 2026 后，配置 GLM provider，发问观察思考块渲染（需用户操作，破坏性重启需确认）。

## 已知边界

- 只捕获主 agent 最终回复的思考内容；子 agent 独立 run 不进入主流（沿用既有架构局限）。
- GLM 的思考开关经 `thinking.type` 透传（与 DeepSeek 一致）；GLM-4.5/4.6 为交错思考，
  每回合结束思考内容会清除（这是 GLM 自身行为，非本接入问题）。
