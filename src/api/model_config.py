"""模型配置管理 API 路由（P1-8，挂进 custom_app.py，端口 2026）。

路由：
    GET    /api/model-configs              列表（api_key 脱敏）+ active
    POST   /api/model-configs              新增/更新（api_key 空 = 保留原值）
    GET    /api/model-configs/{name}       单条（脱敏）
    DELETE /api/model-configs/{name}       删除
    POST   /api/model-configs/{name}/activate  设为激活 provider
    POST   /api/model-configs/test         连通性探活 + 拉取模型列表（discoverModels）

探活说明：对 OpenAI-compatible 网关 `GET {base_url}/models`（Bearer key），
成功即认为接入点可用，并把返回的模型 id 列表带回前端供导入。
api_key 可缺省：带 name 时用已存储的 key（保存前测试则带明文 key）。
"""
from __future__ import annotations

import json
import logging
import urllib.request

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from agent.settings.model_config_store import ModelConfig, get_store, get_user_store
from agent.llms.model import (
    resolve_context_window,
    resolve_context_window_source,
    resolve_max_tokens,
    resolve_max_tokens_source,
)
from agent.utils.offload import offload_long
from api._common import json_response, parse_body, require_user

_logger = logging.getLogger(__name__)


def _ssrf_reject(base_url: str) -> str:
    """出站 SSRF 守卫（P3-8）：返回拒绝原因，空串 = 放行。

    这两个端点的 `base_url` **可以由请求体直接给**、服务端代发 HTTP 并把结果回显，
    而它们只 `require_user`（模型配置是按用户独立 store，限管理员会打断正常用法）
    ⇒ 必须在这里挡住「环回/元数据」这类**服务器能到、配置者本人到不了**的目标。
    口径与理由见 `agent/utils/net_guard.py`：**私网一律放行**（本仓模型网关就在私网）。
    """
    from agent.utils.net_guard import UnsafeOutboundURLError, check_outbound_url

    try:
        check_outbound_url(base_url, what="模型网关地址")
    except UnsafeOutboundURLError as e:
        _logger.warning("[model_config] 出站目标被拒: %s", e)
        return str(e)
    return ""


def _get_store(request: Request):
    """按当前登录用户获取独立 store。"""
    user = require_user(request)
    return get_user_store(user["user_id"])


def _probe_models(base_url: str, api_key: str, api_protocol: str = "", timeout: float = 10.0) -> tuple[bool, str, list[str]]:
    """探测模型列表：GET {base_url}/v1/models → fallback {base_url}/models → (ok, message, model_ids)。

    优先尝试 /v1/models（OpenAI 兼容标准路径），失败（非 JSON 或网络错误）时
    fallback 到 /models（部分网关将 API 挂在根路径）。

    api_protocol="anthropic" 时使用 x-api-key 认证头（而非 Bearer），
    且模型列表获取失败时返回空列表（Anthropic 兼容网关通常无 /models 端点）。
    """
    base = base_url.rstrip("/")
    urls = [f"{base}/v1/models", f"{base}/models"]
    if base.endswith("/v1"):
        # 用户已显式带 /v1，无需重复追加
        urls = [f"{base}/models"]

    is_anthropic = api_protocol == "anthropic"

    # P3-8 纵深防御：本函数是「拿 base_url 就去列表」的通用入口，最容易被后来的新端点复用。
    # 主防线在 handler（那边能回 400 + 明确文案）；这里再挡一次，形状与其它失败一致。
    strict_reason = _ssrf_reject(base_url)
    if strict_reason:
        return False, strict_reason, []

    def _do_probe(url: str) -> tuple[bool, str, list[str]]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; nl2sql/1.0)",
        }
        if api_key:
            if is_anthropic:
                headers["x-api-key"] = api_key
            else:
                headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        ids = []
        for item in body.get("data", []) if isinstance(body, dict) else []:
            mid = item.get("id") if isinstance(item, dict) else None
            if mid:
                ids.append(str(mid))
        ids.sort()
        return True, f"连接成功（{len(ids)} 个模型）", ids

    errors: list[str] = []
    for url in urls:
        try:
            return _do_probe(url)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")

    # Anthropic 兼容网关通常没有 /models 端点，连通性验证已通过（URL 可达），
    # 返回成功但空模型列表，提示用户手动添加模型。
    if is_anthropic:
        return True, "Anthropic 协议不支持自动获取模型列表，请手动添加模型。", []

    return False, f"连接失败: {'; '.join(errors)}", []


# ── 容量探活（context_window / max_tokens 真实值）──────────
# 标准 OpenAI /models 只返回模型 id；部分中转网关（one-api/new-api 等）会在
# 模型列表项或单查里附容量元数据。这里兼容常见字段命名，探测不到就回退静态表。

# 各容量字段的兼容别名（按优先级排列）
_CAPACITY_FIELD_GROUPS: dict[str, list[str]] = {
    "context_window": [
        "context_window", "contextWindow",
        "max_context_length", "maxContextLength",
        "max_input_tokens", "maxInputTokens",
        "max_model_len", "maxModelLen",
    ],
    "max_tokens": [
        "max_tokens", "maxTokens",
        "max_output_tokens", "maxOutputTokens",
        "max_completion_tokens", "maxCompletionTokens",
    ],
}


def _extract_capacity(model_obj: dict) -> dict:
    """从单个模型对象提取容量；无匹配返回 {"context_window": None, "max_tokens": None}。"""
    out: dict = {}
    for field, keys in _CAPACITY_FIELD_GROUPS.items():
        val = None
        for k in keys:
            v = model_obj.get(k)
            if isinstance(v, (int, float)) and v > 0:
                val = int(v)
                break
            if isinstance(v, str) and v.strip().isdigit() and int(v.strip()) > 0:
                val = int(v.strip())
                break
        out[field] = val
    return out


def _probe_model_capabilities(
    base_url: str, api_key: str, api_protocol: str = "", timeout: float = 10.0
) -> dict[str, dict]:
    """拉取网关模型列表并提取容量元数据：{model_id: {"context_window": int|None, "max_tokens": int|None}}。

    Anthropic 协议/网关不提供容量 → 返回空 dict（调用方回退静态表）。

    ⚠️ **P3-8 的出站守卫在 handler 层（`probe_capabilities` 的 `_ssrf_reject`）**：本函数下面
    那个 `except Exception: continue` 会把守卫抛的异常一起吞掉，所以**不要**把守卫只搬进这里 ——
    那样被拒的请求会静默退化成"探活没命中、回退静态值"，用户看不出被拒了。同理见 `_probe_single_model`。
    """
    base = base_url.rstrip("/")
    if api_protocol == "anthropic":
        return {}
    urls = [f"{base}/v1/models", f"{base}/models"]
    if base.endswith("/v1"):
        urls = [f"{base}/models"]

    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; nl2sql/1.0)",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
            out: dict[str, dict] = {}
            for item in body.get("data", []) if isinstance(body, dict) else []:
                if isinstance(item, dict) and item.get("id"):
                    out[str(item["id"])] = _extract_capacity(item)
            return out  # 拿到列表即返回（可能全空，说明网关不暴露容量）
        except Exception:  # noqa: BLE001
            continue
    return {}


def _probe_single_model(
    base_url: str, api_key: str, model_id: str, api_protocol: str = "", timeout: float = 6.0
) -> dict:
    """单查 GET /models/{id} 补容量（列表探测未命中时兜底）。

    ⚠️ 出站守卫在 handler 层，不在本函数里（原因见 `_probe_model_capabilities` docstring）。
    """
    if api_protocol == "anthropic":
        return {"context_window": None, "max_tokens": None}
    base = base_url.rstrip("/")
    candidates = [f"{base}/v1/models/{model_id}", f"{base}/models/{model_id}"]
    if base.endswith("/v1"):
        candidates = [f"{base}/models/{model_id}"]
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; nl2sql/1.0)",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
            obj = body.get("data", body) if isinstance(body, dict) else body
            if isinstance(obj, dict):
                cap = _extract_capacity(obj)
                if cap["context_window"] is not None or cap["max_tokens"] is not None:
                    return cap
        except Exception:  # noqa: BLE001
            continue
    return {"context_window": None, "max_tokens": None}


# ── 路由处理 ─────────────────────────────────────────────
def _enrich_models_with_context_window(providers: list[dict]) -> list[dict]:
    """为每个 provider 的 models 列表自动填充 context_window 和 max_tokens。

    对未设置 context_window 的模型，通过 resolve_context_window 自动推断；
    对未设置 max_tokens 的模型，通过 resolve_max_tokens 自动推断。
    用户显式设置的值不受影响。
    """
    for p in providers:
        for m in p.get("models", []):
            if isinstance(m, dict):
                model_id = m.get("id", "")
                # 用户未设置时自动推断
                if m.get("context_window") is None:
                    m["context_window"] = resolve_context_window(model_id)
                if m.get("max_tokens") is None:
                    resolved = resolve_max_tokens(model_id)
                    if resolved is not None:
                        m["max_tokens"] = resolved
    return providers


async def list_configs(request: Request):
    s = _get_store(request)
    providers = s.list_configs(masked=True)
    providers = _enrich_models_with_context_window(providers)
    return json_response({
        "providers": providers,
        "active": s.get_active(),
    })


async def get_config(request: Request):
    s = _get_store(request)
    name = request.path_params["name"]
    try:
        cfg = s.get(name)
    except KeyError:
        return json_response({"error": f"模型配置 '{name}' 不存在"}, status=404)
    out = cfg.to_mapping(masked=True)
    out["active"] = s.get_active() == name
    # 自动填充 context_window
    out["models"] = _enrich_models_with_context_window([{"models": out.get("models", [])}])[0]["models"]
    return json_response(out)


async def upsert_config(request: Request):
    s = _get_store(request)
    data = await parse_body(request)
    name = data.get("name")
    if not name:
        return json_response({"error": "name 必填"}, status=400)
    try:
        models = data.get("models") or []
        if isinstance(models, str):
            models = [m.strip() for m in models.split(",") if m.strip()]
        cfg = ModelConfig(
            name=str(name),
            base_url=str(data.get("base_url", "") or ""),
            api_key=str(data.get("api_key", "") or ""),
            models=models,  # 由 ModelConfig.__post_init__ 归一化（兼容 string/dict 列表）
            default_model=str(data.get("default_model", "") or ""),
            display_name=str(data.get("display_name", "") or ""),
            api_protocol=str(data.get("api_protocol", "") or ""),
        )
        s.upsert(cfg)
    except ValueError as e:
        return json_response({"error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001
        return json_response({"error": f"保存失败: {e}"}, status=500)
    return json_response({"ok": True, "name": name})


async def delete_config(request: Request):
    s = _get_store(request)
    name = request.path_params["name"]
    ok = s.delete(name)
    if not ok:
        return json_response({"error": f"模型配置 '{name}' 不存在"}, status=404)
    return json_response({"ok": True, "name": name})


async def activate_config(request: Request):
    s = _get_store(request)
    name = request.path_params["name"]
    try:
        s.set_active(name)
    except KeyError as e:
        return json_response({"error": str(e)}, status=404)
    return json_response({"ok": True, "active": name})


async def test_config(request: Request):
    s = _get_store(request)
    data = await parse_body(request)
    name = str(data.get("name", "") or "")
    base_url = str(data.get("base_url", "") or "")
    api_key = str(data.get("api_key", "") or "")
    api_protocol = str(data.get("api_protocol", "") or "")
    if not base_url and name:
        # 用已存储配置探活
        try:
            cfg = s.get(name)
            base_url, api_key = cfg.base_url, cfg.api_key
            api_protocol = api_protocol or (cfg.api_protocol or "")
        except KeyError:
            return json_response({"error": f"模型配置 '{name}' 不存在"}, status=404)
    if not base_url:
        return json_response({"error": "base_url 必填"}, status=400)
    # P3-8：出站前守卫（base_url 可能来自请求体，也可能来自"先存后测"的配置 —— 两条路径都要挡，
    # 只挡请求体等于留个两步绕过）。响应形状与"探活失败"一致，前端直接显示 message。
    reject = _ssrf_reject(base_url)
    if reject:
        return json_response({"ok": False, "message": reject, "models": []}, status=400)
    # P1-14：`_probe_models` 是同步 `urllib.request.urlopen`（timeout 10s，且会试多个
    # 候选 URL），走长任务池 —— 探活期间别人的 SSE 不该跟着一起等。
    ok, msg, model_ids = await offload_long(_probe_models, base_url, api_key, api_protocol)
    return json_response(
        {"ok": ok, "message": msg, "models": model_ids},
        status=200 if ok else 400,
    )


async def probe_capabilities(request: Request):
    """探活 context_window / max_tokens 真实值。

    Body: {name?, base_url?, api_key?, api_protocol?, models: [id, ...]}
    - `name` 缺 base_url 时用已存储配置；
    - 每个模型返回推荐容量（网关探活命中 > 静态表推断）与来源标记
      `context_window_source` / `max_tokens_source`（"probe" 探活 / "static" 静态推断 / "none" 未知）。
    - 前端「刷新真实容量」用它回填模型配置；用户手动配置始终优先（探活结果只回填、不覆盖）。
    """
    s = _get_store(request)
    data = await parse_body(request)
    name = str(data.get("name", "") or "")
    base_url = str(data.get("base_url", "") or "")
    api_key = str(data.get("api_key", "") or "")
    api_protocol = str(data.get("api_protocol", "") or "")
    raw_models = data.get("models") or []
    if isinstance(raw_models, str):
        raw_models = [x.strip() for x in raw_models.split(",") if x.strip()]
    model_ids = [str(x).strip() for x in raw_models if str(x).strip()]
    if not model_ids:
        return json_response({"error": "models 必填（要探测容量的模型 ID 列表）"}, status=400)
    if not base_url and name:
        try:
            cfg = s.get(name)
            base_url, api_key = cfg.base_url, cfg.api_key
            api_protocol = api_protocol or (cfg.api_protocol or "")
        except KeyError:
            return json_response({"error": f"模型配置 '{name}' 不存在"}, status=404)
    if not base_url:
        return json_response({"error": "base_url 必填"}, status=400)
    # P3-8：出站前守卫 —— **必须在这一层**（下面的 `_probe_model_capabilities`/`_probe_single_model`
    # 把所有异常都 `continue` 掉了，守卫放里面会被吞成"探活没命中、回退静态值"）
    reject = _ssrf_reject(base_url)
    if reject:
        return json_response({"error": reject}, status=400)

    # P1-14：这里最多会发 1 + N 次同步 HTTP（列表探测 10s + 每个未命中模型单查 6s），
    # 整段搬进长任务池的一次调用（循环也在里面，避免 N 次线程切换）。
    def _probe_all() -> list[dict]:
        probe = _probe_model_capabilities(base_url, api_key, api_protocol)
        out = []
        for mid in model_ids:
            cap = probe.get(mid) or {}
            cw = cap.get("context_window")
            mt = cap.get("max_tokens")
            if cw is None and mt is None:
                # 列表未命中容量 → 单查兜底
                single = _probe_single_model(base_url, api_key, mid, api_protocol)
                if cw is None:
                    cw = single.get("context_window")
                if mt is None:
                    mt = single.get("max_tokens")
            # 推荐值：探活 > 静态推断（resolve_* 为用户配置 > 已知表 > 默认）
            cw_final = cw if cw is not None else resolve_context_window(mid)
            mt_final = mt if mt is not None else resolve_max_tokens(mid)
            out.append({
                "id": mid,
                "context_window": cw_final,
                "max_tokens": mt_final,
                "context_window_source": "probe" if cw is not None else resolve_context_window_source(mid),
                "max_tokens_source": "probe" if mt is not None else resolve_max_tokens_source(mid),
            })
        return out

    out = await offload_long(_probe_all)
    return json_response({"ok": True, "message": f"已探测 {len(out)} 个模型", "models": out})


# ── 路由表（custom_app.py 聚合）──────────────────────────
routes: list[BaseRoute] = [
    Route("/api/model-configs", list_configs, methods=["GET"]),
    Route("/api/model-configs", upsert_config, methods=["POST"]),
    Route("/api/model-configs/{name}", get_config, methods=["GET"]),
    Route("/api/model-configs/{name}", delete_config, methods=["DELETE"]),
    Route("/api/model-configs/{name}/activate", activate_config, methods=["POST"]),
    Route("/api/model-configs/test", test_config, methods=["POST"]),
    Route("/api/model-configs/probe-capabilities", probe_capabilities, methods=["POST"]),
]
