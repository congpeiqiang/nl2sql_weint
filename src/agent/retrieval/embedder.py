"""嵌入通道客户端（OpenAI 兼容 `/v1/embeddings`）。

## 契约（方案 §8.1 / 硬规则 R9：嵌入不得成为可用性依赖）

- **任何失败都返回 `None`，绝不抛异常**：连接失败 / 超时 / 模型缺失 / 维数不符 /
  响应体形状不对 —— 全部走 `None`，调用方据此降级纯 FTS（L1）。检索是质量增益，
  它挂了绝不许变成「问数挂了」。
- **硬超时**：**查询期 4s**（`NL2SQL_EMBED_QUERY_TIMEOUT`）／**索引期单批 2.5s**
  （`NL2SQL_EMBED_TIMEOUT`）。一次问数里嵌入只值几毫秒，宁可降级也不许把时间预算花在
  等嵌入上 —— 工具超时 300s 不是给嵌入用的。`.13` 上 bge-m3 冷启实测 16.8s（容器重启后
  首调），必然超时降级，这是**设计内行为**。
  4s 这个数是**量出来的**（2026-09-29 从生产容器打 230 次）：顺序 p99 115ms、4 并发 p99
  234ms、32 条批 486ms ⇒ 4s 仍是正常尾延迟的 17 倍。
- **重试 1 次**（`NL2SQL_EMBED_RETRIES`）：2026-09-29 生产实证两次查询期 `timed out`
  ——正常 73ms 的通道**离散停顿 ≥2.5s**（宿主/网络瞬时停顿或本容器 GIL 饥饿，两者事后
  不可区分），这种形状**重试能救**。只重试「可能一次就好」的失败：超时 / 连接类
  URLError / 5xx（Ollama 载模型中回 503）；**4xx 绝不重试**（模型名或路径写错，重试
  一万次也一样）。退避 0.25s 起、每次 ×2。
- **查询期熔断冷却**（`NL2SQL_EMBED_COOLDOWN`，默认 60s，`0`＝关闭）：**它是重试的配套**,
  不是额外功能 —— 没有它，通道整个消失时每次检索要花 `4s × 2`，比原来的 2.5s 更糟（一次
  问数有 2~6 次检索 ⇒ 白烧几十秒）。有一次失败就冷却 60s，窗口内直接返回 `None`（**零**
  等待，比原行为还省）。**只管查询期**：索引期（`embed_texts`）不受熔断影响，否则一次
  查询期的失败会让后台建库**静默丢掉整批向量**。
  **可观测性**（窗口本身是静默的，必须自己说出来）：**开启**打一条 `WARNING`、窗口内
  **每次跳过**打一条 `INFO`（两者都在生产日志级别内）。否则日志里的表现是「一条失败
  `WARNING` + 60 秒静默 + 又一条 `WARNING`」，事后完全查不出中间那几次检索为什么没有
  向量腿 —— 而「为什么降级」正是这类偶发故障唯一能留下的证据。

## 怎么从日志判断这一步成/败（唯一权威读法）

| 日志行 | 含义 |
|---|---|
| `向量可用：N 条 × D 维 / 第 i/T 次尝试 / M ms` | **成功**，且**三道形状闸全过**（不是 HTTP 200 就算）。`i ≥ 2` ⇒ **重试救回**（通道抖了一下，重试把它救回来了） |
| `… 调用失败，重试（第 i/T 次）` | 第 `i-1` 次尝试失败、正在重试（超时/连接类/5xx·429） |
| `… 调用失败（降级纯 FTS）: …` | 尝试**全部**用完 ⇒ 这一次检索没向量腿（纯 FTS） |
| `… HTTP 4xx（降级纯 FTS）` | 配置错（模型名/路径/维数），**不重试** |
| `响应条数不符 / 维数不符 / 服务端模型名不符 ⇒ 降级纯 FTS` | HTTP 通了但**形状闸没过** ⇒ 同样没向量腿 |
| `查询期熔断开启：接下来 Ns 不再尝试嵌入` | 通道进入冷却，**接下来 60s 的检索全走纯 FTS** |
| `查询期熔断冷却中（剩 S s）⇒ 本次不试嵌入` | 窗口内的一次检索：零等待、纯 FTS |
| `[retrieval] 查询嵌入不可用 ⇒ 本次只用 FTS 腿` | 调用方（`lance.py` / `jsonl.py`）确认这一次只剩 FTS 腿 |

⚠️ 成功行**只证明形状对**，不证明向量数值有意义（返回 1024 个 `0.0` 也会打「向量可用」）
—— 见下面「模型名闸的边界」，要抓这个只能上参照向量指纹。
⚠️ 这些行**没有 `rid`**（生产里是 `[rid=-]`，检索跑在同步工具池线程、contextvar 不跨线程）
⇒ 只能按**时间戳**跟具体问数对齐，不能按 rid 关联。
- **维数 / 模型校验**：向量长度必须等于 `NL2SQL_EMBED_DIM`（默认 1024）、**响应里报告
  的**模型名必须等于 `NL2SQL_EMBED_MODEL`，否则返回 `None`。两道都要有：换模型多半连
  维数一起变（维数闸能抓），但**同维数的另一个模型**（bge-m3 1024 → 别的 1024 模型）
  只靠维数闸抓不到 ⇒ 新向量与旧索引混在一起，相似度**静默错乱**（不报错，只是排序变
  垃圾），所以模型名这道闸是必须的，不是冗余。
- **模型名闸的边界（诚实说明）**：比对的是**响应体 `model` 字段**，归一规则只有
  「去空白 / 小写 / 只留最后一段路径」（`BAAI/bge-m3` == `bge-m3`，**不做模糊匹配**）。
  服务端**不回**该字段 ⇒ 跳过这道闸（只留维数闸）——否则一个正常的通道会被误杀成
  永久降级。**已实测**（2026-09-29，从生产容器打 `:11435`）：该实例拿不存在的模型名
  去问回 **HTTP 400**、成功时 `model` 报 `bge-m3` ⇒ **它不是「回显请求名」的服务端**，
  这道闸在我们这条通道上**真会绑**（回显型服务端会让它变 no-op —— 那是另一类服务端的
  风险，不是本通道）。要连回显型服务端也抓，唯一可靠办法是「**参照向量指纹**」（拿一句
  固定探针文本的向量存进索引 meta，查询期复算比对）——要动 indexer/索引格式，**未做**。
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
import threading
import time
import urllib.error
import urllib.request

_log = logging.getLogger(__name__)

ENV_BASE_URL = "NL2SQL_EMBED_BASE_URL"
ENV_MODEL = "NL2SQL_EMBED_MODEL"
ENV_DIM = "NL2SQL_EMBED_DIM"
ENV_TIMEOUT = "NL2SQL_EMBED_TIMEOUT"
ENV_QUERY_TIMEOUT = "NL2SQL_EMBED_QUERY_TIMEOUT"
ENV_RETRIES = "NL2SQL_EMBED_RETRIES"
ENV_COOLDOWN = "NL2SQL_EMBED_COOLDOWN"

DEFAULT_BASE_URL = "http://192.168.25.13:11435/v1"
DEFAULT_MODEL = "bge-m3"
DEFAULT_DIM = 1024
DEFAULT_TIMEOUT_S = 2.5
DEFAULT_QUERY_TIMEOUT_S = 4.0
DEFAULT_RETRIES = 1
DEFAULT_COOLDOWN_S = 60.0
RETRY_BACKOFF_S = 0.25

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


def _norm_model(name: object) -> str:
    """模型名归一：去空白、小写、只留最后一段路径（`BAAI/bge-m3` → `bge-m3`）。

    只做**等价变换**，不做模糊匹配 —— 这道闸的目的是抓住「服务端换了模型」，
    宽容放行等于白写。判不出（非字符串 / 空）⇒ `""`，由调用方决定跳过。
    """
    if not isinstance(name, str):
        return ""
    return name.strip().casefold().rsplit("/", 1)[-1]


def same_model(a: object, b: object) -> bool:
    """两个模型名是否同一个（归一后比较）。判不出（任一侧非字符串/空）⇒ `False`。

    给索引 meta 的 `embed_model` 与当前配置比较用（`search` 的向量腿闸）。
    """
    na, nb = _norm_model(a), _norm_model(b)
    return bool(na) and na == nb


def _model_matches(data: dict) -> bool:
    """响应体报告的模型名是否 == `NL2SQL_EMBED_MODEL`。

    服务端**没有**报告模型名 ⇒ `True`（跳过，只留维数闸）——见模块 docstring
    「模型名闸的边界」：宁可不抓，也不能把一个正常通道误杀成永久降级。
    """
    served = _norm_model(data.get("model"))
    if not served:
        return True
    want = _norm_model(model_name())
    if served == want:
        return True
    _log.warning(
        "[embedder] 服务端模型名不符（期望 %r，实际报告的 %r）⇒ 降级纯 FTS。"
        "同维数的另一个模型会与已有索引静默混用（排序变垃圾但不报错）；"
        "**换过模型必须重建索引**，配置写错就改 %s",
        model_name(),
        data.get("model"),
        ENV_MODEL,
    )
    return False


def timeout_s() -> float:
    try:
        value = float(os.environ.get(ENV_TIMEOUT, "").strip() or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def query_timeout_s() -> float:
    """查询期单条超时（`NL2SQL_EMBED_QUERY_TIMEOUT`，默认 4s）。"""
    try:
        value = float(os.environ.get(ENV_QUERY_TIMEOUT, "").strip() or DEFAULT_QUERY_TIMEOUT_S)
    except ValueError:
        return DEFAULT_QUERY_TIMEOUT_S
    return value if value > 0 else DEFAULT_QUERY_TIMEOUT_S


def retry_count() -> int:
    """单次调用内的重试次数（`NL2SQL_EMBED_RETRIES`，默认 1）。非法/负数 ⇒ 默认值。"""
    raw = os.environ.get(ENV_RETRIES, "").strip()
    if not raw:
        return DEFAULT_RETRIES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_RETRIES


def cooldown_s() -> float:
    """查询期失败后的熔断冷却秒数（`NL2SQL_EMBED_COOLDOWN`，默认 60）。`<=0` ⇒ 熔断关闭。"""
    raw = os.environ.get(ENV_COOLDOWN, "").strip()
    if not raw:
        return DEFAULT_COOLDOWN_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_COOLDOWN_S


# ── 查询期熔断（只管 `embed_one`；见模块 docstring「查询期熔断冷却」）──────
_breaker_until = 0.0
_breaker_lock = threading.Lock()


def breaker_open() -> bool:
    """查询期熔断是否在冷却窗口内（窗口内 `embed_one` 直接返回 None，不发请求）。"""
    return time.monotonic() < _breaker_until


def _open_breaker() -> None:
    cd = cooldown_s()
    if cd <= 0:
        return
    global _breaker_until
    with _breaker_lock:
        _breaker_until = time.monotonic() + cd
    # 状态变化 ⇒ WARNING：紧跟在上面那条失败 WARNING 之后，解释「接下来为什么没向量腿」
    _log.warning(
        "[embedder] 查询期熔断开启：接下来 %.0fs 不再尝试嵌入"
        "（窗口内检索直接走纯 FTS，零等待；NL2SQL_EMBED_COOLDOWN=0 可关掉）",
        cd,
    )


def _reset_breaker_for_test() -> None:
    """测试钩子：关掉冷却窗口、清掉待冷却状态（线上没有调用点）。"""
    global _breaker_until
    with _breaker_lock:
        _breaker_until = 0.0


def _open(req: urllib.request.Request, timeout: float):
    """transport 缝：测试替换它来数尝试次数 / 伪造超时（不必动全局 urllib）。"""
    return urllib.request.urlopen(req, timeout=timeout)


def _post(path: str, payload: dict, timeout: float, *, attempts: int | None = None,
          stats: dict | None = None) -> dict | None:
    """POST 一次（**可重试**）；任何异常都吞掉返回 None（含 HTTPError / URLError / 坏 JSON）。

    重试策略见模块 docstring：只重试超时 / 连接类 URLError / 5xx / 429，**4xx 不重试**。
    `attempts` 是**总尝试次数**（缺省 = `retry_count() + 1`）。
    传 `stats` ⇒ 成功时回填 `{"attempts": 实际尝试次数, "ms": 总耗时}`，给成功日志用。

    ⚠️ 返回 dict **只代表 HTTP + JSON 成功**，不代表向量可用（条数/模型名/维数三道闸在
    调用方 `embed_texts` 里）；所以「成功日志」不打在这里，打在三道闸之后。
    """
    url = f"{base_url()}{path}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    total = max(1, attempts if attempts is not None else retry_count() + 1)
    delay = RETRY_BACKOFF_S
    started = time.monotonic()
    for attempt in range(total):
        last = attempt + 1 >= total
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with _open(req, timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if stats is not None:
                stats.update(attempts=attempt + 1, ms=int((time.monotonic() - started) * 1000))
            return data
        except urllib.error.HTTPError as e:  # noqa: PERF203
            # Ollama 载模型时回 503；4xx 是配置错（模型名/路径），重试无意义
            retryable = e.code >= 500 or e.code == 429
            if not retryable or last:
                _log.warning("[embedder] %s HTTP %s（降级纯 FTS）", url, e.code)
                return None
            _log.warning("[embedder] %s HTTP %s，重试（第 %d/%d 次）", url, e.code, attempt + 2, total)
        except Exception as e:  # noqa: BLE001 —— URLError / timeout / JSONDecodeError
            if last:
                _log.warning("[embedder] %s 调用失败（降级纯 FTS）: %s", url, e)
                return None
            _log.warning("[embedder] %s 调用失败，重试（第 %d/%d 次）: %s", url, attempt + 2, total, e)
        time.sleep(delay)
        delay *= 2
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

    total_attempts = retry_count() + 1
    stats: dict = {}
    data = _post(
        "/embeddings",
        {"model": model_name(), "input": items},
        timeout if timeout is not None else timeout_s(),
        attempts=total_attempts,
        stats=stats,
    )
    if not isinstance(data, dict):
        return None
    if not _model_matches(data):
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
    # **三道闸全过**才打成功日志（不是 HTTP 200 就打）—— 否则「成功」与「返回了一堆无用
    # 向量」在日志里长得一样。带尝试次数：`第 2/2 次` 出现 ⇒ **重试救回**，是发版后判断
    # 「加上重试到底有没有用」的唯一直接证据。
    _log.info(
        "[embedder] 向量可用：%d 条 × %d 维 / 第 %d/%d 次尝试 / %dms",
        len(out), expected, stats.get("attempts", 1), total_attempts, stats.get("ms", 0),
    )
    return out


def embed_one(text: str, *, timeout: float | None = None) -> list[float] | None:
    """单条查询向量（问数期：每次检索 1 次调用）。失败返回 None。

    与 `embed_texts` 的两点差异（都是**查询期专用**，见模块 docstring）：
    ① 缺省超时是 `query_timeout_s()`（4s）而不是 `timeout_s()`（2.5s）；
    ② 失败后开**熔断冷却**：窗口内直接返回 None（零等待），不再让每一次检索都把超时
       预算花光。`NL2SQL_EMBED_COOLDOWN=0` 可整体关掉。
    """
    if breaker_open():
        # INFO 而不是 DEBUG：生产日志级别到 INFO ⇒ 这条必须能落进日志，否则窗口内
        # 每次检索「为什么又没向量腿」在日志里毫无痕迹（见模块 docstring 可观测性）。
        _log.info("[embedder] 查询期熔断冷却中（剩 %.1fs）⇒ 本次不试嵌入",
                  max(0.0, _breaker_until - time.monotonic()))
        return None
    out = embed_texts([text], timeout=timeout if timeout is not None else query_timeout_s())
    if not out:
        _open_breaker()
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
        "error": "" if ok else "调用失败 / 维数不符 / 模型名不符（三者都可能，详见日志）",
    }
