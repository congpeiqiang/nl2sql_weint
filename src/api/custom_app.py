"""langgraph API 自定义 app 组合根（注册表）。

被 `LANGGRAPH_HTTP.app` 钩子加载（见 .env），与 langgraph 原生路由合并为同一
Starlette app、同一进程/端口（2026），不改 langgraph-api 源码。

新增自定义 API：在 `src/api/` 建模块暴露 `routes: list[BaseRoute]`，
并在下方 `ROUTES` 里展开一行即可（组合根模式，避免堆进单个文件）。
"""
import logging
import os
import sys
from contextlib import asynccontextmanager  # noqa: E402

# 防御性插 src 进 sys.path：start_server.py 已插；此处兜底 langgraph dev 等
# 未主动插 src 的启动方式，保证 api/mcp_server 包可导入。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.cors import CORSMiddleware  # noqa: E402
from starlette.routing import BaseRoute  # noqa: E402

_logger = logging.getLogger(__name__)

import api.auto_title  # noqa: E402
import api.db_config  # noqa: E402
import api.deployment_info  # noqa: E402
import api.drain  # noqa: E402
import api.metrics  # noqa: E402
import api.request_log  # noqa: E402

import api.eval_flags  # noqa: E402
import api.langfuse_metadata  # noqa: E402
import api.message_feedback  # noqa: E402
import api.model_config  # noqa: E402
import api.report_file  # noqa: E402
import api.sql_approval  # noqa: E402
import api.task_cancel  # noqa: E402
import api.thread_compact  # noqa: E402
import api.thread_delete  # noqa: E402
import api.thread_export  # noqa: E402
import api.thread_fork  # noqa: E402
import api.thread_run_status  # noqa: E402
import api.thread_search  # noqa: E402
import api.wren_semantic  # noqa: E402
import api.trace_routes  # noqa: E402
import api.feedback_stats  # noqa: E402
import api.feedback_annotation  # noqa: E402
import api.experiment  # noqa: E402
import api.auth_routes  # noqa: E402
import api.auth_middleware  # noqa: E402
import api.auth_admin  # noqa: E402
import api.scheduled_tasks  # noqa: E402

# ⚠️ 模块级别名，专供 `_lifespan` 用：函数体里那句 `import api.drain` 会让 `api`
# 变成**局部名**（import 也是赋值），在它之前写 `api.metrics.xxx` 会 UnboundLocalError
# —— 启动即崩，但只有真跑 lifespan 才暴露（verify_metrics.py 第 ⑧ 段正是照着这个打）。
_metrics = api.metrics

ROUTES: list[BaseRoute] = [
    *api.auth_routes.routes,
    *api.auth_admin.routes,
    *api.drain.routes,          # P2-1 优雅停机（admin 触发排空/查状态/撤销）
    *api.metrics.routes,        # P2-3 /metrics（指标 + ?format=json 队列快照）
    *api.db_config.routes,
    *api.deployment_info.routes,   # 前端首屏零配置：默认助手 ID（需登录）
    *api.message_feedback.routes,
    *api.auto_title.routes,
    *api.model_config.routes,
    *api.report_file.routes,
    *api.sql_approval.routes,
    *api.task_cancel.routes,
    *api.thread_compact.routes,
    *api.thread_delete.routes,     # 删会话 = 取消这次 run + 删注册表（定时下轮照跑）
    *api.thread_export.routes,
    *api.thread_fork.routes,
    *api.thread_run_status.routes,
    *api.thread_search.routes,
    *api.wren_semantic.routes,
    *api.trace_routes.routes,
    *api.feedback_stats.routes,
    *api.feedback_annotation.routes,
    *api.experiment.routes,
    *api.eval_flags.routes,
    *api.scheduled_tasks.routes,  # 定时任务（cron/interval/once → 服务端自建 run）
    # 后期新增：import api.<name> + 展开 *api.<name>.routes
]

# M2 监控增强：langgraph server 会提取 custom_app 的 user_middleware 全局应用
# （langgraph_api/server.py），对 run 创建端点注入 Langfuse config.metadata。
# 纯 ASGI 中间件，不缓冲响应（不破坏 /runs/stream 的 SSE）。


@asynccontextmanager
async def _lifespan(app: "Starlette"):
    """自定义 app 生命周期——langgraph server 会把它并入进程停机路径
    （langgraph_api/server.py combine_lifespans，shutdown 逆序执行）。

    停机顺序（P2-1 起，顺序本身是设计的一部分）：
      ① **排空**（`api.drain`）：等"本实例在跑的后台任务数"清零（**计数按 edition 分派** ——
         inmem 走 `langgraph_runtime_inmem.queue.get_num_workers()`、postgres 走
         `langgraph_runtime.metrics.get_metrics()['workers']['active']`，见 `api/drain._read_workers`），
         预算 `NL2SQL_DRAIN_SECS`（默认 180，0 = 不等）。**必须在 langgraph 自己的
         停机之前**——它的 inmem 排空窗口是硬编码 5 秒，之后就是进程退出，我们插不进手。
         本 lifespan 比基础 runtime 的 lifespan 先退出（AsyncExitStack 逆序），所以这里是
         唯一的插入点。详见 `api/drain.py` 文件头。
      ② **停机 flush**（P0 评估可靠交付，设计文档 §8.2）：显式刷新 Langfuse SDK 上送队列，
         避免进程退出丢最近窗口的分数/trace。放在排空**之后**：让最后一批 run 的 trace
         也进入本次 flush（反过来的话最后几秒的分数只能靠 atexit 兜底）。
    不重复任何启动逻辑（base runtime 的 lifespan 由 server 自行管理）。两步都不抛异常、不阻断停机。

    P2-3 插了两件事（都不改上面两条的顺序）：**yield 之前**起指标采样任务（`api.metrics`，
    否则进程起来后前 10 秒面板是空的）；**flush 之后**停它（排空期间还要看得见队列在往下掉，
    所以不能提前停）。采样失败只 warning、不影响服务也不阻断停机。

    P2-4 同理插一件事：**yield 之前**起子任务终态补写器（`pending_terminal`）。它把
    "watcher 因主线程忙（update_state 硬 409）而放手的终态"补写回主线程 state ——
    启动即跑一轮，所以**进程重启后残留的待补写行会立刻落地**（这正是僵尸 run 场景
    唯一的恢复路径：pckl 里的恒 running run 被重启清掉后线程才写得进去）。

    P2-5 再插一件：**yield 之前**起保留策略维护线程（`agent.utils.retention`，按龄清
    trace_events/eval_queue/工作区中间产物 + 量各区域占用给 `/metrics`），停机时停它。
    同样是**后台线程 + 同步工作**：文件遍历与 DELETE 绝不能落在事件循环上（P1-14）。

    2026-10-13 再插一件（排在维护线程之后）：**嵌入通道保活线程**（`agent.retrieval.embedder`，
    每 5 分钟打一次真实嵌入请求、单次超时 120s）。理由见 `embedder.py` 末尾「后台预热」节：
    查询期的 4s×2 ≈ 8.25s 跑不完一次 13~17s 的 GPU 冷加载，而且**客户端掐断时 Ollama 会把
    这次加载一并放弃** ⇒ 没有这条路径，`.13` 那边重建 `bgem3` 容器之后问数就**永久**降级
    纯 FTS（2026-10-11 发生过、10-13 才被发现）。`NL2SQL_RETRIEVAL!=hybrid` 或
    `NL2SQL_EMBED_WARMUP_INTERVAL<=0` 时不启动。

    2026-09-25 再插一件（**最靠前**）：运行时库归位 —— `eval_queue` / `trace_bind` /
    `pending_terminal` 三件套从数据根目录搬进各自的同名子目录（`agent/utils/sqlite_paths.py`）。
    必须在 `start_reaper` / `start_maintenance` **之前**：它俩会建连，而搬运的前提是
    「没人打开过那份老库」。失败的后果只是老文件留在根上（新库照常在子目录里建），不拦启动。
    """
    try:
        from agent.utils.sqlite_paths import adopt_all_stores  # 惰性 import

        _moved = [k for k, v in adopt_all_stores().items() if v]
        if _moved:
            _logger.info("[custom_app] 运行时库已归位到同名子目录: %s", ", ".join(_moved))
    except Exception as e:  # noqa: BLE001  归位失败不该拦住服务启动
        _logger.warning("[custom_app] 运行时库归位失败: %s", e)
    _metrics.start()
    try:
        from agent.subagents.pending_terminal import start_reaper  # 惰性 import

        start_reaper()
    except Exception as e:  # noqa: BLE001  补写器起不来不该拦住服务启动
        _logger.warning("[custom_app] 启动终态补写器失败: %s", e)
    try:
        from agent.utils.retention import start_maintenance  # P2-5，惰性 import

        start_maintenance()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[custom_app] 启动保留策略维护失败: %s", e)
    # 2026-10-13：嵌入通道**保活线程**。冷加载 13~17s 跑不完查询期的 4s×2 预算，且客户端
    # 掐断会让 Ollama 连加载一起放弃（自锁）⇒ 必须有一条允许跑完的后台路径，否则一旦
    # `.13` 重建 `bgem3` 容器，问数就永久降级纯 FTS（10-11 发生过，10-13 才被发现）。
    # 与上面三件同款：yield 前起、停机停；起不来只 warning，绝不拦启动。
    try:
        from agent.retrieval.embedder import start_warmer

        start_warmer()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[custom_app] 启动嵌入预热失败: %s", e)
    # 定时任务调度 worker（方案 docs/NL2SQL定时任务实现方案.md §3）：60s tick、
    # CAS at-most-once、`.tick.lock` 防蓝绿双色双触发。必须在 adopt_all_stores
    # 之后（它会建 scheduled_tasks 库连接）。同款纪律：起不来只 warning，不拦启动。
    try:
        from agent.scheduler.worker import start_worker

        start_worker()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[custom_app] 启动定时任务调度器失败: %s", e)
    yield
    # 停机段**最先**停调度器：排空窗口内不再产生新的定时 run（方案 §3）。
    try:
        from agent.scheduler.worker import stop_worker

        stop_worker()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[custom_app] 停定时任务调度器失败: %s", e)
    try:
        import api.drain  # 惰性 import：与 langgraph 的 import 顺序解耦

        api.drain.begin_drain(reason="lifespan-shutdown")
        await api.drain.wait_for_idle()
    except Exception as e:  # noqa: BLE001  排空失败不该阻断停机
        _logger.warning("[custom_app] 停机排空失败（继续停机）: %s", e)
    try:
        from agent.trace.langfuse_client import flush_langfuse  # 惰性 import

        flush_langfuse()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[custom_app] 停机 flush Langfuse 失败: %s", e)
    # P2-4：停补写器（残留行在 SQLite 里，下次启动第一轮立刻重放）。
    # 放最后：排空/flush 期间它还在跑，正是"让最后几秒的终态尽量落地"的时机。
    try:
        from agent.subagents.pending_terminal import stop_reaper

        stop_reaper()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[custom_app] 停终态补写器失败: %s", e)
    # P2-5：停保留策略维护线程（没跑完的那一轮下次启动继续，不留半截状态）
    try:
        from agent.utils.retention import stop_maintenance

        stop_maintenance()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[custom_app] 停保留策略维护失败: %s", e)
    # 嵌入保活线程：daemon，无落盘状态，join 等不到在飞的那一轮（最长 120s）是正常的
    try:
        from agent.retrieval.embedder import stop_warmer

        stop_warmer()
    except Exception as e:  # noqa: BLE001
        _logger.warning("[custom_app] 停嵌入预热失败: %s", e)
    await _metrics.stop()


# CORS 允许的前端 Origin（逗号分隔）。未设置时使用开发默认值。
# 生产环境应在 .env 或 compose 中配置 NL2SQL_CORS_ORIGINS。
_CORS_ORIGINS = [
    o.strip()
    for o in (
        os.environ.get("NL2SQL_CORS_ORIGINS") or
        "http://localhost:3000,http://localhost:8080,http://192.168.25.64:8080,http://192.168.25.34:8080"
    ).split(",")
    if o.strip()
]

app = Starlette(
    routes=ROUTES,
    middleware=[
        # CORS 必须最外层：跨域 cookie 需要 Allow-Credentials + 精确 Origin
        Middleware(
            CORSMiddleware,
            allow_origins=_CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        ),
        # P2-2：请求级 rid + 访问日志。位置=CORS 之内、DrainGate/Auth 之外 ——
        # 这样 503（排空中）/401/403 各有一行 access 日志可查，且响应头总能回显 rid。
        # 与 DrainGate 同理，langgraph 会把自定义中间件提到全局，原生 /threads/.../runs/stream
        # 也在覆盖范围内（前端真正提交 run 的那条路径）。
        Middleware(api.request_log.RequestContextMiddleware),
        # P2-1：排在 Auth 之前——排空期间"拒绝新 run"与调用者是谁无关，先拒就不用做鉴权；
        # 且 langgraph 把自定义 app 的中间件提到全局（server.py），原生 /threads/{tid}/runs/stream
        # 也在拦截范围内（那才是前端真正提交 run 的路径）。
        Middleware(api.drain.DrainGateMiddleware),
        Middleware(api.auth_middleware.AuthMiddleware),
        Middleware(api.langfuse_metadata.LangfuseMetadataMiddleware),
    ],
    lifespan=_lifespan,
)
