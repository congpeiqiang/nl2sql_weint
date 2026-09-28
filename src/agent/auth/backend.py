"""Langgraph 官方 custom auth 适配（覆盖 /threads、/runs/* 等原生路由）。

通过 graph.json 的 auth 键加载：
  "auth": {"path": "./src/agent/auth/backend.py:auth"}

与 auth_middleware.py 共用 verify_token()，逻辑不重复。

本模块同时承担**会话归属隔离**（2026-09-23 加）：
原生 `/threads/search`（侧边栏列表）、`/threads/{tid}/state|history`、
`/threads/{tid}/runs/stream` 等路由只有认证、没有授权，任何登录账号都能看到
所有人的会话（生产 64 上实测：Z0051 与 admin 各自 search 都返回 268 条、集合完全相同）。
授权钩子在 **ops 层**执行（不是 ASGI 中间件层），因此只覆盖原生路由；
自定义路由（src/api/*）不走这里，得各自校验。
"""
from __future__ import annotations

import logging

from langgraph_sdk import Auth

from agent.auth.grants import claim_thread
from agent.utils.offload import offload
from agent.auth.ownership import (
    ADMIN_PERMISSION,
    INTERNAL_IDENTITY,
    OWNER_KEY,
    mark_stamped,
    owner_filter,
)
from agent.auth.token import extract_token_from_headers, verify_token

logger = logging.getLogger(__name__)

auth = Auth()


@auth.authenticate
async def authenticate(headers: dict) -> dict:
    """从 Cookie/Bearer 提取 token → 校验 → 返回 user dict。

    langgraph SDK 自动把 headers dict 传进来（key 已小写）。
    返回的 dict 会被 langgraph 转成 AuthCredentials + BaseUser。
    identity → 用户唯一标识；display_name → 显示名。
    注：LangGraph auth 结果不会自动注入 configurable，
    langfuse_metadata 中间件通过 scope["state"]["user"] 读取用户身份。
    """
    # headers 可能是 {str: str} 或 {bytes: bytes}，统一处理
    str_headers: dict[str, str] = {}
    for k, v in headers.items():
        sk = k.decode("utf-8") if isinstance(k, bytes) else k
        sv = v.decode("utf-8") if isinstance(v, bytes) else v
        str_headers[sk.lower()] = sv

    token = extract_token_from_headers(str_headers)
    if not token:
        # 内部请求旁路：无 token + 无 X-Forwarded-For = 来自 Docker 内部（如 sync 循环）
        # nginx 转发的外部请求一定会带 X-Forwarded-For
        if not str_headers.get("x-forwarded-for"):
            return {
                "identity": INTERNAL_IDENTITY,
                "display_name": "Internal",
                "permissions": [],
            }
        raise Auth.exceptions.HTTPException(
            status_code=401, detail="Missing authentication token"
        )

    user = verify_token(token)
    if not user:
        raise Auth.exceptions.HTTPException(
            status_code=401, detail="Invalid or expired token"
        )

    # langgraph SDK 要求返回 dict，会自动包装成 BaseUser
    # "identity" → 用户唯一标识（必填）
    # "display_name" → 显示名（选填）
    # "permissions" → 授权钩子里用 ctx.permissions 读（**必须显式返回**：
    #   ProxyUser 没有 permissions 属性，dict 里没这个键时 ctx.user.permissions
    #   会抛 AttributeError）
    return {
        "identity": user["user_id"],
        "display_name": user["display_name"],
        "permissions": [ADMIN_PERMISSION] if user.get("is_admin") else [],
    }


# ── 会话归属授权（LangGraph 官方资源授权钩子） ──────────────────────
#
# 分工（同一 (resource, action) 只会执行最具体的那一个 handler）：
#   on.threads.create   → 打归属标记（改 value["metadata"] 是官方手段，
#                          inmem 下 value["metadata"] 与请求 metadata 是同一对象）
#   on.threads.search   → 列表过滤（**对所有人，含 admin**：admin 是库/模型维度的
#                          超管，但列表里不该看到别人的会话，见 2026-09-23 反馈）
#   on.threads.update   → 归属过滤器 + 归属键不可改（否则客户端能把自己会话改成
#                          legacy / 塞进他人侧边栏）
#   on.threads          → 兜住其余动作（read/delete/create_run）：
#                          归属不符 → 404（不是 403，ops 层统一按「取不到」处理）


def _identity(ctx) -> str:
    user = getattr(ctx, "user", None)
    ident = getattr(user, "identity", "") if user is not None else ""
    return ident or ""


def _is_internal(ctx) -> bool:
    return _identity(ctx) == INTERNAL_IDENTITY


def _is_admin(ctx) -> bool:
    perms = getattr(ctx, "permissions", None) or ()
    return ADMIN_PERMISSION in perms


@auth.on.threads.create
async def _stamp_thread_owner(ctx, value):
    """建会话时把归属写进 metadata **和 grants 账本**（新会话从此有主）。

    internal 身份不改动（后端可能代用户写：比如迁移/claim 路径已经把 owner 算好）。

    P1-5：这里同时 `claim_thread`。原先 grants 行只在**建 run 时**才写，于是
    「建了但还没跑过」的会话在 REST 层是未登记状态——`owned_thread` 改 fail-closed 后
    那种会话的合法主人自己会被 403（trace/feedback/export/run-status）。
    两套账本（metadata.owner / grants.thread_owner）必须在**同一个入口**一起写，
    否则迟早出现「列表里看得见、点进去 403」。
    """
    if _is_internal(ctx):
        return None
    identity = _identity(ctx)
    if not identity or not isinstance(value, dict):
        return None
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        value["metadata"] = metadata
    # 强制覆盖：不让请求方自己指定归属（否则可把会话塞进别人的列表）
    metadata[OWNER_KEY] = identity
    thread_id = value.get("thread_id")
    if thread_id:
        # 记进进程内集合：langfuse_metadata 中间件据此跳过重复补打
        mark_stamped(str(thread_id))
        # grants 账本同写（best-effort：账本写失败不该让建会话失败，
        # langfuse_metadata 在第一个 run 上还会补一次）
        try:
            # P1-14：sqlite 写（fsync）→ 线程。这是**建会话**的必经路径（auth 钩子），
            # 每个新会话一次；挂在事件循环上等于每次建会话都让全站等一次 fsync。
            await offload(claim_thread, str(thread_id), identity)
        except Exception:  # noqa: BLE001
            logger.warning("[auth] claim_thread 失败 tid=%s", thread_id, exc_info=True)
    return None


@auth.on.threads.search
async def _filter_thread_list(ctx, value):
    """侧边栏列表只返回「自己的 + legacy 存量」。"""
    if _is_internal(ctx):
        return None
    identity = _identity(ctx)
    if not identity:
        return None  # 无身份（理论上到不了这里）→ 交给上层 401
    return owner_filter(identity)


@auth.on.threads.update
async def _guard_thread_update(ctx, value):
    """改 metadata：一是不能改别人的会话，二是**不能改归属**。

    只做 `_guard_thread_access` 那一层会漏一个洞：归属键 `owner` 本身就在
    metadata 里，客户端 PATCH 自己的会话时能把 owner 改成 `legacy`（= 全站可见）
    或别人的 user_id（= 塞进别人侧边栏）。所以这里既返回归属过滤器（不是自己的
    改不到），又把传进来的 owner 强制压回登录身份。
    管理员例外：admin 可以改别人的会话，此时不动 owner（避免管理操作顺手偷归属）。
    """
    if _is_internal(ctx):
        return None
    identity = _identity(ctx)
    if _is_admin(ctx) or not identity:
        return None
    if isinstance(value, dict):
        metadata = value.get("metadata")
        if isinstance(metadata, dict) and OWNER_KEY in metadata:
            metadata[OWNER_KEY] = identity
    return owner_filter(identity)


@auth.on.threads
async def _guard_thread_access(ctx, value):
    """read / delete / create_run：不是自己的会话就取不到。"""
    if _is_internal(ctx) or _is_admin(ctx):
        return None
    identity = _identity(ctx)
    if not identity:
        return None
    return owner_filter(identity)
