# -*- coding: utf-8 -*-
"""部署默认配置 API（前端首屏「零配置」用，2026-09-28）。

路由（挂载见 ``src/api/custom_app.py``）：

- ``GET /api/deployment-info`` → 本部署的默认「助手 ID」+ 可用图名列表

背景：前端首屏原本要求用户**手填两个字段**才能进聊天页
（`ConfigDialog`，且在 `AuthGuard` 之外 ⇒ 未登录就先问配置）。而这两个值
**没有一个需要用户提供**：

  · 部署 URL —— 前端留空即跟随当前访问地址（`lib/deploymentUrl.ts`），
    填了反而容易踩坑（填成 `:2026` 那个只绑回环的端口 = 「模型都没了 + Failed to fetch」）；
  · 助手 ID —— 就是本部署的**图名**，常量，写在仓库根 ``langgraph.json`` 的 ``graphs`` 里。

本端点把「助手 ID」交给前端 ⇒ 登录后可直接进聊天页，配置弹窗降级为
「探测失败时的兜底」。**只读、需登录**（刻意不进 ``auth_middleware`` 白名单：
登录前没有任何理由暴露图名，而前端要把探测放在 ``AuthGuard`` 之后正是为了拿到 cookie）。

⚠️ ``assistant_id`` 的语义：它可以是 LangGraph 的 **graph id**（如 ``chat_agent``），
不必是某个 assistant 的 UUID —— 前端 ``fetchAssistant`` 对非 UUID 会走
``assistants.search({graphId})`` 找默认 assistant（见 ``src/app/page.tsx``）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response, require_user

_logger = logging.getLogger(__name__)

# 主入口图名。**兜底值**：正常路径从 langgraph.json 读，读不到才用它。
# 与 ``langgraph.json`` 的 ``graphs`` 和 ``src/agent/subagents/sync_subagent_todos.py``
# 里内调 API 用的 ``assistant_id="chat_agent"`` 必须一致。
_DEFAULT_ASSISTANT_ID = "chat_agent"


def _graph_ids(path: Path | None = None) -> list[str]:
    """读 ``langgraph.json`` 的图名列表。任何异常 → ``[]``（fail-open）。

    路径：本文件是 ``<root>/src/api/deployment_info.py`` ⇒ ``parents[2]`` 就是仓库根，
    在容器里等于 ``/app``（Dockerfile ``WORKDIR /app`` + ``COPY . .``）⇒
    ``/app/langgraph.json``，与本地开发同构。

    ``path`` 只为验收脚本可注入（默认 None = 真实路径）。
    """
    try:
        path = path or (Path(__file__).resolve().parents[2] / "langgraph.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        graphs = data.get("graphs")
        if not isinstance(graphs, dict):
            raise ValueError("langgraph.json 缺 graphs 段")
        return [str(k).strip() for k in graphs if str(k).strip()]
    except Exception as e:  # noqa: BLE001  只读部署元信息，绝不能让它打断首屏
        _logger.warning("[deployment-info] 读取 langgraph.json 失败，回落常量: %s", e)
        return []


async def deployment_info(request: Request) -> None:
    """GET：返回默认助手 ID + 图名列表。需登录。"""
    require_user(request)

    graph_ids = _graph_ids()
    if graph_ids:
        # 主入口优先；它被别人改名/换图时，退而取第一个图名（自愈，不用改前端）
        assistant_id = (
            _DEFAULT_ASSISTANT_ID if _DEFAULT_ASSISTANT_ID in graph_ids else graph_ids[0]
        )
        source = "langgraph.json"
    else:
        assistant_id = _DEFAULT_ASSISTANT_ID
        source = "fallback"

    return json_response(
        {
            "ok": True,
            "assistant_id": assistant_id,
            "graph_ids": graph_ids,
            "source": source,
        }
    )


# ── 路由表（custom_app.py 聚合）──────────────────────────
routes: list[BaseRoute] = [
    Route("/api/deployment-info", deployment_info, methods=["GET"]),
]
