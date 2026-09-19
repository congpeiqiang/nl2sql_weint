# 模型配置探活：Anthropic 协议兼容方案

> 2026-08-19

## 背景

新增阿里云百炼 Anthropic 兼容端点（`https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic`）时，点击"获取可用模型"报 404。

## 根因

1. **认证方式不同**：百炼 Anthropic 通道使用 `x-api-key` 头（而非 OpenAI 兼容的 `Authorization: Bearer`），后端 `_probe_models` 硬编码了 `Bearer` 认证
2. **无 /models 端点**：百炼 Anthropic 兼容端点不提供 `/models` 接口（返回 404），但 `/v1/messages` 实际可用（配额耗尽时返回 429，说明路径可达）

## 解决方案

### 方案 B（已实施）：代码侧自动适配

修改 `_probe_models`，根据 `api_protocol` 自动切换认证方式和容错策略：

**核心改动：**

1. `_probe_models` 新增 `api_protocol` 参数
2. `api_protocol="anthropic"` 时：
   - 认证头改为 `x-api-key`（而非 `Bearer`）
   - 所有 `/models` URL 探测失败时，返回 `ok=true` + 空模型列表 + 提示手动添加（不报错）
3. `test_config` 端点从请求体提取 `api_protocol` 并传递
4. 前端 `testModelConfig` 和 `fetchModels` 携带 `api_protocol`

### 方案 A（备选，未采用）：前端 URL 修正

在前端保存时自动补 `/v1` 后缀。但无法解决认证头问题和无 models 端点的问题，治标不治本。

## 改动文件

| 文件 | 改动 |
|------|------|
| `src/api/model_config.py` | `_probe_models` 新增 `api_protocol` 参数，Anthropic 协议用 `x-api-key` 认证、失败时返回空列表不报错；`test_config` 提取并传递 `api_protocol` |
| `src/lib/modelConfigs.ts`（前端） | `testModelConfig` 签名新增 `api_protocol` 参数 |
| `src/app/components/ModelConfigDialog.tsx`（前端） | `fetchModels` 探活 payload 携带 `api_protocol` |

## 关键代码

```python
def _probe_models(base_url: str, api_key: str, api_protocol: str = "", timeout: float = 10.0):
    is_anthropic = api_protocol == "anthropic"

    def _do_probe(url: str):
        headers = {"Accept": "application/json", ...}
        if api_key:
            if is_anthropic:
                headers["x-api-key"] = api_key       # Anthropic 用 x-api-key
            else:
                headers["Authorization"] = f"Bearer {api_key}"  # OpenAI 用 Bearer
        ...

    # 优先 /v1/models，fallback /models
    for url in [f"{base}/v1/models", f"{base}/models"]:
        try: return _do_probe(url)
        except: errors.append(...)

    # Anthropic 网关无 /models 端点 → 返回 ok 但提示手动添加
    if is_anthropic:
        return True, "Anthropic 协议不支持自动获取模型列表，请手动添加模型。", []

    return False, f"连接失败: {'; '.join(errors)}", []
```

## 用户体验

- 配置 Anthropic 协议端点时，点击"获取可用模型"不会报错
- 提示"Anthropic 协议不支持自动获取模型列表，请手动添加模型"
- 用户手动输入模型 ID（如 `claude-sonnet-4-20250514`）后，正常保存使用

## 补充：百炼两种网关的区别

`https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` 选择 Anthropic 协议也能联通，因为它是和 `apps/anthropic` **两个不同的网关**：

| | `compatible-mode/v1` | `apps/anthropic` |
|---|---|---|
| 协议 | OpenAI 兼容（内部转译） | Anthropic 原生 |
| 认证头 | `Authorization: Bearer` | `x-api-key` |
| `/models` 端点 | ✅ 有 | ❌ 404 |
| 请求路径 | `/v1/chat/completions` | `/v1/messages` |

`compatible-mode/v1` 是百炼的 **OpenAI 兼容模式**网关，内部做了协议转换——收到 OpenAI 格式请求（Bearer 认证、`/v1/chat/completions`）后转成 Anthropic 格式发给 Claude。所以它用 `Bearer` 认证、有 `/v1/models` 端点，和 `_probe_models` 的默认行为完全匹配，探活自然成功。

而前端 `api_protocol` 字段仅作为元数据标签，**实际 HTTP 协议由 `base_url` 决定**。走 `compatible-mode/v1` 时实际发的是 OpenAI 兼容请求，`api_protocol` 选什么不影响通信。