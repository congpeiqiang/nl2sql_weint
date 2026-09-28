"""临时用户管理（auth_users.json）。

P0 临时方案：从 AGENT_DATA_ROOT/auth_users.json 读取用户列表。
首次启动无文件 → 自动创建默认管理员 admin/admin123。
SSO 接入后此文件整体替换。

文件格式：
[
  {
    "user_id": "admin",
    "password_hash": "pbkdf2_sha256$260000$<salt>$<hash>",
    "display_name": "管理员",
    "is_admin": true,
    "token_version": 0,          # P1-12：吊销计数器（改密/吊销 +1 → 旧 token 立即失效）
    "must_change_password": true # P1-12：首登强制改密标记（默认只标记不拦截，见 auth_middleware）
  }
]

P1-12 凭据加固（2026-09-23）：
- **哈希**：单轮无盐 SHA-256 → **PBKDF2-HMAC-SHA256 + 每用户随机盐 + 迭代**。
  选标准库而不是 bcrypt/argon2 是因为发版形态：镜像在离线环境靠 `docker save/load` 搬运，
  加新依赖要连包源重建镜像（P1-12 决策记录）。旧哈希**不强制重置** —— 登录成功时顺手
  升级（`_verify_password_hash` + `needs_rehash`），升级过程**不动** `token_version`
  （升级不是改密，不该把别的设备踢下线）。
- **吊销**：`token_version` 写进 token 载荷（`pv`）。改密 / 显式吊销 +1，删号 = 记录消失。
  登录**不**改它（允许多设备并存，见清单决策）。缺字段一律按 0 → 老 token（无 `pv`）
  在本次升级后仍然有效，不会在发版瞬间把所有人踢出去。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any

from agent.auth.token import User

logger = logging.getLogger(__name__)

_users_cache: list[dict[str, Any]] | None = None

# ── 密码哈希（P1-12）────────────────────────────────────────
#
# 迭代次数：260k ≈ 登录一次几十毫秒（OpenSSL 的 pbkdf2_hmac），对"一天登一次"的
# 交互式登录完全无感；对离线爆破是 10^5 量级的成本抬升（旧方案单轮 SHA-256 在
# GPU 上是每秒数十亿次，且**无盐** → 同一密码全站哈希相同，彩虹表直接命中）。
PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 260_000
_SALT_BYTES = 16

# 密码长度下限（新设/重置/自助改密共用）。旧下限是 4 位 —— 对"能当管理员"的账号
# 太松，本次统一提到 8。管理员仍在 `add_user` 里能收到明确报错文案。
MIN_PASSWORD_LENGTH = 8

# ── 并发保护 ────────────────────────────────────────────────
#
# `_users_cache` 上的操作全是**读-改-写**（`add_user`：load → append → _save_users
# 里清空缓存）。没有锁的后果是两个并发 add 各拿到同一份旧列表，后写的那次覆盖前者
# → **丢用户**；`update_user`/`remove_user` 同理，甚至会把已删用户从旧快照复活。
# 所有「读列表 + 写列表」的入口一律持同一把可重入锁（load_users / find_user 也会
# 在锁内被调用，故必须是 RLock）。
_users_lock = threading.RLock()


def _users_path() -> Path:
    data_root = os.getenv("AGENT_DATA_ROOT", "")
    if data_root:
        return Path(data_root) / "auth_users.json"
    return Path(__file__).resolve().parents[3] / "auth_users.json"


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 + 随机盐 → `pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>`。

    格式自带参数（算法/迭代/盐），所以将来换算法或提迭代是**逐条就地升级**，
    不需要全量迁移：`needs_rehash()` 认旧格式，登录成功时重算即可。
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return (
        f"{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}"
        f"${salt.hex()}${digest.hex()}"
    )


def _legacy_sha256(password: str) -> str:
    """旧方案（单轮无盐 SHA-256）。**只保留用于校验存量哈希**，不再签发。"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password_hash(stored: str, password: str) -> bool:
    """校验密码（兼容两种格式）。比较一律走 `hmac.compare_digest`（定时安全）。"""
    stored = stored or ""
    if stored.startswith(PBKDF2_ALGORITHM + "$"):
        try:
            _, iter_s, salt_hex, want_hex = stored.split("$", 3)
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iter_s)
            )
        except Exception:  # noqa: BLE001
            # 记录被手工改坏：判失败（不能因为解析不了就放行），并留日志
            logger.error("[auth] 密码哈希格式损坏，无法校验（按失败处理）: %.24s…", stored)
            return False
        return hmac.compare_digest(digest.hex(), want_hex)
    # 存量：单轮无盐 SHA-256
    return hmac.compare_digest(stored, _legacy_sha256(password))


def needs_rehash(stored: str) -> bool:
    """是否该在下次登录成功时就地升级哈希（旧格式 / 迭代数落后）。"""
    stored = stored or ""
    if not stored.startswith(PBKDF2_ALGORITHM + "$"):
        return True
    try:
        return int(stored.split("$", 3)[1]) < PBKDF2_ITERATIONS
    except Exception:  # noqa: BLE001
        return True


def token_version_of(record: dict[str, Any] | None) -> int:
    """读吊销计数器（缺字段 = 0：本次升级前签发的用户记录与 token 都按 0 对齐）。"""
    if not record:
        return 0
    try:
        return int(record.get("token_version", 0) or 0)
    except (TypeError, ValueError):
        return 0


def must_change_password_of(record: dict[str, Any] | None) -> bool:
    """读首登改密标记（缺字段 = False：存量账号不被本次升级波及）。"""
    return bool(record.get("must_change_password", False)) if record else False


def _default_users() -> list[dict[str, Any]]:
    """默认用户列表（首次启动自动创建）。

    默认管理员的密码是**公开已知**的 `admin123`，所以给它打上
    `must_change_password`（登录响应与 `/api/auth/me` 会带出来，前端可据此提示；
    「拦截」由 `NL2SQL_FORCE_PASSWORD_CHANGE` 开关控制，默认只标记，见 auth_middleware）。
    """
    return [
        {
            "user_id": "admin",
            "password_hash": hash_password("admin123"),
            "display_name": "管理员",
            "is_admin": True,
            "token_version": 0,
            "must_change_password": True,
        }
    ]


def reload_users() -> list[dict[str, Any]]:
    """清除缓存并重新加载用户列表（管理员编辑 JSON 后调用，免重启生效）。"""
    global _users_cache
    _users_cache = None
    users = load_users()
    logger.info("[auth] 用户列表已重载，共 %d 个用户", len(users))
    return users


def load_users() -> list[dict[str, Any]]:
    """加载用户列表（惰性加载，进程内缓存）。"""
    global _users_cache
    if _users_cache is not None:
        return _users_cache

    with _users_lock:
        if _users_cache is not None:  # 双检：等锁期间别人已加载
            return _users_cache

        path = _users_path()
        if not path.exists():
            users = _default_users()
            path.parent.mkdir(parents=True, exist_ok=True)
            # 原子落地：直接 write_text 的话，另一个进程/线程读到的是**写了一半**的
            # JSON（解析失败 → 回落到 _default_users() = 静默把用户砍回只剩 admin）。
            _atomic_write(path, users)
            logger.info("[auth] 自动创建默认用户文件 %s（admin/admin123）", path)
        else:
            try:
                users = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error("[auth] 读取用户文件失败 %s: %s", path, e)
                users = _default_users()

        _users_cache = users
        return users


def find_user(username: str) -> dict[str, Any] | None:
    """按 user_id 查找用户。"""
    for u in load_users():
        if u.get("user_id") == username:
            return u
    return None


# ── CRUD（管理员 API，写文件 + 自动清缓存）──────────────

def _atomic_write(path: Path, users: list[dict[str, Any]]) -> None:
    """同目录临时文件 + replace 落盘（同一文件系统内 rename 是原子的）。

    ⚠️ 临时文件名必须**唯一**（mkstemp），不能用固定的 `path.with_suffix(".tmp")`：
    两个并发写会抢同一个 tmp 文件——A 写完还没 rename、B 覆盖同一 tmp，A rename
    走的其实是 B 的内容；更糟的是 B 的 rename 先成功、A 再 rename 就报
    FileNotFoundError（tmp 已被搬走）。固定 tmp 名只在单写者下成立。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(users, ensure_ascii=False, indent=2))
            fh.flush()
            os.fsync(fh.fileno())  # 保证内容先于 rename 落盘
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)  # 失败不留垃圾
        except OSError:
            pass
        raise


def _save_users(users: list[dict[str, Any]]) -> None:
    """写入用户列表并清除缓存（原子写：先 tmp 再 rename）。"""
    global _users_cache
    with _users_lock:
        _atomic_write(_users_path(), users)
        _users_cache = None
    logger.info("[auth] 用户文件已写入，共 %d 个用户", len(users))


def add_user(
    user_id: str,
    password: str,
    display_name: str = "",
    is_admin: bool = False,
    must_change_password: bool = True,
) -> dict[str, Any]:
    """新增用户。返回新用户 dict（不含 password_hash）。

    `must_change_password` 默认 True：管理员设的初始密码是**经手人知道的**，
    首登改掉它才是标准做法（只标记，是否拦截由开关决定）。要建服务账号可显式传 False。
    """
    # 整段读-改-写必须在锁内：否则两个并发 add 各拿同一份旧列表 → 后者覆盖前者
    with _users_lock:
        if not user_id or not user_id.strip():
            raise ValueError("user_id 不能为空")
        if not password or len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")
        users = load_users()
        if any(u.get("user_id") == user_id for u in users):
            raise ValueError(f"用户 '{user_id}' 已存在")
        new_user = {
            "user_id": user_id.strip(),
            "password_hash": hash_password(password),
            "display_name": display_name.strip() or user_id.strip(),
            "is_admin": bool(is_admin),
            "token_version": 0,
            "must_change_password": bool(must_change_password),
        }
        users.append(new_user)
        _save_users(users)
    return {k: v for k, v in new_user.items() if k != "password_hash"}


def update_user(
    user_id: str,
    password: str | None = None,
    display_name: str | None = None,
    is_admin: bool | None = None,
    must_change_password: bool | None = None,
) -> dict[str, Any]:
    """修改用户。只更新传入的字段。返回更新后的 dict（不含 password_hash）。

    **改密 = 吊销**：`password` 一变就把 `token_version` +1，该账号此前签发的所有
    token 立即失效（清单原文「改密…即失效」）。升管理员/改显示名**不**吊销。
    """
    with _users_lock:
        users = load_users()
        target = None
        for u in users:
            if u.get("user_id") == user_id:
                target = u
                break
        if target is None:
            raise ValueError(f"用户 '{user_id}' 不存在")
        if password is not None:
            if len(password) < MIN_PASSWORD_LENGTH:
                raise ValueError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")
            target["password_hash"] = hash_password(password)
            target["token_version"] = token_version_of(target) + 1
            # 管理员重置的密码同样只标记首登改密（显式传 False 可关，如服务账号）
            target["must_change_password"] = True
        if display_name is not None:
            target["display_name"] = display_name.strip()
        if is_admin is not None:
            target["is_admin"] = bool(is_admin)
        if must_change_password is not None:
            target["must_change_password"] = bool(must_change_password)
        _save_users(users)
    return {k: v for k, v in target.items() if k != "password_hash"}


def revoke_tokens(user_id: str) -> int:
    """吊销某账号当前所有 token（`token_version` +1）→ 返回新版本号。

    与改密的区别：**不动密码**。用在"密码可能泄露但不想改密码""踢下线"这类场景，
    以及管理员侧的一键吊销端点。删号则不需要它（记录消失 = `verify_token` 直接判无效）。
    """
    with _users_lock:
        users = load_users()
        target = next((u for u in users if u.get("user_id") == user_id), None)
        if target is None:
            raise ValueError(f"用户 '{user_id}' 不存在")
        target["token_version"] = token_version_of(target) + 1
        _save_users(users)
        return target["token_version"]


def change_password(user_id: str, old_password: str, new_password: str) -> None:
    """自助改密：先验旧密码，再写新密码（并吊销该账号所有旧 token）。

    为什么不让路由自己拼 `verify_password` + `update_user`：那样两处都得记得
    "改密必须吊销"，漏一处就是一个改完密旧 token 还能用 24h 的洞。这里一次做全。
    """
    if not new_password or len(new_password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"新密码至少 {MIN_PASSWORD_LENGTH} 位")
    if hmac.compare_digest(old_password or "", new_password or ""):
        raise ValueError("新密码不能与旧密码相同")
    record = find_user(user_id)
    if record is None:
        raise ValueError(f"用户 '{user_id}' 不存在")
    if not verify_password_hash(record.get("password_hash", ""), old_password):
        raise ValueError("旧密码不正确")
    update_user(user_id, password=new_password, must_change_password=False)


def remove_user(user_id: str) -> None:
    """删除用户。"""
    with _users_lock:
        users = load_users()
        new_users = [u for u in users if u.get("user_id") != user_id]
        if len(new_users) == len(users):
            raise ValueError(f"用户 '{user_id}' 不存在")
        if not new_users:
            raise ValueError("不能删除最后一个用户")
        _save_users(new_users)


def verify_password(username: str, password: str) -> User | None:
    """校验用户名密码 → User 或 None。

    登录成功时顺带做两件事：
    · **原地升级哈希**（旧的无盐 SHA-256 → PBKDF2）：失败不影响登录（只记日志），
      且**不动** `token_version` —— 升级不是改密，不该把用户其他设备踢下线。
    · 带上 `must_change_password` / `token_version`，供签发 token 与响应体使用。
    """
    user = find_user(username)
    if not user:
        return None
    stored = user.get("password_hash", "")
    if not verify_password_hash(stored, password):
        return None
    if needs_rehash(stored):
        try:
            _upgrade_hash(user["user_id"], password)
        except Exception:  # noqa: BLE001
            logger.warning("[auth] 密码哈希就地升级失败 user=%s", user["user_id"], exc_info=True)
    return User(
        user_id=user["user_id"],
        display_name=user.get("display_name", user["user_id"]),
        is_admin=user.get("is_admin", False),
        must_change_password=must_change_password_of(user),
        token_version=token_version_of(user),
    )


def _upgrade_hash(user_id: str, password: str) -> None:
    """把存量哈希换成新格式（持锁、只改 password_hash 一个字段）。"""
    with _users_lock:
        users = load_users()
        target = next((u for u in users if u.get("user_id") == user_id), None)
        if target is None:
            return
        target["password_hash"] = hash_password(password)
        _save_users(users)
        logger.info("[auth] 密码哈希已升级为 %s user=%s", PBKDF2_ALGORITHM, user_id)
