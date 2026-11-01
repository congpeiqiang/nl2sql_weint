"""工号账号的默认配置播种（公有模型 + 固定库）。

背景（2026-10-28）：运营平台 iframe 用户首次登录即自动建号，但新账号按 09-28 拍板
「不继承任何共享配置」⇒ 空模型、空授权 = 能进聊天框、按发送被 `ModelRequiredMiddleware`
拦下。这里把**一个模板账号**的模型 provider 与库授权复制过去，让「点 AI 就能问」成立。

**为什么用模板账号，而不是 `NL2SQL_SSO_GRANT_DBS=ops_db` 这种 env 写死库名**：
库名是 `db_configs` 里的一条记录，会被改名/重建；写进 env 就多了一处要同步的真相。
模板账号有什么，工号账号就有什么——模型和库共用同一个来源。

**幂等闸（两条，都判「非空就跳过」而不是「缺哪个补哪个」）**：
- 目标已有**任何** provider ⇒ 不播种模型。判「active 是否为空」会冲掉用户的激活选择
  （用户自己加了 provider 但没激活时，active 仍可能为空）。
- 目标已有**任何**一条授权 ⇒ 不播种库。判「这个库没有授权」会导致：管理员撤销公有库后，
  该用户下次登录又被补回来，撤销形同虚设。

**与 09-28 拍板的关系**：那次决定是「新账号不继承大模型」（理由：额度记在别人名下）。
播种等于部分反转它，所以范围必须收窄——只在 `emp_no` 非空（工号账号）且开关打开时生效，
普通新建账号仍然从空开始。开关 `NL2SQL_SSO_SEED_FROM` **默认关**。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def ensure_sso_defaults(user_id: str, emp_no: str) -> None:
    """把模板账号的模型与库授权复制给工号账号。幂等，随时可重入。

    ⚠️ 同步阻塞（文件读写 + SQLite）——调用方必须 `to_thread`（P1-14）。
    """
    template = (os.getenv("NL2SQL_SSO_SEED_FROM", "") or "").strip()
    if not template or not emp_no:
        return  # 开关关 / 非工号账号：一律不碰（保护 09-28 拍板的适用范围）
    if user_id == template:
        return

    from agent.auth.ownership import is_real_owner

    if not is_real_owner(user_id):
        # `get_user_store` 对非真实身份会回退**全局** store —— 往那里写等于改所有人的配置
        return

    from agent.auth.grants import grant_db, list_user_grants
    from agent.settings.model_config_store import get_user_store

    target = get_user_store(user_id)
    source = get_user_store(template)

    if not target.list_configs():
        providers = source.get_all_decrypted()
        for cfg in providers:
            target.upsert(cfg)          # api_key 明文只在进程内过一手，落盘仍是密文
        if providers:
            active = source.get_active()
            if active:
                target.set_active(active)   # upsert 会自动激活第一个，这里对齐模板
            logger.info(
                "[sso] 播种模型 user=%s 来源=%s 共 %d 个 provider",
                user_id, template, len(providers),
            )

    if not list_user_grants(user_id):
        template_grants = list_user_grants(template)
        for g in template_grants:
            grant_db(user_id, g["db_name"], g["level"])
        if template_grants:
            logger.info(
                "[sso] 播种库授权 user=%s 来源=%s 共 %d 个库",
                user_id, template, len(template_grants),
            )
