"""工号直取身份（内网环境，不做签名校验）。

运营平台在 iframe src 上带 `?emp=Z064`，前端调本端点换 Cookie。
凭证形态与手工登录完全一致（复用 `_set_auth_cookie`），既有鉴权路径零改动 ——
Cookie 由浏览器自动携带，iframe 内的**导航类请求**（会话导出、报告下载）也天然带身份，
不需要给每个 fetch 挂 Bearer 头。

**首次登录自动建号**（2026-10-28 用户拍板）：工号没建过就当场建一个，别再要求两边
人工同步花名册。建出来的账号是 SSO 专用 —— 密码是随机串（没人知道 ⇒ `/api/auth/login`
对它等同于不存在）、`is_admin` 恒为 False、`must_change_password` 为 False（免登账号
没有改密动作，标 True 遇上强制改密开关就是登录成功却处处 403）。

**自动建号之后紧跟播种**（`agent.auth.provisioning.ensure_sso_defaults`）：把模板账号
（`NL2SQL_SSO_SEED_FROM`）的模型 provider 与库授权复制给这个工号账号。没有这一步，
新账号能进聊天框却发不出话。幂等、开关默认关、失败不影响登录。

安全边界（内网方案的全部防线，改动前先读）：
- **管理员一律拒绝**：本端点免鉴权，而 8080 继续对外 ⇒ 不拦就是「知道管理员工号即可
  内网提权」。管理员只能走入口 A 的账号密码。
- **URL 里的工号是「声明」不是「凭证」**：谁都能手敲 `?emp=Z051` 冒充任意普通用户，
  拿到那个人的历史会话与报告。这是内网部署接受的风险，边界不在代码而在网络层
  （这个域名只对内网开放）。要真正收紧就加 HMAC + 短有效期，前端零改动
  （见 docs/iframe嵌入SSO实施方案.20261028.md §10）。
- **自动建号让「删号」失去停用语义**：删掉的工号下次登录会被重建。当前没有停用
  接口 —— 要停某人只能在网络层拦（或后续补黑名单，见 §10）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent.auth.provisioning import ensure_sso_defaults
from api.auth_routes import _json, _set_auth_cookie, _user_payload

logger = logging.getLogger(__name__)

# 工号格式：Z + 3 位数字。出现第二个前缀只改这一行。
EMP_NO_RE = re.compile(r"^Z\d{3}$")


def _provision(emp_no: str) -> dict | None:
    """按工号建号；已存在（并发首登）则回读。返回不含 password_hash 的记录。

    ⚠️ 同步阻塞（文件写 + PBKDF2 26 万次迭代 ≈ 几十 ms）——调用方必须 `to_thread`，
    否则首登高峰会让所有人的 SSE 跟着排队（P1-14）。
    """
    from agent.auth.users import add_user, find_by_emp_no

    try:
        record = add_user(
            user_id=emp_no,
            password=secrets.token_urlsafe(32),  # 没人知道 ⇒ 只能走 SSO
            display_name=emp_no,
            is_admin=False,
            emp_no=emp_no,                       # ⇒ must_change_password 自动为 False
        )
        logger.info("[sso] 自动建号 emp_no=%s", emp_no)
        return record
    except ValueError as e:
        # 并发：另一个请求刚把这个工号建好 → 回读并沿用（两个首登都成功，不报错）
        record = find_by_emp_no(emp_no)
        if record is not None:
            return {k: v for k, v in record.items() if k != "password_hash"}
        logger.error("[sso] 自动建号失败 emp_no=%s: %s", emp_no, e)
        return None


async def sso_login(request: Request) -> Response:
    """POST /api/sso/login — 工号换登录身份，直接种 HttpOnly Cookie。

    错误一律「返回」带 `error` 的 JSON（抛 HTTPException 会被 langgraph 折成
    `{"detail":...}`，而前端只认 `.error`）。
    """
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid JSON body"}, 400)

    emp_no = str(body.get("emp_no") or "").strip()
    if not EMP_NO_RE.match(emp_no):
        return _json({"error": "invalid emp_no"}, 400)

    from agent.auth.users import find_by_emp_no, token_version_of
    from agent.auth.token import sign_token

    record = find_by_emp_no(emp_no)
    if record is None:
        # 首次登录：当场建号（PBKDF2 是同步 CPU 活，必须离事件循环）
        record = await asyncio.to_thread(_provision, emp_no)
        if record is None:
            return _json({"error": "provision failed"}, 403)

    # 管理员不给免登：本端点免鉴权，8080 又对外 ⇒ 不拦等于内网提权
    if record.get("is_admin"):
        logger.warning("[sso] 拒绝管理员免登: %s", emp_no)
        return _json({"error": "admin requires password"}, 403)

    user_id = record["user_id"]

    # 公有模型 + 固定库：幂等（已配好的账号是空转），失败不能拖垮登录
    try:
        await asyncio.to_thread(
            ensure_sso_defaults, user_id, record.get("emp_no") or emp_no
        )
    except Exception:  # noqa: BLE001
        logger.warning("[sso] 播种默认配置失败 user=%s", user_id, exc_info=True)

    token = sign_token(
        user_id,
        record.get("display_name") or user_id,
        False,
        token_version=token_version_of(record),
    )
    resp = _json({"user": _user_payload(record)})
    _set_auth_cookie(resp, request, token)   # 属性与手工登录逐字一致，杜绝两处漂移
    logger.info("[sso] 工号登录 emp_no=%s user_id=%s", emp_no, user_id)
    return resp


routes = [
    Route("/api/sso/login", sso_login, methods=["POST"]),
]
