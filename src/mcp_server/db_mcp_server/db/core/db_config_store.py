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

from dotenv import load_dotenv

# 必须先加载 .env 再读密钥/路径，否则密钥取决于导入顺序（settings 是否已 import），
# 导致不同进程用不同密钥加密同一文件。
load_dotenv()

_logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# 默认存储文件位置：优先 .env 的 DB_CONFIG_PATH，否则由 WorkspaceManager 动态解析
_DEFAULT_PATH = os.getenv("DB_CONFIG_PATH", "") or None  # None 表示由 WorkspaceManager 推导
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
    wren_project: str = ""   # 关联的 Wren 项目绝对路径；空=未配置（该库走 dbmcp 直连）

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
            wren_project=str(data.get("wren_project", "")),
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
        if path:
            self._path = Path(path)
        elif _DEFAULT_PATH:
            self._path = Path(_DEFAULT_PATH)
        else:
            # 由 WorkspaceManager 动态解析
            from agent.workspace_manager import get_workspace_manager
            self._path = get_workspace_manager().db_config_path
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
        """按 name 取配置（密码已解密）。未找到抛 KeyError。

        大小写容错（2026-08-23 串库路由修复）：精确匹配优先，未命中再按
        忽略大小写匹配。背景：前端下拉传 store 原样名（如 `Chinook_Aliyun`），
        但脚本/外部调用可能传大小写不同名（如 `chinook_aliyun`）——精确匹配
        失败会落 .env 兜底报「未配置」，LLM 随即自行探索其它库触发串库级联。
        统一返回 canonical 配置，下游路由（is_modeled / 工具 db_name）不受影响。
        """
        with _LOCK:
            raw = self._read_raw()
            items = [DBConfig.from_mapping(item) for item in raw.get("databases", [])]
            exact = next((c for c in items if c.name == db_name), None)
            if exact is not None:
                exact.password = self._cipher.decrypt(exact.password)
                return exact
            lower = db_name.lower()
            ci = next((c for c in items if c.name.lower() == lower), None)
            if ci is not None:
                ci.password = self._cipher.decrypt(ci.password)
                return ci
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
            # 大小写容错唯一性（与 get 同口径，2026-08-23）：禁止新增仅大小写
            # 不同的库名——get() 大小写容错下会两个库名解析歧义、前端下拉重复展示。
            # 编辑既有库（精确同名）仍走 existing 更新；case-duplicate 直接拒绝。
            if existing is None:
                case_dup = next(
                    (d for d in dbs if d.get("name", "").lower() == (cfg.name or "").lower()),
                    None,
                )
                if case_dup is not None:
                    raise ValueError(
                        f"库名 '{cfg.name}' 与已有库 '{case_dup.get('name')}' 仅大小写不同，"
                        "请使用已有名称或换名"
                    )
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
            # 与 get() 同口径（大小写容错）：upsert 已禁止 case-duplicate，故最多一条匹配
            lower = db_name.lower()
            raw["databases"] = [
                d for d in dbs
                if d.get("name") != db_name and d.get("name", "").lower() != lower
            ]
            if len(raw["databases"]) == before:
                return False
            self._write_raw(raw)
            _logger.info("[db_config] 删除数据库 '%s'", db_name)
            return True

    def set_wren_project(self, db_name: str, wren_project: str) -> bool:
        """仅更新某库的 wren_project 字段（不触碰连接信息/密码）。

        语义库管理（新增/删除语义库）用：把一个库关联到 Wren 项目目录，或置空
        解绑。相比读整条再 upsert，这里只 patch 单字段，避免误覆盖 host/port 等。
        未找到该库返回 False。
        """
        with _LOCK:
            raw = self._read_raw()
            lower = db_name.lower()
            for item in raw.get("databases", []):
                if item.get("name") == db_name or item.get("name", "").lower() == lower:
                    item["wren_project"] = wren_project
                    self._write_raw(raw)
                    _logger.info(
                        "[db_config] '%s' 的 wren_project → %r", db_name, wren_project
                    )
                    return True
            return False

    # ── 密钥一致性 ─────────────────────────────────────
    def _ensure_consistent(self) -> None:
        """检测存储是否用旧密钥加密（DB_CONFIG_SECRET 变更 / 历史 dev 回退密钥）。

        加密值在当前密钥下无法解密（InvalidTag）时，该文件的密码已不可恢复；
        从 .env 重建（.env 的 DB_N_* 是权威源，明文密码可重新加密写入）。
        """
        with _LOCK:
            raw = self._read_raw()
            for item in raw.get("databases", []):
                pwd = item.get("password", "")
                if not pwd or not pwd.startswith("enc:"):
                    continue
                try:
                    self._cipher.decrypt(pwd)
                except Exception:  # noqa: BLE001  InvalidTag / ValueError
                    _logger.warning(
                        "[db_config] 检测到加密密钥变更，已加密密码不可解密；"
                        "从 .env 重建存储（前端新录入的配置需重新配置）"
                    )
                    self._write_raw({"version": 1, "databases": []})
                    self.migrate_from_env()
                    return

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


# 单例（供 agent 进程与 API 进程共用同一文件，支持工作区切换）
_default_store: Optional[DbConfigStore] = None
_default_store_path: Optional[str] = None  # 记录上次的工作区路径，变更时重建


def get_store() -> DbConfigStore:
    global _default_store, _default_store_path
    try:
        from agent.workspace_manager import get_workspace_manager
        current_path = str(get_workspace_manager().db_config_path)
    except Exception:
        current_path = str(_DEFAULT_PATH or "")
    if _default_store is None or _default_store_path != current_path:
        _default_store = DbConfigStore(path=current_path if current_path else None)
        _default_store_path = current_path
        try:
            _default_store._ensure_consistent()
        except Exception as e:  # noqa: BLE001
            _logger.warning("[db_config] 密钥一致性检查跳过: %s", e)
        try:
            _default_store.migrate_from_env()
        except Exception as e:  # noqa: BLE001
            _logger.warning("[db_config] .env 迁移跳过: %s", e)
    return _default_store


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = get_store()
    print(json.dumps(s.list_configs(masked=True), ensure_ascii=False, indent=2))
