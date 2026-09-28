# -*- coding: utf-8 -*-
"""P1-12 凭据加固（离线，无后端/数据库/网络/LLM；用真 Starlette app + 真中间件）。

**被测的四件事**：

  ① **密码哈希**：单轮无盐 SHA-256 → PBKDF2-HMAC-SHA256 + 每用户随机盐 + 26 万次迭代。
     判据是行为不是常量：**同一密码两次哈希必须不同**（有盐）、旧格式仍能登录、
     登录成功时就地升级且**不影响在线会话**（不动 token_version）。
  ② **token 吊销**：改密 / 删号 / 显式吊销 → 该账号此前签发的 token **立即**失效；
     登录**不**吊销（多设备并存）。判据里最要紧的一条是**发版不踢人**：老 token 没有
     `pv`、老记录没有 `token_version`，两边都按 0 对齐 → 升级那一刻谁都不掉线。
  ③ **首登强制改密（标记）**：默认管理员的密码是公开已知的 `admin123` → 记录带
     `must_change_password`，登录响应与 `/api/auth/me` 都吐出来；自助改密后清除。
     拦截默认**关**（`NL2SQL_FORCE_PASSWORD_CHANGE`），开了以后必须**留出自解套的路**
     （me / change-password / logout）——这条用真请求验，因为"少放一个路径=用户被锁死"
     是这里唯一不可回的错。
  ④ **Cookie `Secure`**：本环境是明文 HTTP，无条件 `secure=True` 会让浏览器丢掉 Cookie
     （登录成功但全是 401）。所以按请求判定（scheme / X-Forwarded-Proto）+ env 覆盖。

**负对照是破坏性的**（每条都真的把防护拆掉，断言必须翻转）：
拆 `token_version` 比较 → 已吊销的 token 复活；拆哈希校验 → 错密码也能登录。

用法（脚本自建临时 AGENT_DATA_ROOT，不碰真实数据）：
    ./.venv/Scripts/python.exe scripts/verify_auth_hardening.py
退出码 0 = 全部通过。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

os.environ.setdefault("AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-auth-"))
os.environ.pop("NL2SQL_AUTH_DISABLED", None)
os.environ.pop("NL2SQL_FORCE_PASSWORD_CHANGE", None)
os.environ.pop("NL2SQL_COOKIE_SECURE", None)
# 别让测试触发真实 Langfuse/tracing
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
os.environ.setdefault("LANGSMITH_TRACING", "false")

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
for cand in (_ROOT / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

# Windows 控制台默认 GBK，本脚本输出含中文 → 强制 utf-8（否则 print 直接崩）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []

ADMIN_PW = "admin123"
NEW_PW = "Str0ng-Passw0rd!"


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ── 测试用 app（真中间件 + 真路由）──────────────────────────

def build_client():
    """AuthMiddleware 包住 auth 路由 + 一个受保护的假端点。"""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from api import auth_admin, auth_routes
    from api.auth_middleware import AuthMiddleware

    async def ping(request):
        return JSONResponse({"ok": True, "user": request.state.user["user_id"]})

    async def boom(request):
        raise RuntimeError("这个端点不该被调到（未改密时应当已被 403 拦下）")

    routes = [
        *auth_routes.routes,
        *auth_admin.routes,
        Route("/api/ping", ping),
        Route("/api/boom", boom),
    ]
    return TestClient(AuthMiddleware(Starlette(routes=routes)))


def login(client, username, password, **extra):
    return client.post("/api/auth/login", json={"username": username, "password": password, **extra})


def fresh_users():
    """清空用户文件与缓存 → 下次 load_users 重建默认管理员。"""
    from agent.auth import users

    path = users._users_path()
    if path.exists():
        path.unlink()
    users._users_cache = None
    return users.load_users()


# ── ① 密码哈希 ──────────────────────────────────────────────

def t1_hashing() -> None:
    section("① 密码哈希：加盐 + 可升级 + 旧格式仍可登录")
    from agent.auth import users

    h1 = users.hash_password("same-password")
    h2 = users.hash_password("same-password")
    check(h1 != h2, "同一密码两次哈希**不同**（= 有随机盐，彩虹表失效）", f"{h1[:24]}… vs {h2[:24]}…")
    check(h1.startswith("pbkdf2_sha256$") and h1.count("$") == 3,
          "格式自描述 `pbkdf2_sha256$iter$salt$hash`（换算法可就地升级，不需全量迁移）", h1[:24])
    iters = int(h1.split("$")[1])
    check(iters >= 200_000, "迭代次数 ≥ 200k（旧方案是**单轮**）", str(iters))
    check(users.verify_password_hash(h1, "same-password")
          and not users.verify_password_hash(h1, "same-passwor"),
          "新格式校验：对密码通过、对错一位不通过")
    check(not users.verify_password_hash("garbage-not-a-hash", "anything")
          and not users.verify_password_hash("pbkdf2_sha256$notanint$aa$bb", "anything"),
          "哈希串损坏/被手工改坏 → **判失败**（不是解析不了就放行）")
    check(users.needs_rehash(h1) is False, "新写的哈希不需要再升级")

    # 旧格式（存量）必须仍能登录，且登录时就地升级
    fn = fresh_users()
    legacy = users._legacy_sha256(ADMIN_PW)
    fn[0]["password_hash"] = legacy  # 伪造一条"09-23 之前"的记录
    fn[0].pop("token_version", None)
    fn[0].pop("must_change_password", None)
    users._save_users(fn)
    check(users.verify_password_hash(legacy, ADMIN_PW), "旧的无盐 SHA-256 哈希仍能校验通过")
    check(users.needs_rehash(legacy) is True, "旧格式被判定为「需升级」")

    u = users.verify_password("admin", ADMIN_PW)
    check(u is not None, "旧格式账号仍能登录（不强制全员重置密码）")
    rec = users.find_user("admin")
    check(rec["password_hash"].startswith("pbkdf2_sha256$"),
          "登录成功时**就地升级**为 PBKDF2（无人工迁移）", rec["password_hash"][:24])
    check(users.token_version_of(rec) == 0,
          "升级**不动** token_version（升级不是改密，不该踢掉其他设备）",
          f"token_version={users.token_version_of(rec)}")
    check(users.verify_password("admin", ADMIN_PW) is not None
          and users.verify_password("admin", "wrong") is None,
          "升级后新旧密码判定正确（升级没把密码改坏）")


# ── ② token 吊销 ────────────────────────────────────────────

def t2_revocation() -> None:
    section("② token 吊销：改密/吊销/删号立即失效，登录不作废，发版不掉线")
    from agent.auth import token, users

    fresh_users()
    users.add_user("alice", "alice-pass-123", "爱丽丝", must_change_password=False)
    a1 = users.verify_password("alice", "alice-pass-123")
    t1 = token.sign_token("alice", a1["display_name"], a1["is_admin"], token_version=a1["token_version"])
    t2 = token.sign_token("alice", a1["display_name"], a1["is_admin"], token_version=a1["token_version"])
    check(token.verify_token(t1) is not None and token.verify_token(t2) is not None,
          "同一账号可同时持有多串 token（多设备/多标签并存）")

    # 老 token（无 pv）在 pv=0 的记录上仍然有效 —— 发版不踢人的关键
    legacy_tok = token.sign_token("alice", "爱丽丝", False)  # 不传 token_version → 默认 0
    check(token.verify_token(legacy_tok) is not None,
          "**发版兼容**：本次升级前签发的 token（无 pv）对未吊销账号仍然有效（不会全站重登）")

    users.change_password("alice", "alice-pass-123", NEW_PW)
    check(token.verify_token(t1) is None and token.verify_token(t2) is None,
          "**改密 → 该账号所有旧 token 立即失效**（不只是当前设备）")
    check(token.verify_token(legacy_tok) is None, "老格式 token 同样被吊销（版本号变了）")

    a2 = users.verify_password("alice", NEW_PW)
    tk = token.sign_token("alice", a2["display_name"], a2["is_admin"], token_version=a2["token_version"])
    check(token.verify_token(tk) is not None, "用新密码重新登录后新 token 有效")
    check(users.token_version_of(users.find_user("alice")) == 1, "改密把 token_version 从 0 → 1")

    # 登录不吊销（本项决策：多设备并存）
    again = users.verify_password("alice", NEW_PW)
    tk2 = token.sign_token("alice", again["display_name"], again["is_admin"], token_version=again["token_version"])
    check(token.verify_token(tk) is not None and token.verify_token(tk2) is not None,
          "**登录不作废**既有 token（清单原文的「登录即失效」未采纳，见决策记录）")

    # 显式吊销：不动密码
    ver = users.revoke_tokens("alice")
    check(ver == 2 and token.verify_token(tk) is None and token.verify_token(tk2) is None,
          "显式吊销（`token_version` +1）→ 全部失效，**密码不变**", f"ver={ver}")
    check(users.verify_password("alice", NEW_PW) is not None, "吊销后密码仍可用（两者解耦）")

    # 删号
    a3 = users.verify_password("alice", NEW_PW)
    tk3 = token.sign_token("alice", a3["display_name"], a3["is_admin"], token_version=a3["token_version"])
    users.remove_user("alice")
    check(token.verify_token(tk3) is None, "**删号 → 立即失效**（记录消失，无需额外版本号）")

    # 身份字段以记录为准（降权立即生效）
    users.add_user("bob", "bob-pass-1234", "鲍勃", is_admin=True, must_change_password=False)
    b = users.verify_password("bob", "bob-pass-1234")
    tok_admin = token.sign_token("bob", b["display_name"], b["is_admin"], token_version=b["token_version"])
    check(token.verify_token(tok_admin)["is_admin"] is True, "管理员 token 的 is_admin 为真")
    users.update_user("bob", is_admin=False)
    after = token.verify_token(tok_admin)
    check(after is not None and after["is_admin"] is False,
          "**降权立即生效**：token 里的 is_admin 已不再被采信（身份以记录为准）", str(after))


# ── ③ 首登强制改密（标记）──────────────────────────────────

def t3_must_change() -> None:
    section("③ 首登强制改密：默认管理员带标记 + 自助改密清标记")
    from agent.auth import users

    fn = fresh_users()
    check(users.must_change_password_of(fn[0]) is True,
          "新建的默认管理员（密码是公开的 admin123）带 `must_change_password`")
    check(users.verify_password_hash(fn[0]["password_hash"], ADMIN_PW),
          "默认密码仍是 admin123（本次不擅自改掉，避免所有人都进不来）")

    users.add_user("carol", "carol-pass-123", "卡罗")
    check(users.must_change_password_of(users.find_user("carol")) is True,
          "管理员新建的用户默认也带标记（初始密码经手人知道）")
    users.add_user("svc", "svc-pass-12345", "服务账号", must_change_password=False)
    check(users.must_change_password_of(users.find_user("svc")) is False,
          "显式 `must_change_password=False` 可建服务账号（留出例外口）")

    client = build_client()
    r = login(client, "admin", ADMIN_PW)
    check(r.status_code == 200 and r.json().get("must_change_password") is True,
          "登录响应带 `must_change_password: true`（前端据此提示改密）", str(r.json()))
    r = client.get("/api/auth/me")
    check(r.status_code == 200 and r.json().get("must_change_password") is True,
          "`/api/auth/me` 也带该标记（中间件注入的字典必须带上它，否则这里恒为 false）",
          str(r.json()))

    r = client.post("/api/auth/change-password",
                    json={"old_password": ADMIN_PW, "new_password": NEW_PW})
    check(r.status_code == 200 and r.json().get("must_change_password") is False,
          "自助改密成功 → 响应里标记转 false", str(r.json()))
    check(users.must_change_password_of(users.find_user("admin")) is False,
          "记录里的标记已清除（下次登录不再提示）")
    check(login(client, "admin", ADMIN_PW).status_code == 401,
          "旧密码已失效")
    check(login(client, "admin", NEW_PW).status_code == 200, "新密码可登录")

    # 改密的输入校验（都走真端点）
    c = build_client()
    login(c, "admin", NEW_PW)
    check(c.post("/api/auth/change-password",
                 json={"old_password": "wrong", "new_password": "another-pass-1"}).status_code == 400,
          "旧密码不对 → 400")
    check(c.post("/api/auth/change-password",
                 json={"old_password": NEW_PW, "new_password": "short"}).status_code == 400,
          "新密码过短 → 400（下限由 4 位提到 8 位）")
    check(c.post("/api/auth/change-password",
                 json={"old_password": NEW_PW, "new_password": NEW_PW}).status_code == 400,
          "新旧密码相同 → 400")
    check(c.post("/api/auth/change-password",
                 json={"old_password": NEW_PW, "new_password": "x" * 8}).status_code == 200,
          "合规的新密码 → 200")


# ── ④ Cookie / 端点行为 ─────────────────────────────────────

def t4_cookie_and_endpoints() -> None:
    section("④ Cookie `Secure` 按请求判定 + 改密换发 Cookie + 管理员吊销端点")
    from api.auth_routes import cookie_secure

    class _Req:
        def __init__(self, headers, scheme):
            self.headers = headers
            self.url = type("U", (), {"scheme": scheme})()

    os.environ.pop("NL2SQL_COOKIE_SECURE", None)
    check(cookie_secure(_Req({}, "http")) is False,
          "明文 HTTP → **不加** Secure（加了浏览器会丢 Cookie：登录成功但全 401）")
    check(cookie_secure(_Req({}, "https")) is True, "真 HTTPS → 加 Secure")
    check(cookie_secure(_Req({"x-forwarded-proto": "https"}, "http")) is True,
          "经 nginx 终结 TLS（X-Forwarded-Proto: https）→ 加 Secure")
    check(cookie_secure(_Req({"x-forwarded-proto": "https, http"}, "http")) is True,
          "多段 XFF proto 取第一段（nginx 追加的语义）")
    os.environ["NL2SQL_COOKIE_SECURE"] = "1"
    check(cookie_secure(_Req({}, "http")) is True, "env 强制开启（压过请求判定）")
    os.environ["NL2SQL_COOKIE_SECURE"] = "0"
    check(cookie_secure(_Req({}, "https")) is False, "env 强制关闭（压过请求判定）")
    os.environ.pop("NL2SQL_COOKIE_SECURE", None)

    from agent.auth import users

    fn = fresh_users()
    fn[0]["must_change_password"] = False
    users._save_users(fn)

    client = build_client()
    r = login(client, "admin", ADMIN_PW)
    cookie_hdr = r.headers.get("set-cookie", "")
    check("HttpOnly" in cookie_hdr and "SameSite=lax" in cookie_hdr.replace("sameSite", "SameSite"),
          "Cookie 属性：HttpOnly + SameSite=Lax + Path=/", cookie_hdr)
    check("Secure" not in cookie_hdr, "明文 HTTP 下响应里没有 Secure（与上一条判定一致）")
    old_cookie = client.cookies.get("nl2sql_token")
    check(bool(old_cookie) and client.get("/api/auth/me").status_code == 200, "登录后 /me 可用")

    r = client.post("/api/auth/change-password",
                    json={"old_password": ADMIN_PW, "new_password": NEW_PW})
    new_cookie = client.cookies.get("nl2sql_token")
    check(r.status_code == 200 and new_cookie and new_cookie != old_cookie,
          "改密**换发**新 Cookie（否则改完密当场把自己踢下线）")
    check(client.get("/api/auth/me").status_code == 200, "换发后当前会话继续可用")

    # 旧 Cookie 必须失效（拿它单独打一次）
    from starlette.testclient import TestClient
    stale = build_client()
    stale.cookies.set("nl2sql_token", old_cookie)
    check(stale.get("/api/auth/me").status_code == 401,
          "**改密前的旧 Cookie 立即 401**（这才是吊销；换发只救当前这一个会话）")

    # 管理员吊销端点
    admin = build_client()
    login(admin, "admin", NEW_PW)
    users.add_user("dave", "dave-pass-1234", "戴夫", must_change_password=False)
    d = users.verify_password("dave", "dave-pass-1234")
    from agent.auth import token as _tk
    dave_tok = _tk.sign_token("dave", d["display_name"], False, token_version=d["token_version"])
    r = admin.post("/api/auth/users/dave/revoke")
    check(r.status_code == 200 and _tk.verify_token(dave_tok) is None,
          "管理员 `POST /api/auth/users/{uid}/revoke` → 目标账号 token 立即失效", str(r.json()))

    dave = build_client()
    login(dave, "dave", "dave-pass-1234")
    users.update_user("dave", is_admin=False)
    check(dave.post("/api/auth/users/admin/revoke").status_code == 403,
          "非管理员调吊销端点 → 403")
    r = dave.post("/api/auth/users/nobody-exists/revoke")
    check(r.status_code == 403, "非管理员连不存在的账号也拿 403（先判权限再判存在性）")
    r = admin.post("/api/auth/users/nobody-exists/revoke")
    check(r.status_code == 400, "管理员吊销不存在的账号 → 400（不是 500）", str(r.json()))


# ── ⑤ 拦截开关（默认关）────────────────────────────────────

def t5_enforcement_switch() -> None:
    section("⑤ 强制改密拦截：默认关；打开后必须留出自解套的路")
    from agent.auth import users

    fresh_users()  # 默认管理员带标记
    client = build_client()

    os.environ.pop("NL2SQL_FORCE_PASSWORD_CHANGE", None)
    login(client, "admin", ADMIN_PW)
    check(client.get("/api/ping").status_code == 200,
          "**默认只标记不拦截**：未改密账号照常访问业务端点（本批的发版形态）")
    check(client.get("/api/auth/me").status_code == 200, "默认关时 /me 正常")

    os.environ["NL2SQL_FORCE_PASSWORD_CHANGE"] = "1"
    try:
        c2 = build_client()
        login(c2, "admin", ADMIN_PW)
        r = c2.get("/api/ping")
        check(r.status_code == 403 and r.json().get("error") == "must_change_password",
              "开关打开后：未改密访问业务端点 → 403 `must_change_password`", str(r.json()))
        check("change-password" in str(r.json()), "403 文案里给出**自解套的办法**（不是干巴巴的 403）")
        check(c2.get("/api/auth/me").status_code == 200, "拦截期间 `/api/auth/me` 仍可用")
        # ⚠️ 顺序有讲究：logout 会**清掉 Cookie**，放在改密之前的话后面就变成"无凭证请求"
        # （首版就踩了这个：断言红了但生产代码没错，红的是测试自己的顺序）
        r = c2.post("/api/auth/change-password",
                    json={"old_password": ADMIN_PW, "new_password": NEW_PW})
        check(r.status_code == 200, "拦截期间**改密可用**（这是唯一解套路径，少放它=锁死用户）", str(r.json()))
        check(c2.get("/api/ping").status_code == 200, "改完密立刻恢复访问（不需要重新登录）")

        c4 = build_client()
        login(c4, "admin", NEW_PW)
        c4.get("/api/auth/me")
        check(c4.post("/api/auth/logout").status_code == 204, "拦截期间可以登出（换账号/重登）")
        check(c4.get("/api/ping").status_code == 401, "登出清了 Cookie → 后续请求 401（登出确实生效）")

        # 服务账号（未标记）不受影响
        users.add_user("svc2", "svc2-pass-1234", "服务账号", must_change_password=False)
        c3 = build_client()
        login(c3, "svc2", "svc2-pass-1234")
        check(c3.get("/api/ping").status_code == 200, "未标记的账号不受拦截影响（开关只对带标记的生效）")
    finally:
        os.environ.pop("NL2SQL_FORCE_PASSWORD_CHANGE", None)


# ── ⑥ 破坏性负对照 ──────────────────────────────────────────

def t6_negative_controls() -> None:
    section("⑥ 负对照（破坏性）：拆掉防护后断言必须翻转")
    from agent.auth import token, users

    fresh_users()
    users.add_user("erin", "erin-pass-1234", "艾琳", must_change_password=False)
    e = users.verify_password("erin", "erin-pass-1234")
    tok = token.sign_token("erin", e["display_name"], False, token_version=e["token_version"])
    users.change_password("erin", "erin-pass-1234", "erin-new-pass-1")
    check(token.verify_token(tok) is None, "（前置）吊销后旧 token 已失效")

    # 负控 1：把版本号比较拆掉 → 已吊销的 token 复活
    real_version_of = users.token_version_of
    users.token_version_of = lambda record: 0  # type: ignore[assignment]
    try:
        revived = token.verify_token(tok)
        check(revived is not None,
              "**负对照①**：拆掉 `token_version` 比较 → 已吊销的 token **复活**（说明防线就是它）",
              str(revived))
    finally:
        users.token_version_of = real_version_of  # type: ignore[assignment]
    check(token.verify_token(tok) is None, "恢复后旧 token 重新失效（负对照没留下副作用）")

    # 负控 2：把哈希校验拆掉 → 错密码也能登录
    real_verify = users.verify_password_hash
    users.verify_password_hash = lambda stored, password: True  # type: ignore[assignment]
    try:
        bad = users.verify_password("erin", "完全不对的密码")
        check(bad is not None,
              "**负对照②**：拆掉哈希校验 → 任意密码都能登录（说明登录确实走这道判定）",
              str(bad))
    finally:
        users.verify_password_hash = real_verify  # type: ignore[assignment]
    check(users.verify_password("erin", "完全不对的密码") is None, "恢复后错密码重新被拒")

    # 负控 3：把哈希换回无盐单轮 → 有盐断言必须失败
    real_hash = users.hash_password
    users.hash_password = lambda password: users._legacy_sha256(password)  # type: ignore[assignment]
    try:
        check(users.hash_password("same") == users.hash_password("same"),
              "**负对照③**：换回无盐哈希 → 同一密码两次结果**相同**（① 的有盐断言确实在测盐）")
    finally:
        users.hash_password = real_hash  # type: ignore[assignment]
    check(users.hash_password("same") != users.hash_password("same"), "恢复后有盐行为回来")


# ── ⑦ 回归：没有绕过新契约的旧写法 ──────────────────────────

def t7_regression() -> None:
    section("⑦ 回归扫描：旧写法/旧调用点没有残留")
    import ast
    import re

    hits: list[str] = []
    for path in sorted((_ROOT / "src").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if "__pycache__" in rel or "workspace-temp" in rel:
            continue
        src = path.read_text(encoding="utf-8")
        if "_hash_password(" in src:
            hits.append(f"{rel}: _hash_password(（旧函数名）")
        if re.search(r'password_hash"\]\s*!=|password_hash"\]\s*==', src):
            hits.append(f"{rel}: 直接比较 password_hash（绕过 verify_password_hash）")
    check(not hits, "`src/` 下没有残留的旧哈希写法（`_hash_password` / 直接比对密码哈希）", str(hits))

    # sign_token 必须带上 token_version，否则签出的 token 永久不可吊销
    missing: list[str] = []
    for path in sorted((_ROOT / "src").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if "__pycache__" in rel or "workspace-temp" in rel:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines = path.read_text(encoding="utf-8").splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sign_token":
                if not any(k.arg == "token_version" for k in node.keywords):
                    # 取整条调用语句（可能跨行）
                    seg = "\n".join(lines[node.lineno - 1: node.end_lineno])
                    missing.append(f"{rel}:{node.lineno} {seg.strip()[:80]}")
    check(not missing,
          "所有 `sign_token(...)` 调用点都显式传了 `token_version`（漏传 = 该 token 永不可吊销）",
          str(missing))

    # 端点清单：改密与吊销都真的挂上了
    from api import auth_admin, auth_routes

    paths = {getattr(r, "path", "") for r in [*auth_routes.routes, *auth_admin.routes]}
    check("/api/auth/change-password" in paths, "自助改密端点已挂载")
    check("/api/auth/users/{uid}/revoke" in paths, "管理员吊销端点已挂载")
    check("/api/auth/logout" in paths and "/api/auth/me" in paths, "登录/登出/me 仍在（没被改动打掉）")


def main() -> int:
    print(f"临时 AGENT_DATA_ROOT = {os.environ['AGENT_DATA_ROOT']}")
    t1_hashing()
    t2_revocation()
    t3_must_change()
    t4_cookie_and_endpoints()
    t5_enforcement_switch()
    t6_negative_controls()
    t7_regression()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
