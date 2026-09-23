"""会话归属（thread owner）——元数据契约与过滤器构造。

契约：**每条 thread 的 metadata 里都必须有 `owner` 键**。

这是 LangGraph 授权过滤器的硬约束：过滤器只能表达「某键等于/包含某值」，
「键不存在」在任何过滤器下都匹配不到（langgraph_runtime_inmem/ops.py 的
`_check_filter_match`：key 不在 metadata 里直接 False）。所以没有 owner 的会话
既过不了过滤、也放不过任何账号 —— 必须有值。

取值：
  <user_id>      正常归属（创建时由 threads.create 钩子按登录身份写入）
  "internal"     后端内部（子 agent / sync 循环 / 索引）创建的会话
  "legacy"       登录体系（2026-09-21 P0）上线前的存量**空壳**会话，
                 按 2026-09-23 拍板对所有人可见

⚠️ 过滤器只能用 `$eq`（简写）与 `$or`：inmem 运行时的 `_check_filter_match`
对**未知操作符是静默忽略 = 直接放行**（没有 else 分支），所以 `$in`/`$ne`
这类写法不但不生效，还会变成越权漏洞。
"""
from __future__ import annotations

OWNER_KEY = "owner"

INTERNAL_IDENTITY = "internal"
"""后端内部调用的身份（backend.py 的 authenticate 无 token 时给出）。"""

LEGACY_OWNER = "legacy"
"""存量空壳会话的哨兵归属，对所有人可见。"""

ADMIN_PERMISSION = "admin"
"""authenticate 返回的 permissions 里的管理员标记。"""


def owner_filter(identity: str) -> dict:
    """列表/单条访问的归属过滤器：仅自己 + legacy 存量。"""
    return {"$or": [{OWNER_KEY: identity}, {OWNER_KEY: LEGACY_OWNER}]}


def owner_of(user_metadata: dict | None) -> str:
    """读出一条 thread 的归属（缺失返回空串）。"""
    if not isinstance(user_metadata, dict):
        return ""
    value = user_metadata.get(OWNER_KEY)
    return value if isinstance(value, str) else ""


# ── 「真实用户」判定（服务端补打归属时用）──────────────────────────
#
# 容器内部调用（子 agent / sync 循环 / 自定义 API 自调用）在 AuthMiddleware 与
# backend.authenticate 里都被标识成 internal，但**它们往往是代某个真实用户干活**
# （请求体 configurable 里带着父 run 的用户）。归属必须记真实用户：把子 agent 线程
# 记成 internal 的后果是「用户自己都读不到自己的子线程」（前端 getState 会被 404）。

NON_USER_IDENTITIES = frozenset({INTERNAL_IDENTITY, "dev"})
"""不是真实用户的身份：internal=容器内部，dev=NL2SQL_AUTH_DISABLED 开发旁路。"""


def is_real_owner(value: object) -> bool:
    """该值能不能当归属人（真实用户 id）。空 / internal / dev / legacy 都不行。"""
    return (
        isinstance(value, str)
        and bool(value)
        and value not in NON_USER_IDENTITIES
        and value != LEGACY_OWNER
    )


# ── 进程内「已打标」集合 ──────────────────────────────────────
#
# 打标有两处：threads.create 钩子（POST /threads，零成本）与 langfuse_metadata
# 中间件的补打（覆盖子 agent 线程这类不经钩子的创建路径，代价是一次内部 PATCH）。
# 用这个集合把补打限制成「每线程每进程一次」，避免每个 run 都白写一遍 metadata。

_STAMPED: set[str] = set()
_STAMPED_CAP = 20000


def mark_stamped(thread_id: str) -> None:
    if len(_STAMPED) >= _STAMPED_CAP:
        _STAMPED.clear()  # 长跑进程防无界增长（清空只是多打几次标，无害）
    _STAMPED.add(thread_id)


def was_stamped(thread_id: str) -> bool:
    return thread_id in _STAMPED
