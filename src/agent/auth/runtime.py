# -*- coding: utf-8 -*-
"""运行期「调用者身份」取用（唯一实现，2026-09-23 P1-16）。

工具调用发生在 LangGraph 运行时里，拿不到 Starlette 的 `scope["state"]["user"]`，
只能从 `langgraph.config.get_config()["configurable"]` 取身份。本模块把这件事收成
一处，避免每个中间件各写一遍（P1-2 的 `tool_filter` 与 P1-16 的 `query_gate` 都用它）。

**这个值为什么可信任**：外部 run 请求的 `configurable` 由 `LangfuseMetadataMiddleware`
按 AuthMiddleware 校验过的登录身份**覆盖式**写入（P1-2，客户端伪造会被改写并留 warning）；
子 agent / sync 循环由 `deepagents_async_config_patch` 从父 run 透传。所以读到它 =
读到服务端认定的身份，不是客户端说了算。

**fail-open 的边界**（与 `tool_filter._allowed_wrenai_prefixes` 同口径，别改）：
- `is_real_owner()` 为假（`internal` / `dev` / 空）→ 返回 None = **不启用**按用户判权。
  容器内部调用与开发旁路本来就没有用户维度，硬判会把所有内部调用打死。
- 读用户记录（`users.json`）抛异常 → 返回 None（fail-open）+ warning。一次文件 IO
  故障不该让所有人查不了数。（**查得到记录但没授权**不在此列 —— 那是 fail-closed，
  由调用方拿可见库集合去判。）

⚠️ 与 P1-2 的耦合：这条链路的可信度**依赖** `LangfuseMetadataMiddleware` 的覆盖写入。
若那条钳制被回退，本模块的判定就变成"客户端说了算"，等于没判。
"""
from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


def caller_identity() -> str:
    """当前工具/模型调用的登录身份（configurable.user_id）。

    `langgraph_auth_user_id` 是同一身份的别名（sync 循环补写、子 agent 读取），
    两条键名都认，避免某条链路只写了其中一个就判成"无身份"。
    取不到 → 返回 ""（调用方按「不启用判权」处理）。
    """
    try:
        from langgraph.config import get_config as _cfg

        if _cfg is not None:
            c = _cfg().get("configurable", {}) or {}
            return str(c.get("user_id") or c.get("langgraph_auth_user_id") or "")
    except Exception:  # noqa: BLE001  运行时装不上（无 config 上下文等）
        pass
    return ""


def resolve_caller(component: str = "auth.runtime") -> dict[str, Any] | None:
    """解析调用者，供 `grants.can_access_db` / `grants.visible_dbs` 使用。

    返回：
      `None`  —— **不启用**按用户判权（内部调用 / dev 旁路 / 读用户记录失败）；
      `dict`  —— 真实身份，形如 `{"user_id": ..., "is_admin": ...}`。
                 查不到用户记录时返回的是 `is_admin=False` 的 user（可见库为空 →
                 调用方 fail-closed 拒掉），因为"身份真实但账号已被删"不是故障而是结论。
    """
    from agent.auth.ownership import is_real_owner

    uid = caller_identity()
    if not is_real_owner(uid):
        return None

    try:
        from agent.auth.users import find_user

        rec = find_user(uid)
    except Exception:  # noqa: BLE001
        _logger.warning(
            "[auth.runtime] 读取用户记录失败（%s），本次跳过按用户判权", component,
            exc_info=True,
        )
        return None
    return {"user_id": uid, "is_admin": bool((rec or {}).get("is_admin"))}


def caller_can_access_db(user: dict[str, Any], db_name: str, component: str = "auth.runtime") -> bool:
    """`can_access_db` 的读故障包装：读授权表抛异常 → fail-open（True）+ warning。

    与 `tool_filter` 保持同一取舍：读授权本身失败属于"基础设施故障"，此时若 fail-closed
    会让所有人突然查不了数；而"读到了、就是没授权"走 `can_access_db` 的正常 False。
    """
    try:
        from agent.auth.grants import can_access_db

        return bool(can_access_db(user, db_name))
    except Exception:  # noqa: BLE001
        _logger.warning(
            "[auth.runtime] 读取库授权失败（%s，db=%s），本次放行", component, db_name,
            exc_info=True,
        )
        return True
