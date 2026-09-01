# 模型配置支持 max_tokens 和 temperature 参数

NL2SQL 模型配置增强方案（2026-08-22 **已实施**）。在已有的 `context_window` 基础上，新增 `max_tokens`（最大输出 token）和 `temperature`（温度参数）两个 per-model 配置项，前端可编辑、后端自动应用到 LLM 调用。

**目标**：用户在模型配置界面可配置每个模型的 `max_tokens` 和 `temperature`，`max_tokens` 自动获取真实值（已知模型映射表），`temperature` 默认 0，均支持用户覆盖，最终应用到 `create_model()` 的 `ChatModel` 构造中。

---

## 核心设计

### 优先级

对于 `max_tokens`：
1. 用户在模型配置中显式设置的值（最高优先）
2. `KNOWN_MODEL_MAX_TOKENS` 精确匹配
3. `_FUZZY_MAX_TOKENS_PATTERNS` 模糊匹配（模型 ID 包含关键词）
4. 返回 `None`（不设 `max_tokens`，由模型自行决定）

对于 `temperature`：
1. 用户在模型配置中显式设置的值
2. 默认 0.0

### 与 `context_window` 的差异

- `context_window` 有兜底默认值 120000（几乎所有模型都有上下文窗口）
- `max_tokens` 无兜底值，未知模型返回 `None`（不设限制，由模型自行决定），因为不同模型差异很大（4K~16K），硬编码默认值容易导致截断或浪费
- `temperature` 不需要自动推断（默认 0 在 `create_model` 中处理），API 返回时不自动填充

---

## 改动清单

### 后端

| 文件 | 改动 |
|---|---|
| `src/agent/llms/model.py` | 新增 `KNOWN_MODEL_MAX_TOKENS` 映射表 + `_FUZZY_MAX_TOKENS_PATTERNS` + `resolve_max_tokens()` 函数；`_resolve_llm_config` 返回值从 4 元组扩展为 6 元组；`create_model()` 中 `temperature` 和 `max_tokens` 从配置读取 |
| `src/agent/settings/model_config_store.py` | `_normalize_models` 新增 `temperature` 字段解析 |
| `src/api/model_config.py` | `_enrich_models_with_context_window` 扩展为同时自动填充 `max_tokens` |

### 前端

| 文件 | 改动 |
|---|---|
| `src/lib/modelConfigs.ts` | `ModelInfo` 接口新增 `temperature?: number` |
| `src/app/components/ModelConfigDialog.tsx` | `ModelDraft` 新增 `temperature`；`startEdit`/`save` 读写 temperature；展开行新增温度参数输入框（number 类型，0-2，步长 0.1，默认 0） |

### 临时脚本适配

| 文件 | 改动 |
|---|---|
| `src/agent/workspace/tmp/bench_create_model.py` | 解包从 4 元组改为 6 元组 |
| `src/agent/workspace/tmp/diagnose_thinking_toggle.py` | 解包从 4 元组改为 6 元组 |

---

## 已知模型映射表

### `KNOWN_MODEL_MAX_TOKENS`

```python
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
```

### `_FUZZY_MAX_TOKENS_PATTERNS`

```python
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
```

---

## `create_model()` 改动

```python
def create_model(enable_thinking=None, route=None, model_name=None):
    # 解包 6 元组
    api_key, base_url, resolved_model, cw_override, mt_override, temp_override = \
        _resolve_llm_config(route, model_name)

    # 解析参数
    context_window = resolve_context_window(resolved_model, cw_override)
    max_tokens = resolve_max_tokens(resolved_model, mt_override)
    temperature = temp_override if temp_override is not None else 0.0

    common_kwargs = dict(
        api_key=api_key, base_url=base_url, model=resolved_model,
        temperature=temperature,  # 不再是硬编码 0
        timeout=60, max_retries=3,
    )
    if max_tokens is not None:
        common_kwargs["max_tokens"] = max_tokens  # 有值才设
    # ...
```

---

## 前端 UI 变更

模型配置展开行新增温度参数输入框：

```
┌─ 模型 ID ──────┬─ 显示名称 ──────┬─ [>] [🗑] ─┐
│ 上下文窗口      │ 最大输出 token  │              │
│ [如 256K   ]   │ [如 32K    ]   │              │
│ 温度参数        │                │              │
│ [默认 0     ]   │                │              │
└────────────────┴────────────────┴──────────────┘
```

- 温度输入框：`type="number"`，`min=0`，`max=2`，`step=0.1`，`placeholder="默认 0"`
- 空值表示使用默认 0，保存时写入 `model_config.json`

---

## 验证

```
# resolve_max_tokens 精确匹配
python -c "from agent.llms.model import resolve_max_tokens; print(resolve_max_tokens('deepseek-chat'))"  # → 8192
python -c "from agent.llms.model import resolve_max_tokens; print(resolve_max_tokens('glm-4'))"          # → 4096
python -c "from agent.llms.model import resolve_max_tokens; print(resolve_max_tokens('gpt-4o'))"         # → 16384

# resolve_max_tokens 模糊匹配
python -c "from agent.llms.model import resolve_max_tokens; print(resolve_max_tokens('deepseek-v3-new'))"  # → 8192

# 未知模型返回 None
python -c "from agent.llms.model import resolve_max_tokens; print(resolve_max_tokens('unknown-model'))"    # → None

# 用户覆盖
python -c "from agent.llms.model import resolve_max_tokens; print(resolve_max_tokens('deepseek-chat', 4096))"  # → 4096

# resolve_context_window 不受影响
python -c "from agent.llms.model import resolve_context_window; print(resolve_context_window('deepseek-chat'))"  # → 128000
```

前端 `npm run build` ✅ 编译成功。

---

**Why**：原有 `model.py` 硬编码 `temperature=0` 且未设 `max_tokens`，用户无法按模型调整温度和输出长度。新增 per-model 配置后，用户可在模型配置界面自由调整，`max_tokens` 自动推断真实值减少手动填写。

**How to apply**：已全部实施。前端模型配置展开行新增温度输入框，后端 `create_model()` 自动从配置读取 `temperature` 和 `max_tokens`。