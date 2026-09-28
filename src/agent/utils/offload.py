# -*- coding: utf-8 -*-
"""P1-14：把同步阻塞调用搬离事件循环的统一入口。

背景（评估报告 §3.1 / §3.3）：10 个后台 job 共用**同一个**事件循环，而
`LANGGRAPH_ALLOW_BLOCKING=true` 把运行时的阻塞检测护栏关掉了 —— 于是"在 async
函数里直接做同步 I/O"会**静默**地把全站串起来：一个用户在跑语义库构建
（wren CLI，最长 600s），其他人的 SSE 与子任务一起卡住。

判据不是"函数看起来像不像 async"，而是"这段代码里有没有 `await`"：本仓大量驱动是
「**async 外壳 + 同步实心**」—— `mcp_server/db_mcp_server/db/engine/*/sql_runner.py`
10 个引擎全都是 `async def run_sql` 而实现里一个 `await` 都没有。所以
`await runner.run_sql(...)` 并不会让出控制权，只会把整条查询挂在循环上。

**两个池，别合成一个**：

  · `offload()`      —— **短**调用（每个请求都走：中间件写库、按请求读表）。
                        走默认执行器（等价于裸用 `asyncio.to_thread`）。
  · `offload_long()` —— **长**调用（wren 构建 180~600s、git push 300s、
                        同步库内省 30s×N、模型探活 30s）。
                        走**独立的**有界池。若两类共用一个池：一条 600s 的构建占满
                        默认池（`min(32, cpu+4)` 个线程）后，每个请求都要走的
                        auth 写库会排队排在它后面 —— 那只是把「卡事件循环」换成了
                        「卡线程池」，而且故障形态更难查（请求全都"慢"但日志无异常）。
                        分开之后，长任务再多也吃不掉短调用的线程。

`asyncio.to_thread` 会自动传播 contextvars（3.9+），所以 worker 里照样能读到
`langgraph.config.get_config()` 的 `user_id` / `thread_id`；`run_in_executor`
**不会**自动传播，`offload_long` 只用于不读请求上下文的调用（子进程、内省、探活）。
"""
from __future__ import annotations

import asyncio
import functools
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")

# 长任务池上限：默认 4。取 4 是"给并发用户留够额度"与"别把机器打满"之间的折中 ——
# 每次 wren 构建都会起子进程、吃几百 MB，N 个用户同时点"语义库更新"时真正该做的是
# 排队（这些入口都是管理员动作，天然低频），而不是并发跑 N 份。
_LONG_WORKERS = max(1, int(os.getenv("NL2SQL_LONG_OFFLOAD_WORKERS", "4")))

# 进程内单例，import 时创建（`ThreadPoolExecutor` 在第一次 submit 之前不建线程，
# 所以空转不占资源）。**不加锁的惰性创建**会有并发双建 + 一个池泄漏 fd 的坑。
_long_pool = ThreadPoolExecutor(max_workers=_LONG_WORKERS, thread_name_prefix="offload-long")


async def offload(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """短阻塞调用 → 默认执行器（等价 `asyncio.to_thread`，保留 contextvars）。"""
    return await asyncio.to_thread(fn, *args, **kwargs)


async def offload_long(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """长阻塞调用（秒级 ~ 十分钟级）→ 独立有界池，不与每请求调用抢线程。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_long_pool, functools.partial(fn, *args, **kwargs))


def long_pool_stats() -> dict[str, int]:
    """给验证脚本/运维看的池状态（`_work_queue` 是私有的，这里只暴露近似值）。"""
    threads = list(_long_pool._threads)  # noqa: SLF001 —— 只读，用于观测
    return {
        "max_workers": _LONG_WORKERS,
        "alive_threads": sum(1 for t in threads if t.is_alive()),
    }
