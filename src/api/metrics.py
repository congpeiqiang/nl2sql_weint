# -*- coding: utf-8 -*-
r"""P2-3：`GET /metrics` 指标 + 最小告警。

**验收（清单原文）**：并发压测时能在面板上看到**队列深度**与**事件循环延迟**。
这两样都不是 HTTP 请求能"现算"的（一个在 langgraph 的调度器里、一个是时间维度的
现象），只能由后台任务周期性采样 → 写进 prometheus_client 的**全局 REGISTRY**。

**为什么复用 langgraph 自带的 handler**：`langgraph_api/api/meta.py` 的
`meta_metrics` 已经在做两件我们要的事 —— `?format=prometheus` 直接
`generate_latest()`（读的就是同一个全局 REGISTRY，所以我们注册的指标**自动**
出现在同一份 exposition 里）、`?format=json` 给「队列/worker/连接池」快照
（`Runs.stats` → `n_pending`/`n_running`/等待时长）。它本来只在设了
`MOUNT_PREFIX` 时才挂载（`langgraph_api/server.py`），本仓没设 → **由我们在
custom_app 的 ROUTES 里挂上**，不重复实现、不 fork 上游、上游加字段我们白得。
代价是依赖了它的内部模块：真断了由 `scripts/verify_metrics.py` 报警（不是静默
降级成空面板），且 `metrics_endpoint` 自带兜底（见下）。

**指标清单**（统一 `nl2sql_` 前缀，不与 `python_*`/上游家族撞名）：

| 指标 | 类型 | 采样来源 / 埋点 |
| --- | --- | --- |
| `nl2sql_process_rss_bytes` | Gauge | `/proc/self/statm`；非 Linux 走 psapi（都拿不到就不产出） |
| `nl2sql_event_loop_lag_seconds` / `_max_seconds` | Gauge | 采样任务自己的 `asyncio.sleep` 超时值（见下） |
| `nl2sql_run_queue_running` / `_pending` | Gauge | `langgraph_runtime.ops.Runs.stats` |
| `nl2sql_run_queue_wait_seconds{quantile=max\|med}` | Gauge | 同上（pending run 的等待时长） |
| `nl2sql_worker_slots{state=max\|active\|available}` | Gauge | `langgraph_runtime.metrics.get_metrics()` |
| `nl2sql_llm_calls_total{outcome=ok\|timeout\|error}` | Counter | `ModelTimeoutMiddleware`（模型调用唯一收口点） |
| `nl2sql_llm_latency_seconds{outcome}` | Histogram | 同上 |
| `nl2sql_llm_gate_total{result=free\|waited\|bypassed\|off\|sync_on_loop}` | Counter | `agent/utils/llm_gate`（P3-4 并发闸；`bypassed` 涨 = 闸已长期不够用） |
| `nl2sql_llm_gate_wait_seconds` | Histogram | 同上（只统计排队后拿到的那部分） |
| `nl2sql_llm_retries_total{outcome=recovered\|exhausted}` | Counter | 同上（连接类错误的退避重试结局） |
| `nl2sql_sqlite_lock_wait_seconds{store}` 等 4 条 | Histogram/Counter/Gauge | `agent/utils/prom_metrics.metered_rlock`（7 个共享连接存储） |
| `nl2sql_mcp_servers{status=ok\|failed}` | Gauge | `tools/mcp_tool._sub_entries` 注册表 |
| `nl2sql_process_children` | Gauge | `/proc` 里 PPid==本进程的进程数（MCP stdio 子进程口径） |
| `nl2sql_alerts_total{alert}` / `nl2sql_alert_active{alert}` | Counter/Gauge | 本模块的看门狗 |

**事件循环延迟怎么量**：采样循环 `await asyncio.sleep(interval)` 前后取
`loop.time()`，实际睡过头多少就是延迟多少（同步代码堵住循环 → 心跳被推迟）。
健康值在毫秒级；`> 0.5s` 说明有同步阻塞（本仓的历史坑就是 sqlite / 文件 IO 在
事件循环上跑，见 `docs/生产就绪度评估/` 的 P1-14）。

**最小告警**：不是告警系统，是"翻日志能看见"的那一层 —— 每条规则**连续 N 轮**
成立才 `[alert] …` 记一条 WARNING，**只在状态翻转时记**（不刷屏），恢复时
`[alert-clear]`；同时落 `nl2sql_alert_active` 供面板显示。查法：
`grep '\[alert\]' /app/data/logs/agent-server.log`。阈值全走环境变量（见
`_rules()` 的 `env` 列，`0` = 关闭该条），默认值只对**本仓实际规模**负责：
N_JOBS_PER_WORKER=10、`/app/data` 在宿主机上、单次问数约吃 3 个 run。

**暴露面（安全）**：`/metrics` **不在** auth 白名单 → 走 `AuthMiddleware` 的
内部判定：带 Cookie 或带 XFF 的外部请求 → 401/403；只有容器网络内部
（内网 IP + 无 Cookie + 无 XFF）才放行 —— 这是刻意的（指标里有库名/规模信息，
不该给普通用户看）。生产 nginx 只把 `/api/` 等转发给后端，`/metrics` 落在
前端 → **外部路径根本到不了这里**（verify_metrics.py 有断言）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

_logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECS = 10.0
_PAGE_SIZE = 4096  # /proc/self/statm 的单位是页；本仓只跑在 x86-64/arm64 Linux 容器

# ── 指标对象（注册进全局 REGISTRY，/metrics 的 prometheus 输出自动带上）──
RSS = Gauge("nl2sql_process_rss_bytes", "进程常驻内存 RSS（字节）")
LOOP_LAG = Gauge(
    "nl2sql_event_loop_lag_seconds",
    "最近一轮采样中事件循环被阻塞的秒数（= asyncio.sleep 的实际超时值）",
)
LOOP_LAG_MAX = Gauge("nl2sql_event_loop_lag_max_seconds", "进程启动以来事件循环延迟的最大值")
RUNNING = Gauge("nl2sql_run_queue_running", "在跑的后台 run/任务数（inmem workers）")
PENDING = Gauge("nl2sql_run_queue_pending", "已提交、还在排队等槽位的 run 数")
QUEUE_WAIT = Gauge(
    "nl2sql_run_queue_wait_seconds", "排队中的 run 已等待的秒数", labelnames=("quantile",)
)
WORKER_SLOTS = Gauge(
    "nl2sql_worker_slots", "worker 槽位数量（max=上限/active=在跑/available=空闲）",
    labelnames=("state",),
)
MCP_SERVERS = Gauge("nl2sql_mcp_servers", "MCP server 注册表条目数", labelnames=("status",))
CHILDREN = Gauge(
    "nl2sql_process_children", "本进程的 OS 子进程数（MCP stdio 子进程口径；非 Linux 不产出）"
)
ALERTS_TOTAL = Counter("nl2sql_alerts_total", "告警触发次数（只在每次翻转为响时 +1）", labelnames=("alert",))
# P2-5：磁盘水位（每次采样直接读，一次 syscall）+ 各区域占用（由保留策略那一轮量完缓存，
# **不在采样轮里遍历目录** —— 采样 10s 一轮，遍历 /app/data 会把事件循环拖住）
DISK_FREE_BYTES = Gauge("nl2sql_disk_free_bytes", "数据卷可用字节", labelnames=("path",))
DISK_USED_RATIO = Gauge("nl2sql_disk_used_ratio", "数据卷已用比例 0~1", labelnames=("path",))
DATA_BYTES = Gauge("nl2sql_data_bytes", "各区域占用字节（从保留策略那轮缓存，非实时）", labelnames=("area",))
ALERT_ACTIVE = Gauge("nl2sql_alert_active", "该告警此刻是否在响（1=在响 / 0=已恢复）", labelnames=("alert",))
SAMPLES = Counter("nl2sql_metrics_samples_total", "采样轮次（按结果分：ok/error）", labelnames=("result",))


# ── 采样：系统层（内存 / 子进程）────────────────────────────────────
def _read_rss_bytes() -> Optional[int]:
    """读进程 RSS。Linux 走 `/proc/self/statm`（第二列 = 常驻页数）。

    非 Linux（开发机 Windows / macOS）退到 psapi / `resource`；都拿不到返回
    None → **不产出该指标**（宁可缺一条，也不要报一个假 0 —— 内存告警会因此
    永不触发）。
    """
    try:
        with open("/proc/self/statm", "r") as f:
            fields = f.read().split()
        if len(fields) >= 2:
            return int(fields[1]) * _PAGE_SIZE
    except (OSError, ValueError):
        pass
    # Windows：psapi.GetProcessMemoryInfo（不需要 psutil，本仓未装）
    try:  # pragma: no cover - 平台相关
        import ctypes
        from ctypes import wintypes

        class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
            ctypes.windll.kernel32.GetCurrentProcess(),  # type: ignore[attr-defined]
            ctypes.byref(counters),
            counters.cb,
        )
        if ok:
            return int(counters.WorkingSetSize)
    except Exception:  # noqa: BLE001
        pass
    try:  # pragma: no cover - 平台相关
        import resource

        ru = resource.getrusage(resource.RUSAGE_SELF)
        # macOS 的 ru_maxrss 单位是字节，Linux 是 KB；只在非 Linux 走到这里
        return int(ru.ru_maxrss)
    except Exception:  # noqa: BLE001
        return None


def _ppid_from_stat(raw: bytes) -> Optional[int]:
    """从 `/proc/<pid>/stat` 里取 ppid（第 4 个字段）。

    ⚠️ 不能 `split()[3]`：字段 2 是 `(comm)`，**可含空格与右括号**（如
    `(python (x))`）→ 必须从**最后一个** `)` 之后开始切：其后的第 1 个是 state，
    第 2 个才是 ppid。这是 `/proc` 解析的经典坑。
    """
    close = raw.rfind(b")")
    if close < 0:
        return None
    fields = raw[close + 1:].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _count_children(pid: Optional[int] = None, proc_root: str = "/proc") -> Optional[int]:
    """数 OS 里 PPid == 本进程的进程数（MCP stdio 子进程的口径）。

    非 Linux（无 `/proc`）→ None = 不产出该指标。本仓的 MCP 子进程由
    `langchain_mcp_adapters` 起（stdio），**按工具调用存活**，所以这个数会在
    0/1 之间跳 —— 它回答的是"此刻有几个 MCP 进程在跑"，不是"配了几个库"
    （后者看 `nl2sql_mcp_servers`）。
    """
    pid = os.getpid() if pid is None else pid
    if not os.path.isdir(proc_root):
        return None
    count = 0
    try:
        names = os.listdir(proc_root)
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, name, "stat"), "rb") as f:
                raw = f.read()
        except OSError:
            continue  # 进程刚退出：正常竞态，跳过
        if _ppid_from_stat(raw) == pid:
            count += 1
    return count


# ── 采样：langgraph 队列 / worker ───────────────────────────────────
async def _read_queue_snapshot() -> Optional[dict[str, Any]]:
    """读队列深度与 worker 槽位。

    与 `langgraph_api/api/meta.py` 的 JSON 分支**同源**（同样的 `connect()` +
    `Runs.stats`），区别只在异常处理：那边是请求路径（失败就 500），这边是后台
    采样（失败本轮跳过、下轮再来）。任何一环拿不到 → 返回 None，**不写假 0**。
    """
    try:
        from langgraph_api.feature_flags import IS_POSTGRES_OR_GRPC_BACKEND
        from langgraph_runtime.database import connect
        from langgraph_runtime.metrics import get_metrics
    except Exception as e:  # noqa: BLE001
        _logger.debug("[metrics] 读不到 langgraph 运行时（本轮跳过队列指标）: %s", e)
        return None
    try:
        if IS_POSTGRES_OR_GRPC_BACKEND:  # pragma: no cover - 本仓跑 inmem
            from langgraph_api.grpc.ops import Runs
        else:
            from langgraph_runtime.ops import Runs

        async with connect() as conn:
            stats = dict(await Runs.stats(conn) or {})
    except Exception as e:  # noqa: BLE001
        _logger.debug("[metrics] 读队列深度失败（本轮跳过）: %s", e)
        return None
    try:
        workers = dict(get_metrics().get("workers") or {})
    except Exception as e:  # noqa: BLE001
        _logger.debug("[metrics] 读 worker 槽位失败: %s", e)
        workers = {}
    return {"queue": stats, "workers": workers}


def _read_mcp_registry() -> Optional[dict[str, int]]:
    """读 MCP 注册表（`_sub_entries`）按 status 计数。"""
    try:
        from agent.tools.mcp_tool import _sub_entries
    except Exception as e:  # noqa: BLE001
        _logger.debug("[metrics] 读不到 MCP 注册表（本轮跳过）: %s", e)
        return None
    ok = sum(1 for entry in list(_sub_entries.values()) if getattr(entry, "status", "") == "ok")
    total = len(_sub_entries)
    return {"ok": ok, "failed": max(0, total - ok)}


# ── 告警（最小看门狗）───────────────────────────────────────────────
@dataclass(frozen=True)
class _Rule:
    """一条告警规则：读某个读数 → 与阈值比 → 连续 `samples` 轮成立就叫。"""

    name: str
    env: str                      # 阈值环境变量名（`0` / 负数 = 关闭该条）
    default: float
    value: Callable[[dict], Optional[float]]
    trip: Callable[[float, float], bool]
    samples: int
    message: Callable[[float, float, dict], str]


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "无数据"
    return f"{v:.3g}"


def _rules() -> list[_Rule]:
    """默认阈值只对**本仓的实际规模**负责，说明见每条 message。

    `samples`：水位型指标（内存/积压）要连续 3 轮，避免单次抖动；事件计数型
    （失败数/锁竞争，按两次采样差值算）本身就是窗口量，1 轮即可。
    """
    return [
        _Rule(
            name="event_loop_lag",
            env="NL2SQL_ALERT_LOOP_LAG_SECS",
            default=0.5,
            value=lambda r: r.get("event_loop_lag"),
            trip=lambda v, t: v > t,
            samples=3,
            message=lambda v, t, r: (
                f"事件循环延迟 {_fmt(v)}s > {_fmt(t)}s（连续 3 轮）：有同步代码堵住了事件循环，"
                "同进程的所有并发请求都在跟着变慢 —— 查 P1-14 类搬运点（sqlite/文件 IO 是否回到循环上）"
            ),
        ),
        _Rule(
            name="run_slots_saturated",
            env="NL2SQL_ALERT_RUNNING_RUNS",
            default=8.0,
            value=lambda r: r.get("running"),
            trip=lambda v, t: v >= t,
            samples=3,
            message=lambda v, t, r: (
                f"在跑 run {_fmt(v)} 个 >= {_fmt(t)}（槽位上限 {r.get('worker_max') or '?'}）："
                "并发已到天花板（一次问数约吃 3 个 run），新请求会开始排队等槽位"
            ),
        ),
        _Rule(
            name="run_backlog",
            env="NL2SQL_ALERT_PENDING_RUNS",
            default=20.0,
            value=lambda r: r.get("pending"),
            trip=lambda v, t: v >= t,
            samples=3,
            message=lambda v, t, r: (
                f"排队 run {_fmt(v)} 个 >= {_fmt(t)}：队列积压，用户在界面上的表现为「点了没反应/一直转圈」"
            ),
        ),
        _Rule(
            name="memory_high",
            env="NL2SQL_ALERT_RSS_MB",
            default=4096.0,
            value=lambda r: (r["rss_bytes"] / 1048576.0) if r.get("rss_bytes") else None,
            trip=lambda v, t: v >= t,
            samples=3,
            message=lambda v, t, r: (
                f"进程 RSS {_fmt(v)}MB >= {_fmt(t)}MB（连续 3 轮）：注意容器内存上限与 OOM Killer"
            ),
        ),
        _Rule(
            name="llm_failures",
            env="NL2SQL_ALERT_LLM_FAILURES",
            default=3.0,
            value=lambda r: r.get("llm_failures_delta"),
            trip=lambda v, t: v >= t,
            samples=1,
            message=lambda v, t, r: (
                f"本采样窗口内模型调用失败 {_fmt(v)} 次 >= {_fmt(t)}：模型服务在超时或报错，"
                "用户看到的是「模型调用超时」那条友好提示"
            ),
        ),
        _Rule(
            name="llm_gate_bypass",
            env="NL2SQL_ALERT_LLM_GATE_BYPASSES",
            default=5.0,
            value=lambda r: r.get("llm_gate_bypass_delta"),
            trip=lambda v, t: v >= t,
            samples=3,
            message=lambda v, t, r: (
                f"本采样窗口内有 {_fmt(v)} 次模型调用**等槽超预算被放行** >= {_fmt(t)}"
                "（连续 3 轮）：出站模型调用的并发已经长期超过 "
                "NL2SQL_LLM_MAX_CONCURRENCY —— 要么调高它（先确认上游扛得住），"
                "要么这就是 P2-7 那批 APIConnectionError 的来源（放行等于闸没起作用）"
            ),
        ),
        _Rule(
            name="sqlite_lock_contention",
            env="NL2SQL_ALERT_LOCK_CONTENTIONS",
            default=3.0,
            value=lambda r: r.get("lock_contention_delta"),
            trip=lambda v, t: v >= t,
            samples=1,
            message=lambda v, t, r: (
                f"本采样窗口内有 {_fmt(v)} 次等锁超过 0.1s：写热点（反馈/归属/trace）在互相排队，"
                "表现为「偶发但普遍的慢」，看 nl2sql_sqlite_lock_wait_seconds 的 store 标签定位"
            ),
        ),
        _Rule(
            name="disk_low",
            env="NL2SQL_ALERT_DISK_FREE_PCT",
            default=10.0,
            value=lambda r: (r.get("disk_free_ratio") * 100.0)
            if r.get("disk_free_ratio") is not None
            else None,
            trip=lambda v, t: v <= t,
            samples=3,
            message=lambda v, t, r: (
                f"数据卷剩余 {_fmt(v)}% <= {_fmt(t)}%（连续 3 轮，{r.get('disk_path') or '?'}）："
                "盘满会同时打坏 SQLite 写、日志与 checkpoint（整站故障）—— 先看 "
                "nl2sql_data_bytes{area=…} 找出是谁在涨，再决定调 NL2SQL_RETENTION_DAYS_* 还是扩盘"
            ),
        ),
        _Rule(
            name="mcp_servers_failed",
            env="NL2SQL_ALERT_MCP_FAILED_SERVERS",
            default=1.0,
            value=lambda r: r.get("mcp_failed"),
            trip=lambda v, t: v >= t,
            samples=3,
            message=lambda v, t, r: (
                f"MCP server 注册表里有 {_fmt(v)} 个条目处于失败态（连续 3 轮）："
                "该库的工具对模型不可见，会被当成「库没建模/查不了」"
            ),
        ),
    ]


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _logger.warning("[metrics] %s 不是数字（%r），按默认 %s", name, raw, default)
        return default


_STATE_LOCK = threading.Lock()
_ALERT_STATE: dict[str, dict[str, Any]] = {}
# 事件计数型规则的"上一轮读数"（差值 = 本窗口新增量）
_LAST_TICK: dict[str, float] = {}


def _delta(key: str, current: Optional[float]) -> Optional[float]:
    """本窗口新增量。第一次见到该读数时返回 None（**不**把历史累计当成新增，
    否则进程刚起来就把开机以来的几百次失败一次性报成告警）。"""
    if current is None:
        return None
    prev = _LAST_TICK.get(key)
    _LAST_TICK[key] = float(current)
    return None if prev is None else max(0.0, float(current) - prev)


def _evaluate_alerts(readings: dict[str, Any]) -> list[str]:
    """评估全部规则，返回本轮**新触发**的告警名。日志在锁外统一发。"""
    fired: list[str] = []
    notices: list[str] = []
    for rule in _rules():
        threshold = _env_float(rule.env, rule.default)
        value = rule.value(readings)  # 先取值：关掉的规则也要推进差值基线
        with _STATE_LOCK:
            state = _ALERT_STATE.setdefault(
                rule.name, {"hits": 0, "firing": False, "value": None, "threshold": threshold}
            )
            state["value"] = value
            state["threshold"] = threshold
            if threshold <= 0:
                if state["firing"]:
                    state["firing"] = False
                    ALERT_ACTIVE.labels(rule.name).set(0)
                    notices.append(f"[alert-clear] {rule.name} | 该规则已关闭（{rule.env}=0）")
                state["hits"] = 0
                continue
            if value is None:
                continue  # 本轮无数据：既不加也不清（缺数据不该误报，也不该掩盖在响的告警）
            if rule.trip(value, threshold):
                state["hits"] += 1
                if state["hits"] >= rule.samples and not state["firing"]:
                    state["firing"] = True
                    ALERT_ACTIVE.labels(rule.name).set(1)
                    ALERTS_TOTAL.labels(rule.name).inc()
                    fired.append(rule.name)
                    notices.append(
                        f"[alert] {rule.name} | {rule.message(value, threshold, readings)}"
                        f"（阈值 {rule.env}={_fmt(threshold)}）"
                    )
            else:
                if state["firing"]:
                    state["firing"] = False
                    ALERT_ACTIVE.labels(rule.name).set(0)
                    notices.append(
                        f"[alert-clear] {rule.name} | 已恢复：当前 {_fmt(value)}，"
                        f"阈值 {_fmt(threshold)}，此前连续 {state['hits']} 轮超标"
                    )
                state["hits"] = 0
    for line in notices:
        _logger.warning("%s", line)
    return fired


def alert_state() -> dict[str, Any]:
    """当前告警快照（自检脚本 / 排障用；面板看 `nl2sql_alert_active`）。"""
    with _STATE_LOCK:
        return {
            name: {
                "firing": st["firing"],
                "value": st["value"],
                "hits": st["hits"],
                "threshold": st["threshold"],
            }
            for name, st in _ALERT_STATE.items()
        }


# ── 一轮采样 ───────────────────────────────────────────────────────
_loop_lag_max = 0.0


async def sample_once(lag: Optional[float] = None) -> dict[str, Any]:
    """跑一轮采样：读系统/队列/MCP → 写指标 → 评估告警。返回本轮读数。

    `lag` 由采样循环传入（它才知道自己睡过头多少）；手工/自检调用时可省，
    此时**不动**延迟指标、也不评估延迟规则（避免写一个假的 0）。
    """
    readings: dict[str, Any] = {}

    if lag is not None:
        _LOOP_LAG_SET(lag)  # 事件循环延迟只能由采样循环自己量（见模块头）

    rss = _read_rss_bytes()
    if rss is not None:
        RSS.set(rss)
        readings["rss_bytes"] = rss

    snapshot = await _read_queue_snapshot()
    if snapshot:
        queue = snapshot.get("queue") or {}
        workers = snapshot.get("workers") or {}
        if queue:
            readings["pending"] = float(queue.get("n_pending") or 0)
            readings["running"] = float(queue.get("n_running") or 0)
            PENDING.set(readings["pending"])
            RUNNING.set(readings["running"])
            for label, key in (
                ("max", "pending_runs_wait_time_max_secs"),
                ("med", "pending_runs_wait_time_med_secs"),
            ):
                value = queue.get(key)
                if value is not None:
                    QUEUE_WAIT.labels(label).set(float(value))
        if workers:
            for label, key in (("max", "max"), ("active", "active"), ("available", "available")):
                if workers.get(key) is not None:
                    WORKER_SLOTS.labels(label).set(float(workers[key]))
            readings["worker_max"] = workers.get("max")

    mcp = _read_mcp_registry()
    if mcp is not None:
        readings["mcp_failed"] = float(mcp["failed"])
        MCP_SERVERS.labels("ok").set(mcp["ok"])
        MCP_SERVERS.labels("failed").set(mcp["failed"])

    children = _count_children()
    if children is not None:
        CHILDREN.set(children)
        readings["children"] = float(children)

    # P2-5 磁盘水位：`disk_usage` 是一次 syscall，每次采样直接读（要的就是"现在"）
    try:
        from agent.utils.retention import area_sizes, disk_status

        disk = disk_status()
        if disk.get("free_ratio") is not None:
            path_label = disk.get("path") or "?"
            DISK_FREE_BYTES.labels(path_label).set(disk["free_bytes"])
            DISK_USED_RATIO.labels(path_label).set(disk["used_ratio"])
            readings["disk_free_ratio"] = float(disk["free_ratio"])
            readings["disk_path"] = path_label
        # 各区域占用：保留策略那轮量完的缓存。**首轮之前是空的 ⇒ 不产出该指标**
        # （不写 0：写 0 会让人以为"这些目录是空的"）
        for area, nbytes in (area_sizes() or {}).items():
            DATA_BYTES.labels(area).set(float(nbytes))
    except Exception as e:  # noqa: BLE001  指标坏了不能让采样整体失败
        _logger.debug("[metrics] 读磁盘水位失败: %s", e)

    # 事件计数型：差值 = 本窗口新增（见 _delta）
    try:
        from agent.utils.prom_metrics import gate_bypass_count, llm_failure_count

        readings["llm_failures_delta"] = _delta("llm_failures", float(llm_failure_count()))
        readings["llm_gate_bypass_delta"] = _delta(
            "llm_gate_bypass", float(gate_bypass_count())
        )
    except Exception as e:  # noqa: BLE001
        _logger.debug("[metrics] 读 LLM 计数失败: %s", e)
    readings["lock_contention_delta"] = _delta(
        "lock_contention", _sum_counter("nl2sql_sqlite_lock_contention_total")
    )

    _evaluate_alerts(readings)
    return readings


def _LOOP_LAG_SET(lag: float) -> None:
    global _loop_lag_max
    LOOP_LAG.set(lag)
    if lag > _loop_lag_max:
        _loop_lag_max = lag
        LOOP_LAG_MAX.set(lag)


def _sum_counter(name: str) -> Optional[float]:
    """把某个 Counter 家族所有标签的合计值读出来（不依赖 label 值集合）。"""
    try:
        from prometheus_client import REGISTRY

        # ⚠️ counter 的**家族名不含 `_total`**（prometheus_client 会剥掉），
        # 所以拿 "xxx_contention_total" 去比 metric.name 永远匹配不上（实测踩过）
        family = name[:-6] if name.endswith("_total") else name
        total = 0.0
        seen = False
        for metric in REGISTRY.collect():
            if metric.name != family:
                continue
            for sample in metric.samples:
                if sample.name == name:
                    total += float(sample.value)
                    seen = True
        return total if seen else None
    except Exception as e:  # noqa: BLE001
        _logger.debug("[metrics] 汇总 %s 失败: %s", name, e)
        return None


# ── 采样循环（由 custom_app 的 lifespan 起停）────────────────────────
_task: Optional[asyncio.Task] = None


def interval_secs() -> float:
    """采样间隔（`NL2SQL_METRICS_INTERVAL_SECS`，默认 10s，下限 1s）。

    在采样任务启动时读一次 —— 改环境变量要重启（与告警阈值不同，后者每轮现读）。
    """
    value = _env_float("NL2SQL_METRICS_INTERVAL_SECS", _DEFAULT_INTERVAL_SECS)
    return max(1.0, value)


async def _safe_sample(lag: Optional[float]) -> None:
    try:
        await sample_once(lag=lag)
        SAMPLES.labels("ok").inc()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001  采样失败绝不影响服务
        SAMPLES.labels("error").inc()
        _logger.warning("[metrics] 采样失败（本轮跳过，服务不受影响）: %s", e)


async def _sampler() -> None:
    interval = interval_secs()
    loop = asyncio.get_running_loop()
    _logger.warning(
        "[metrics] 指标采样已启动：间隔 %.1fs，端点 /metrics（?format=json 为队列快照）", interval
    )
    try:
        # 启动即出一次数（不含 lag：那一刻还没睡过，报 0 会是假数据）
        await _safe_sample(None)
        while True:
            started = loop.time()
            await asyncio.sleep(interval)
            lag = max(0.0, loop.time() - started - interval)
            await _safe_sample(lag)
    except asyncio.CancelledError:  # pragma: no cover - 正常停机路径
        _logger.warning("[metrics] 指标采样已停止")
        raise


def start() -> Optional[asyncio.Task]:
    """起采样任务（幂等）。在 lifespan 的 yield **之前**调用。"""
    global _task
    if _task is not None and not _task.done():
        return _task
    try:
        _task = asyncio.get_running_loop().create_task(_sampler(), name="nl2sql-metrics-sampler")
    except RuntimeError as e:  # 没有事件循环（同步上下文/自检）→ 不阻断
        _logger.warning("[metrics] 无跑着的事件循环，采样未启动: %s", e)
        return None
    return _task


async def stop() -> None:
    """停采样任务（幂等）。在 lifespan 收尾（排空 + flush 之后）调用。"""
    global _task
    task, _task = _task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:  # noqa: BLE001
        _logger.debug("[metrics] 停止采样时异常（忽略）: %s", e)


# ── HTTP 端点 ──────────────────────────────────────────────────────
_meta_metrics = None
_meta_error: Optional[str] = None
_meta_resolved = False


def _resolve_meta_metrics():
    """惰性解析 langgraph 自带的 exposition handler（prometheus 文本 + json 快照）。

    **不能在 import 期解析**：`import langgraph_api.api.meta` 会连带
    `langgraph_api.config`，而它要求 `REDIS_URI` 等配置齐全（缺了直接
    `KeyError: Config 'REDIS_URI' is missing`）。生产里 langgraph server 本来就
    是启动方、这些早已就位；但自检脚本/单测想**单独** import 本模块采一轮数，
    没理由被上游的 env 门槛挡住。顺带：上游哪天改了模块位置，这里也只是降级
    （有日志、有 `_meta_error` 上报），不会把服务起不来。
    """
    global _meta_metrics, _meta_error, _meta_resolved
    if _meta_resolved:
        return _meta_metrics
    _meta_resolved = True
    try:
        from langgraph_api.api.meta import meta_metrics  # noqa: PLC0415

        _meta_metrics = meta_metrics
    except Exception as e:  # noqa: BLE001
        _meta_error = f"{type(e).__name__}: {e}"
        _logger.warning("[metrics] 上游 meta_metrics 不可用（队列指标降级）: %s", _meta_error)
    return _meta_metrics


async def metrics_endpoint(request):
    """`GET|HEAD /metrics`：`?format=json` 走快照，其余走 prometheus 文本。"""
    handler = _resolve_meta_metrics()
    if handler is not None:
        response = await handler(request)
    else:  # pragma: no cover - 上游变更时的兜底（verify_metrics.py 会先发现）
        response = _degraded_response(request)
    # 指标必须每次现算：中间层/浏览器缓存会让面板显示几分钟前的队列深度
    response.headers["cache-control"] = "no-store"
    return response


def _degraded_response(request) -> Any:
    """拿不到 langgraph 的 handler 时的降级输出：**只**给本模块自采的指标。

    宁可给一份缺队列深度的 exposition（并显式说明降级），也不要 500 ——
    `nl2sql_event_loop_lag_seconds` 这类指标与上游无关，没理由一起陪葬。
    """
    if (request.query_params.get("format") or "prometheus") == "json":
        return JSONResponse(
            {
                "degraded": f"langgraph meta_metrics 不可用（{_meta_error}）",
                "queue": None,
                "alerts": alert_state(),
            }
        )
    body = generate_latest()
    header = f"# nl2sql: langgraph meta_metrics 不可用（{_meta_error}），队列指标缺失\n".encode("utf-8")
    return PlainTextResponse(header + body, media_type=CONTENT_TYPE_LATEST)


routes: list[Route] = [
    Route("/metrics", metrics_endpoint, methods=["GET", "HEAD"]),
]
