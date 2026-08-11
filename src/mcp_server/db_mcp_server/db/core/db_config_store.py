"""运行时数据库配置存储 — db_config.json + AES-GCM 密码加密。

前端通过管理 API（`db_config_api.py`）CRUD 本存储；子 agent 的 runner
从 store 读取连接配置（`McpSqlConfig.from_store`）。

设计：
- 单 JSON 文件（默认 `src/agent/workspace/db_config.json`，已 gitignore）。
- 密码用 AES-256-GCM 加密落盘（密钥取自 `.env` 的 `DB_CONFIG_SECRET`，
  缺失时回退开发默认值并告警）。加密值形如 `enc:<base64(nonce+ct+tag)>`。
- 原子写：写临时文件后 `os.replace`。
- `.env` 的 `DB_N_*` 在首次加载时迁移进 store（`migrate_from_env`）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# 默认存储文件位置（相对仓库根，可被 .env 的 DB_CONFIG_PATH 覆盖）
# 文件位于 src/mcp_server/db_mcp_server/db/core/ → parents[5] 即仓库根
_DEFAULT_PATH = os.getenv(
    "DB_CONFIG_PATH",
    str(Path(__file__).resolve().parents[5] / "src" / "agent" / "workspace" / "db_config.json"),
)
# 开发回退密钥（.env 未配 DB_CONFIG_SECRET 时使用，仅限本地开发）
_DEV_FALLBACK_SECRET = "dev-only-db-config-secret-do-not-use-in-prod"

_SUPPORTED_DB_TYPES = ("mysql", "clickhouse", "postgres", "sqlite")
_MASK = "***"


@dataclass
class DBConfig:
    """一条数据库连接配置。"""

    name: str                # 展示名 / db_name 引用（如 "生产MySQL"）
    db_type: str = "mysql"   # mysql / clickhouse / postgres / sqlite …
    host: str = "localhost"
    port: int = 3306
    database: str = ""       # 物理库名（mysql 的 database）
    user: str = ""
    password: str = ""       # 加密落盘，读时解密
    extra_config: dict = field(default_factory=dict)  # 额外 KV（如 sslmode）

    @classmethod
    def from_mapping(cls, data: dict) -> "DBConfig":
        """从 JSON 映射构造（兼容缺省字段）。"""
        return cls(
            name=str(data.get("name", "")),
            db_type=str(data.get("db_type", "mysql")),
            host=str(data.get("host", "localhost")),
            port=int(data.get("port", 3306)),
            database=str(data.get("database", "")),
            user=str(data.get("user", "")),
            password=str(data.get("password", "")),
            extra_config=dict(data.get("extra_config", {}) or {}),
        )

    def to_mapping(self, masked: bool = False) -> dict:
        """转 JSON 映射。masked=True 时密码脱敏（管理 API 列表用）。"""
        d = asdict(self)
        if masked:
            d["password"] = _MASK if d.get("password") else ""
            d["password_configured"] = bool(self.password)
        else:
            d["password_configured"] = bool(self.password)
        return d


class _Cipher:
    """AES-256-GCM 加解密（cryptography 库）。"""

    def __init__(self, secret: str) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = __import__("hashlib").sha256(secret.encode("utf-8")).digest()
        self._aesgcm = AESGCM(key)

    def encrypt(self, plain: str) -> str:
        if not plain:
            return ""
        nonce = secrets.token_bytes(12)
        ct = self._aesgcm.encrypt(nonce, plain.encode("utf-8"), None)
        return "enc:" + base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, blob: str) -> str:
        if not blob:
            return ""
        if not blob.startswith("enc:"):
            return blob  # 明文（旧 .env 迁移前的历史值）
        raw = base64.b64decode(blob[4:])
        nonce, ct = raw[:12], raw[12:]
        return self._aesgcm.decrypt(nonce, ct, None).decode("utf-8")


class DbConfigStore:
    """JSON 文件 + 密码加密的数据库配置存储。"""

    def __init__(self, path: Optional[str] = None, secret: Optional[str] = None) -> None:
        self._path = Path(path or _DEFAULT_PATH)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        secret = secret or os.getenv("DB_CONFIG_SECRET", "") or _DEV_FALLBACK_SECRET
        if not os.getenv("DB_CONFIG_SECRET"):
            _logger.warning(
                "[db_config] 未配置 DB_CONFIG_SECRET，使用开发回退密钥（仅限本地开发）"
            )
        self._cipher = _Cipher(secret)

    # ── 读 ──────────────────────────────────────────────
    def _read_raw(self) -> dict:
        if not self._path.exists():
            return {"version": 1, "databases": []}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            _logger.warning("[db_config] 读取 %s 失败: %s，按空配置处理", self._path, e)
            return {"version": 1, "databases": []}

    def _write_raw(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ── 对外接口 ────────────────────────────────────────
    def list_configs(self, masked: bool = True) -> list[dict]:
        with _LOCK:
            raw = self._read_raw()
            out = []
            for item in raw.get("databases", []):
                cfg = DBConfig.from_mapping(item)
                out.append(cfg.to_mapping(masked=masked))
            return out

    def get(self, db_name: str) -> DBConfig:
        """按 name 取配置（密码已解密）。未找到抛 KeyError。"""
        with _LOCK:
            raw = self._read_raw()
            for item in raw.get("databases", []):
                cfg = DBConfig.from_mapping(item)
                if cfg.name == db_name:
                    cfg.password = self._cipher.decrypt(cfg.password)
                    return cfg
            raise KeyError(f"数据库 '{db_name}' 未在 db_config.json 中配置")

    def get_all_decrypted(self) -> list[DBConfig]:
        """返回全部配置（密码解密）。供 runner 查询用。"""
        with _LOCK:
            raw = self._read_raw()
            out = []
            for item in raw.get("databases", []):
                cfg = DBConfig.from_mapping(item)
                cfg.password = self._cipher.decrypt(cfg.password)
                out.append(cfg)
            return out

    def upsert(self, cfg: DBConfig) -> None:
        """新增或更新。password 为明文时自动加密落盘。"""
        if cfg.db_type not in _SUPPORTED_DB_TYPES:
            raise ValueError(
                f"不支持的 db_type: {cfg.db_type!r}。支持: {', '.join(_SUPPORTED_DB_TYPES)}"
            )
        if not cfg.name:
            raise ValueError("数据库 name 不能为空")
        if cfg.db_type != "sqlite" and not cfg.host:
            raise ValueError("host 不能为空")

        with _LOCK:
            raw = self._read_raw()
            dbs = raw.get("databases", [])
            existing = next((d for d in dbs if d.get("name") == cfg.name), None)
            data = cfg.to_mapping(masked=False)
            # 密码：为空表示保留原密码；否则视为明文需加密
            if cfg.password:
                data["password"] = self._cipher.encrypt(cfg.password)
            if existing:
                if not cfg.password:
                    data["password"] = existing.get("password", "")
                existing.update(data)
            else:
                dbs.append(data)
            raw["databases"] = dbs
            self._write_raw(raw)
            _logger.info("[db_config] upsert 数据库 '%s' (%s)", cfg.name, cfg.db_type)

    def delete(self, db_name: str) -> bool:
        with _LOCK:
            raw = self._read_raw()
            dbs = raw.get("databases", [])
            before = len(dbs)
            raw["databases"] = [d for d in dbs if d.get("name") != db_name]
            if len(raw["databases"]) == before:
                return False
            self._write_raw(raw)
            _logger.info("[db_config] 删除数据库 '%s'", db_name)
            return True

    # ── .env 迁移 ───────────────────────────────────────
    def migrate_from_env(self) -> int:
        """把 .env 的 DB_N_* 导入 store（幂等：已存在的 name 跳过）。"""
        from mcp_server.db_mcp_server.db.core.settings import settings

        added = 0
        with _LOCK:
            raw = self._read_raw()
            existing_names = {d.get("name") for d in raw.get("databases", [])}
            for env_cfg in settings.get_databases():
                name = env_cfg["name"]
                if name in existing_names:
                    continue
                raw.setdefault("databases", []).append(
                    DBConfig(
                        name=name,
                        db_type=settings.DB_TYPE,
                        host=env_cfg["host"],
                        port=env_cfg["port"],
                        database=name,
                        user=env_cfg["user"],
                        password=self._cipher.encrypt(env_cfg["password"]),
                    ).to_mapping(masked=False)
                )
                existing_names.add(name)
                added += 1
            if added:
                raw["databases"] = raw.get("databases", [])
                self._write_raw(raw)
                _logger.info("[db_config] 从 .env 迁移 %d 个数据库", added)
        return added


# 单例（供 agent 进程与 API 进程共用同一文件）
_default_store: Optional[DbConfigStore] = None


def get_store() -> DbConfigStore:
    global _default_store
    if _default_store is None:
        _default_store = DbConfigStore()
        try:
            _default_store.migrate_from_env()
        except Exception as e:  # noqa: BLE001
            _logger.warning("[db_config] .env 迁移跳过: %s", e)
    return _default_store


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = get_store()
    print(json.dumps(s.list_configs(masked=True), ensure_ascii=False, indent=2))
