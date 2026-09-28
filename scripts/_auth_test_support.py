# -*- coding: utf-8 -*-
"""验证脚本共用的鉴权测试夹具（**不是生产代码**，只在 scripts/ 下用）。

存在理由（P1-12，2026-09-23）：`verify_token` 改成**以用户记录为唯一真源**之后，
"凭空签一个 token 冒充某用户" 这种测试手法不再成立 ——

    sign_token("alice", ...)  →  verify_token →  find_user("alice") → None → 401

这正是我们想要的生产行为（记录没了 / 改过密码 / 被吊销 → 立即失效），但它会把
"身份是合成出来的"那些脚本一次性打红（实测：verify_endpoint_guards 30→7、
verify_report_ownership 40→25、verify_thread_ownership 29→27，表现全是 401）。
所以测试要**先把身份登记出来**再签 token —— 等价于生产里"管理员建号 → 用户登录"。

`is_admin` 同样以记录为准（token 里的 is_admin 已不再被采信），所以夹具会在
传参不一致时**改记录**，而不是指望 token 参数生效。
"""
from __future__ import annotations

_TEST_PASSWORD = "test-pass-1234"  # ≥ MIN_PASSWORD_LENGTH(8)


def ensure_auth_user(uid: str, is_admin: bool = False) -> int:
    """保证 `uid` 在 auth_users.json 里存在且 is_admin 一致 → 返回其 token_version。

    幂等：已存在时只按需改 is_admin（不乱动密码/版本号，否则会把"吊销类"断言搅乱）。
    """
    from agent.auth.users import (
        add_user,
        find_user,
        token_version_of,
        update_user,
    )

    record = find_user(uid)
    if record is None:
        try:
            add_user(uid, _TEST_PASSWORD, uid, is_admin=is_admin, must_change_password=False)
        except ValueError:
            # 并发/重复建号：重新读一次即可
            record = find_user(uid)
            if record is None:
                raise
        record = find_user(uid)
    elif bool(record.get("is_admin", False)) != bool(is_admin):
        # 记录是权威 → 改记录（改 is_admin 不吊销 token：update_user 只对 password 动版本号）
        record = update_user(uid, is_admin=is_admin, must_change_password=False) or find_user(uid)
    return token_version_of(record)


def mint_for(uid: str, is_admin: bool = False) -> str:
    """登记身份 + 签发 token（等价于生产里"该用户登录过一次"）。"""
    from agent.auth.token import sign_token

    version = ensure_auth_user(uid, is_admin)
    return sign_token(uid, uid, is_admin, token_version=version)
