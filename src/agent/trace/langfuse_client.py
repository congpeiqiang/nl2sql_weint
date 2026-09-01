"""Langfuse 公共模块 — 集中管理客户端与回调 handler 的创建。

所有 trace 埋点经由本模块获取，避免业务代码散落 langfuse 初始化。
SDK: langfuse 4.14.4（自带 langchain/langgraph 集成，无需单独 langfuse-langchain）。
配置从 .env 读取（LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL）。

用法：
    from agent.trace.langfuse_client import get_langfuse_handler, auth_check

    graph.with_config({"recursion_limit": 500, "callbacks": [get_langfuse_handler()]})

M4 版本管理：prompt 装配处用 get_prompt_text(name, label, fallback) 从 Langfuse 拉
system prompt（失败回退本地文件）；同步脚本用 create_prompt 上传本地 prompt 文件。
"""
from __future__ import annotations

import logging
import os
import random

_logger = logging.getLogger(__name__)

_client = None
_handler = None

# M5 灰度：本进程解析到的 prompt label（进程级，import 时掷一次）+ 各 label 已拉到的版本
_CANARY_RESOLVED: str | None = None
_PROMPT_VERSIONS: dict[str, int] = {}
# M4 评估后新增：prompt 来源追踪（"name@label" → langfuse|local），供 metadata.prompt.source
_PROMPT_SOURCE: dict[str, str] = {}


def langfuse_enabled() -> bool:
    """总开关：LANGFUSE_ENABLE（默认 true，保持既有行为）。

    设 0/false/no/off 时停用一切运行时 Langfuse 埋点/打分/prompt 拉取
    （监控旁路整体一键关停），但 get_client() 本体仍可用——
    collect_badcase 等管理工具需独立读历史 trace，不受此开关影响。
    这是回滚的最顶层手段（比 LANGFUSE_PROMPT_ENABLED 范围更大）。
    """
    v = (os.getenv("LANGFUSE_ENABLE", "true") or "true").strip().lower()
    return v not in ("0", "false", "no", "off")


def get_client():
    """返回全局 Langfuse 客户端单例（惰性创建，配置走 .env）。

    注意：本函数不 gate 总开关（管理工具独立调用需要真实客户端）；
    运行时埋点由 get_langfuse_callbacks / create_score / get_prompt_text 等各自 gate。
    """
    global _client
    if _client is None:
        from langfuse import get_client as _get_client

        _client = _get_client()
        _apply_release(_client)
    return _client


def _apply_release(client) -> None:
    """把 LANGFUSE_RELEASE 设到客户端全局 release（trace 属性 → Release 页分组）。

    4.14.4 CallbackHandler 不读 metadata 里的 release 键，但 trace 创建时统一取
    `client._release`（init 参数或 CI 公共 env 兜底）——这里从 .env 显式注入。
    """
    rel = os.getenv("LANGFUSE_RELEASE", "") or ""
    if not rel:
        return
    try:
        client._release = rel
        _logger.info("[langfuse] release=%s（LANGFUSE_RELEASE）", rel)
    except Exception as e:  # noqa: BLE001
        _logger.debug("[langfuse] 设 release 失败: %s", e)


# ── M5 灰度：A/B 分流（进程级）────────────────────────────

def resolve_prompt_label() -> str:
    """返回本进程应使用的 prompt label（进程级，只决定一次，import 时掷骰）。

    canary 部署标准模型：N 个实例里约 ratio*N 个跑新版本。优先级：
    1. `LANGFUSE_PROMPT_LABEL` —— 显式指定（run_experiment A/B、灰度演练直接用）。
    2. `LANGFUSE_CANARY_LABEL` + `LANGFUSE_CANARY_RATIO` —— 按比例掷骰，命中走 canary。
    3. 默认 `production`（全局旧版，回滚即清空 canary 环境变量）。
    """
    global _CANARY_RESOLVED
    if _CANARY_RESOLVED is not None:
        return _CANARY_RESOLVED
    explicit = (os.getenv("LANGFUSE_PROMPT_LABEL", "") or "").strip()
    if explicit:
        _CANARY_RESOLVED = explicit
    else:
        label = (os.getenv("LANGFUSE_CANARY_LABEL", "") or "").strip()
        try:
            ratio = float(os.getenv("LANGFUSE_CANARY_RATIO", "0") or "0")
        except ValueError:
            ratio = 0.0
        if label and ratio > 0 and random.random() < ratio:
            _CANARY_RESOLVED = label
        else:
            _CANARY_RESOLVED = "production"
    _logger.info("[langfuse] prompt label=%s（A/B 分流）", _CANARY_RESOLVED)
    return _CANARY_RESOLVED


def prompt_label_info() -> dict:
    """当前进程的 prompt label + 版本 + 来源（供 trace metadata 注入，A→B 切换可见分组）。

    - `source`：当前 label 下所有已装配 prompt 的来源聚合——全部 Langfuse → langfuse；
      全部本地 → local；有 Langfuse 有本地（同进程混用）→ mixed。排查「主 Langfuse、
      子本地」这类半回退一眼可见。
    """
    label = resolve_prompt_label()
    info = {"prompt_label": label}
    ver = _PROMPT_VERSIONS.get(label)
    if ver:
        info["prompt_version"] = ver
    sources = {s for k, s in _PROMPT_SOURCE.items() if k.endswith(f"@{label}")}
    if len(sources) == 1:
        info["source"] = next(iter(sources))
    elif len(sources) > 1:
        info["source"] = "mixed"
    return info


def get_langfuse_handler():
    """返回 LangChain/LangGraph 回调 handler 单例；总开关关闭时返回 None。

    用于 graph.with_config({"callbacks": [...]})。langfuse 4.x 的 CallbackHandler
    基于 contextvar 为每次 run 创建独立 trace，可安全跨并发请求共享。
    """
    global _handler
    if not langfuse_enabled():
        return None
    if _handler is None:
        from langfuse.langchain import CallbackHandler

        _handler = CallbackHandler()
    return _handler


def get_langfuse_callbacks() -> list:
    """graph.with_config 用的 callbacks 列表（总开关关闭时为空，不挂 trace）。

    deepagents graph 挂载点统一用它，避免 LANGFUSE_ENABLE=false 时传 [None] 报错。
    """
    handler = get_langfuse_handler()
    return [handler] if handler is not None else []


def auth_check() -> bool:
    """校验与 Langfuse Cloud 的连通性（同步调用，返回 bool）。

    总开关关闭时直接返回 False；其余失败返回 False，不抛异常。
    """
    if not langfuse_enabled():
        return False
    try:
        return bool(get_client().auth_check())
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse] auth_check 失败: %s", e)
        return False


def create_score(
    name: str,
    value: float,
    trace_id: str = "",
    observation_id: str = "",
    comment: str = "",
    data_type: str = "NUMERIC",
    config: dict | None = None,
    metadata: dict | None = None,
) -> bool:
    """写一条 Langfuse Score（M3 五维评分 / 用户反馈打标）。

    create_score 入队后由 SDK 后台线程异步刷新，调用本身开销很小；任何异常
    仅 debug 告警，不影响主流程（评估是旁路，见方案文档 §3.1）。

    Args:
        name: 评分名（如 sql_valid_score / user-feedback）。
        value: 数值分（data_type="NUMERIC" 时 0~1 或 0~100）。
        trace_id: 写分目标 trace；与 observation_id 二选一（精确到 span 用后者）。
        comment: 一句话理由 / 原文。
    """
    if not langfuse_enabled():
        return False
    try:
        kwargs: dict = {"name": name, "value": value, "data_type": data_type}
        if trace_id:
            kwargs["trace_id"] = trace_id
        if observation_id:
            kwargs["observation_id"] = observation_id
        if comment:
            kwargs["comment"] = comment
        if config:
            kwargs["config"] = config
        if metadata:
            kwargs["metadata"] = metadata
        get_client().create_score(**kwargs)
        return True
    except Exception as e:  # noqa: BLE001
        _logger.debug("[langfuse] create_score(%s) 失败: %s", name, e)
        return False


# ── M4 版本管理：Prompt 拉取 / 上传 ────────────────────────
# 策略：Langfuse 是旁路。装配时优先从 Langfuse 拉（label 切换即时回滚），
# 任何失败（未配置/超时/404）回退本地文件，不影响启动与正常装配。

def prompt_enabled() -> bool:
    """M4 开关：LANGFUSE_PROMPT_ENABLED（默认 1=走 Langfuse；0/false=强制本地）。

    回滚演练的第二手段：置 0 后重启即用回本地 prompt 文件，不动 Langfuse。
    """
    v = (os.getenv("LANGFUSE_PROMPT_ENABLED", "1") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def get_prompt_text(
    name: str,
    label: str | None = None,
    fallback: str = "",
    cache_ttl_seconds: int = 60,
    required_markers: list[str] | None = None,
    min_chars: int = 0,
) -> str:
    """拉取 Langfuse text 型 prompt 正文；失败/未启用/内容校验不过则返回 fallback。

    - `label` 缺省时走 M5 进程级 A/B 分流（resolve_prompt_label）：
      显式 LANGFUSE_PROMPT_LABEL → canary 掷骰 → production。
    - 返回 `.prompt` 原始正文（含 {{VAR}} 占位符，供装配处本地替换），
      不用 get_langchain_prompt()（它会把 {{VAR}} 转成 {VAR} 的 Langchain 格式）。
    - `required_markers` / `min_chars`：内容校验——Langfuse 正文若缺关键占位符或
      过短（如 UI 编辑把 {{CHART_SPEC}} 弄丢），视为残缺回退本地，防止带着残缺
      prompt 上线（装配处 .replace() 对缺失占位符是静默 no-op）。仅主/子系统
      prompt 装配传；sync_prompts 对比原文时缺省不做校验。
    - `max_retries=0`：SDK 默认内层重试 2 次（指数退避最长 10s）会放大 Langfuse
      宕机时的启动阻塞——兜底逻辑是我们自己的 except，SDK 重试纯属浪费，关掉。
    - 404（prompt 不存在/被改名）与网络故障分开记日志：前者是配置错误（ERROR），
      后者是可用性降级（WARNING）。
    - 拉到的版本记入 _PROMPT_VERSIONS[label]；来源记 _PROMPT_SOURCE
      （供 prompt_label_info 注入 metadata.prompt.source = langfuse|local|mixed）。
    - 总开关 LANGFUSE_ENABLE=false 时同样回退本地（比 LANGFUSE_PROMPT_ENABLED 更顶层）。
    """
    label = label if label is not None else resolve_prompt_label()
    source_key = f"{name}@{label}"

    def _local(reason: str) -> str:
        _PROMPT_SOURCE[source_key] = "local"
        _logger.debug("[langfuse] prompt %s(label=%s) 本地兜底: %s", name, label, reason)
        return fallback

    if not langfuse_enabled() or not prompt_enabled():
        return _local("未启用（LANGFUSE_ENABLE / LANGFUSE_PROMPT_ENABLED）")
    try:
        client = get_client()
        p = client.get_prompt(
            name,
            label=label,
            type="text",
            cache_ttl_seconds=cache_ttl_seconds,
            max_retries=0,
            fetch_timeout_seconds=3000,
        )
        text = getattr(p, "prompt", "") or ""
        # 内容校验：残缺的 Langfuse 正文比本地还危险，宁可回退本地
        ok_len = len(text.strip()) >= min_chars
        ok_markers = not required_markers or all(m in text for m in required_markers)
        if not text or not ok_len or not ok_markers:
            miss = [m for m in (required_markers or []) if m not in text]
            detail = miss or ("空正文" if not text else f"过短(len={len(text)})")
            _logger.warning(
                "[langfuse] prompt %s(label=%s) 内容校验不过（%s）→ 回退本地",
                name, label, detail,
            )
            return _local(f"内容校验不过:{detail}")
        ver = getattr(p, "version", "?")
        _PROMPT_VERSIONS[label] = ver
        _PROMPT_SOURCE[source_key] = "langfuse"
        _logger.info("[langfuse] prompt %s(label=%s) v%s 生效", name, label, ver)
        return text
    except Exception as e:  # noqa: BLE001
        try:
            from langfuse.api import NotFoundError

            is_404 = isinstance(e, NotFoundError)
        except Exception:  # noqa: BLE001
            is_404 = False
        if is_404:
            _logger.error(
                "[langfuse] prompt %s(label=%s) 在 Langfuse 不存在(404)：可能被改名/删除，"
                "请检查 Prompt 配置，当前回退本地: %s", name, label, e,
            )
            return _local("404 不存在")
        _logger.warning("[langfuse] prompt %s(label=%s) 拉取失败，回退本地: %s", name, label, e)
        return _local("拉取失败")


def get_prompt_version(name: str, label: str | None = None) -> int | None:
    """拉取 Langfuse text 型 prompt 的当前版本号（M6 skill 版本追踪用）。

    - `label` 缺省走 M5 进程级 A/B 分流（与 system prompt 同 label，A→B 切换一致）。
    - 失败/未启用/label 不存在（404）→ None，调用方回退本地版本（source=local）。
    - 走 SDK 缓存（cache_ttl 60s），服务启动期批量拉 14 个 skill 开销可控。
    """
    if not langfuse_enabled() or not prompt_enabled():
        return None
    if label is None:
        label = resolve_prompt_label()
    try:
        # max_retries=0：M6 启动期串行拉 15 个 skill 版本，SDK 内层重试会放大
        # Langfuse 宕机阻塞；超时 2s（版本查询只取 version，比全文装配更短）。
        p = get_client().get_prompt(
            name,
            label=label,
            type="text",
            cache_ttl_seconds=60,
            max_retries=0,
            fetch_timeout_seconds=2000,
        )
        ver = getattr(p, "version", None)
        return ver if isinstance(ver, int) else None
    except Exception:  # noqa: BLE001
        return None


def create_prompt(
    name: str,
    prompt: str,
    labels: list[str] | None = None,
    commit_message: str = "",
) -> bool:
    """上传/更新 Langfuse text 型 prompt。labels 默认 ["production", "latest"]。

    每次调用即新版本（同名同内容也会递增版本）；调用方（sync_prompts.py）应在
    内容无变化时跳过，避免无意义的版本堆积。
    """
    if not langfuse_enabled():
        return False
    try:
        kwargs: dict = {"name": name, "prompt": prompt, "type": "text"}
        kwargs["labels"] = labels or ["production", "latest"]
        if commit_message:
            kwargs["commit_message"] = commit_message
        get_client().create_prompt(**kwargs)
        return True
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse] create_prompt(%s) 失败: %s", name, e)
        return False


def update_prompt_labels(name: str, version: int, new_labels: list[str]) -> bool:
    """把某个 prompt 版本的标签整组替换（回滚/分流用）。

    Langfuse 的 labels 跨版本唯一：把 `production` 打回旧版本 vN 时，
    新版本上的 production 会被自动移除——即「production 标签随时可切回旧版本」。
    `latest` 由 Langfuse 托管（恒指最新版本），无需也不应手动维护。
    """
    if not langfuse_enabled():
        return False
    try:
        get_client().update_prompt(name=name, version=version, new_labels=new_labels)
        return True
    except Exception as e:  # noqa: BLE001
        _logger.warning("[langfuse] update_prompt_labels(%s v%s) 失败: %s", name, version, e)
        return False
