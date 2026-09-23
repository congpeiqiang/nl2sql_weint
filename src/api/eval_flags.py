# -*- coding: utf-8 -*-
"""在线评估开关 API（前端「设置 → 评估」，2026-09-09）。

路由（挂载见 ``src/api/custom_app.py``）：

- ``GET    /api/eval-flags`` → 四个开关的生效值 + 来源 + 代码默认
- ``PUT    /api/eval-flags`` body ``{"flags": {<键>: <值>, ...}}`` → 增量改这些键的覆盖
  （值传空串 = 删除该键的覆盖，即单独「恢复默认」；也容忍扁平写法 ``{"<键>": <值>}``）
- ``DELETE /api/eval-flags`` → 清空全部覆盖项（所有开关回到 .env / 默认）

写入即生效：覆盖层落盘到 ``{AGENT_DATA_ROOT}/shared/eval_flags.json``，评估器全部
读时求值 → 下一次查询 / 下一轮 judge 队列轮询即生效，**无需重启后端**。

只覆盖传入的键；未传入的键继续跟随 .env（前端「恢复默认」= DELETE）。
每次变更记 WARNING 审计日志（时间 / 来源 IP / 生效值前后差异 / 落盘覆盖项）。
"""
from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response, parse_body, require_admin

_logger = logging.getLogger(__name__)


def _client_ip(request: Request) -> str:
    """取真实来源 IP（反代下优先 X-Forwarded-For 首段）。"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or "-"


def _payload() -> dict:
    """响应体：生效值（含来源）+ 当前覆盖项 + 覆盖层文件路径。"""
    from agent.eval import eval_flags_store as store
    from agent.eval.eval_flags import snapshot

    return {
        "ok": True,
        "flags": snapshot(),
        "overrides": store.overrides(),
        "file": str(store.path()),
    }


def _audit(action: str, request: Request, before: dict, after_overrides: dict) -> None:
    """变更审计：来源 IP + 生效值前后差异 + 落盘覆盖项。"""
    from agent.eval.eval_flags import snapshot

    after = snapshot()
    diff = [
        f"{k}: {before[k]['value']}({before[k]['source']}) → "
        f"{after[k]['value']}({after[k]['source']})"
        for k in after
        if k in before
        and (before[k]["value"] != after[k]["value"] or before[k]["source"] != after[k]["source"])
    ]
    _logger.warning(
        "[eval-flags] %s ip=%s 变更=[%s] 覆盖项=%s",
        action,
        _client_ip(request),
        "; ".join(diff) or "无生效值变化",
        after_overrides or "（空，全部跟随 .env）",
    )


async def eval_flags(request: Request) -> None:
    """GET（读）/ PUT（写）/ DELETE（清空）。仅管理员可访问。"""
    require_admin(request)
    from agent.eval import eval_flags_store as store

    method = request.method.upper()

    if method == "GET":
        return json_response(_payload())

    if method == "DELETE":
        before = _payload()["flags"]
        store.clear_overrides()
        _audit("清空覆盖", request, before, {})
        return json_response(_payload())

    # PUT：优先取 {"flags": {...}}；也容忍扁平写法 {"<开关名>": <值>}
    data = await parse_body(request)
    flags = data.get("flags")
    if flags is None and data and all(k in store.KEYS for k in data):
        flags = data
    if not isinstance(flags, dict) or not flags:
        return json_response(
            {"ok": False, "error": "body 需为 {\"flags\": {<开关名>: <值>}}"}, status=400
        )
    before = _payload()["flags"]
    try:
        saved = store.patch_overrides(flags)
    except ValueError as e:
        return json_response({"ok": False, "error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001
        _logger.error("[eval-flags] 写入覆盖层失败: %s", e, exc_info=True)
        return json_response({"ok": False, "error": f"写入失败: {e}"}, status=500)
    _audit("更新覆盖", request, before, saved)
    return json_response(_payload())


# ── 路由表（custom_app.py 聚合）──────────────────────────
routes: list[BaseRoute] = [
    Route("/api/eval-flags", eval_flags, methods=["GET", "PUT", "DELETE"]),
]
