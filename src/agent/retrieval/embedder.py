"""嵌入通道客户端（OpenAI 兼容 `/v1/embeddings`）。

## 契约（方案 §8.1 / 硬规则 R9：嵌入不得成为可用性依赖）

- **任何失败都返回 `None`，绝不抛异常**：连接失败 / 超时 / 模型缺失 / 维数不符 /
  响应体形状不对 —— 全部走 `None`，调用方据此降级纯 FTS（L1）。检索是质量增益，
  它挂了绝不许变成「问数挂了」。
- **硬超时**：默认 **2.5s**（`NL2SQL_EMBED_TIMEOUT`）。一次问数里嵌入只值几毫秒，
  宁可降级也不许把时间预算花在等嵌入上 —— 工具超时 300s 不是给嵌入用的。`.13` 上
  bge-m3 冷启实测 16.8s（容器重启后首调），必然超时降级，这是**设计内行为**。
- **维数 / 模型校验**：向量长度必须等于 `NL2SQL_EMBED_DIM`（默认 1024）、模型名必须
  等于 `NL2SQL_EMBED_MODEL`，否则返回 `None`。换模型会让维数变化，不校验则相似度
  **静默错乱**（不报错，只是排序变垃圾）。
- **只用 stdlib**：`urllib` 发请求，不引入新依赖；调用方在同步工具池里跑（P1-14）。

## 通道

默认打 `http://192.168.25.13:11435/v1`（团队 GPU 机上的**专用** `bgem3` 容器）。
**不要用 `:11434`**：那个实例 `OLLAMA_MAX_LOADED_MODELS=1`，bge-m3 会与 Langfuse
judge 的 27B 互驱；**也不要走 `:8100`**（另一个 vLLM 容器，实测 `/v1/embeddings` 404）。
该机**无外网**、且不是我们的 ⇒ 必须假设它会消失（DR 见方案 §8.1）。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

_log = logging.getLogger(__name__)

ENV_BASE_URL = "NL2SQL_EMBED_BASE_URL"
ENV_MODEL = "NL2SQL_EMBED_MODEL"
ENV_DIM = "NL2SQL_EMBED_DIM"
ENV_TIMEOUT = "NL2SQL_EMBED_TIMEOUT"

DEFAULT_BASE_URL = "http://192.168.25.13:11435/v1"
DEFAULT_MODEL = "bge-m3"
DEFAULT_DIM = 1024
DEFAULT_TIMEOUT_S = 2.5

# 单次请求的最大条数：索引期批量嵌入要限流分批（该机是共享主机，要能容忍对方重启）
MAX_BATCH = 32


def base_url() -> str:
    return (os.environ.get(ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL).rstrip("/")


def model_name() -> str:
    return os.environ.get(ENV_MODEL, "").strip() or DEFAULT_MODEL


def dim() -> int:
    try:
        return int(os.environ.get(ENV_DIM, "").strip() or DEFAULT_DIM)
    except ValueError:
        return DEFAULT_DIM


def timeout_s() -> float:
    try:
        value = float(os.environ.get(ENV_TIMEOUT, "").strip() or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def _post(path: str, payload: dict, timeout: float) -> dict | None:
    """POST 一次；任何异常都吞掉返回 None（含 HTTPError / URLError / 坏 JSON）。"""
    url = f"{base_url()}{path}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _log.warning("[embedder] %s HTTP %s（降级纯 FTS）", url, e.code)
    except Exception as e:  # noqa: BLE001 —— URLError / timeout / JSONDecodeError
        _log.warning("[embedder] %s 调用失败（降级纯 FTS）: %s", url, e)
    return None


def embed_texts(texts: list[str], *, timeout: float | None = None) -> list[list[float]] | None:
    """批量取向量。**失败返回 None**（调用方降级纯 FTS）。

    返回顺序与入参一一对应；维数不符或条数不符同样返回 None。
    """
    items = [str(t or "") for t in (texts or [])]
    if not items:
        return []
    if len(items) > MAX_BATCH:
        _log.warning("[embedder] 单批 %d 条超过上限 %d，请调用方分批", len(items), MAX_BATCH)

    data = _post(
        "/embeddings",
        {"model": model_name(), "input": items},
        timeout if timeout is not None else timeout_s(),
    )
    if not isinstance(data, dict):
        return None
    vectors = data.get("data")
    if not isinstance(vectors, list) or len(vectors) != len(items):
        _log.warning(
            "[embedder] 响应条数不符（期望 %d，得到 %s）⇒ 降级纯 FTS",
            len(items),
            len(vectors) if isinstance(vectors, list) else type(vectors).__name__,
        )
        return None

    # Ollama/OpenAI 都带 index；有就按 index 排，没有就按原序
    ordered: list[tuple[int, list[float]]] = []
    for pos, entry in enumerate(vectors):
        if not isinstance(entry, dict):
            return None
        vec = entry.get("embedding")
        if not isinstance(vec, list) or not vec:
            return None
        idx = entry.get("index")
        ordered.append((idx if isinstance(idx, int) else pos, [float(x) for x in vec]))
    ordered.sort(key=lambda pair: pair[0])
    out = [vec for _, vec in ordered]

    expected = dim()
    bad = [i for i, vec in enumerate(out) if len(vec) != expected]
    if bad:
        _log.warning(
            "[embedder] 维数不符（期望 %d，第 %s 条实际 %d）⇒ 降级纯 FTS；"
            "换过模型就要重建索引",
            expected,
            bad[:3],
            len(out[bad[0]]),
        )
        return None
    return out


def embed_one(text: str, *, timeout: float | None = None) -> list[float] | None:
    """单条查询向量（问数期：每问 1 次调用）。失败返回 None。"""
    out = embed_texts([text], timeout=timeout)
    if not out:
        return None
    return out[0]


def health(timeout: float = 2.0) -> dict:
    """探活（给 verify 脚本与运维用）：返回 {ok, model, dim, base_url, latency_ms, error}。"""
    import time

    started = time.monotonic()
    vecs = embed_texts(["探活"], timeout=timeout)
    latency = int((time.monotonic() - started) * 1000)
    ok = bool(vecs) and len(vecs[0]) == dim()
    return {
        "ok": ok,
        "model": model_name(),
        "dim": len(vecs[0]) if vecs else 0,
        "expected_dim": dim(),
        "base_url": base_url(),
        "latency_ms": latency,
        "error": "" if ok else "调用失败或维数不符（详见日志）",
    }
