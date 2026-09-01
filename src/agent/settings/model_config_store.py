"""运行时模型（LLM provider）配置存储 — model_config.json + AES-GCM 密钥加密。

前端通过管理 API（`src/api/model_config.py`）CRUD 本存储；`agent/llms/model.py`
的 `create_model()` 读本 store（按 route / active provider），无配置时返回 None（不再回退 `.env`）。

设计（对齐 `db_config_store.py` 范式）：
- 单 JSON 文件（默认 `src/agent/shared/model_config.json`，已 gitignore，
  可被 .env 的 `MODEL_CONFIG_PATH` 覆盖）。
- api_key 用 AES-256-GCM 加密落盘（密钥取自 `.env` 的 `MODEL_CONFIG_SECRET`，
  缺失时依次回退 `DB_CONFIG_SECRET`、开发默认值并告警）。
  加密值形如 `enc:<base64(nonce+ct+tag)>`。
- 原子写：写临时文件后 `os.replace`；进程内 `threading.RLock`。
- `.env` 的 `LLM_*` 不再自动迁移进 store —— 模型配置唯一来源是前端 CRUD。
  （`migrate_from_env` 保留但不再被调用，见模块末尾注释。）
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
from typing import Optional

from dotenv import load_dotenv

# 必须先加载 .env 再读密钥/路径（同 db_config_store 的原因：密钥不能依赖导入顺序）
load_dotenv()

_logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# 默认存储文件位置：优先 .env 的 MODEL_CONFIG_PATH，否则由 WorkspaceManager 动态解析
_DEFAULT_PATH = os.getenv("MODEL_CONFIG_PATH", "") or None  # None 表示由 WorkspaceManager 推导
_DEV_FALLBACK_SECRET = "dev-only-model-config-secret-do-not-use-in-prod"

_MASK = "***"
# .env 迁移进来的 provider 固定名
ENV_PROVIDER_NAME = "default"


@dataclass
class ModelConfig:
    """一条 LLM provider 配置（OpenAI-compatible 接入点）。

    models 为逐模型对象列表 [{id, name?, context_window?, max_tokens?}]，
    对齐 deepseek-harness 的模型目录结构；向后兼容旧的扁平 string 列表
    （读入时归一化为 {"id": <string>}）。
    """

    name: str                       # provider 名 / route 引用键（唯一）
    base_url: str = ""              # OpenAI-compatible base_url
    api_key: str = ""               # 加密落盘，读时解密
    models: list = field(default_factory=list)   # 该接入点可用模型列表（对象列表）
    default_model: str = ""         # create_model 未指定模型时使用的模型
    display_name: str = ""          # 显示名称（可选，空则用 name）
    api_protocol: str = ""          # API 协议（openai/anthropic …，仅元数据，探测恒按 openai 兼容）

    @staticmethod
    def _normalize_models(models) -> list:
        """归一化 models 为逐模型对象列表；兼容旧 string 列表与 dict 列表。"""
        out: list = []
        for m in models or []:
            if isinstance(m, str):
                out.append({"id": m.strip()})
            elif isinstance(m, dict):
                mid = str(m.get("id") or m.get("model") or "").strip()
                entry: dict = {"id": mid}
                name = m.get("name")
                if name:
                    entry["name"] = str(name)
                cw = m.get("context_window")
                if cw is not None:
                    try:
                        entry["context_window"] = int(cw)
                    except (TypeError, ValueError):
                        pass
                mt = m.get("max_tokens")
                if mt is not None:
                    try:
                        entry["max_tokens"] = int(mt)
                    except (TypeError, ValueError):
                        pass
                temp = m.get("temperature")
                if temp is not None:
                    try:
                        entry["temperature"] = float(temp)
                    except (TypeError, ValueError):
                        pass
                out.append(entry)
            else:
                out.append({"id": str(m)})
        return out

    def __post_init__(self):
        self.models = self._normalize_models(self.models)

    @classmethod
    def from_mapping(cls, data: dict) -> "ModelConfig":
        return cls(
            name=str(data.get("name", "")),
            base_url=str(data.get("base_url", "")),
            api_key=str(data.get("api_key", "")),
            models=data.get("models", []) or [],
            default_model=str(data.get("default_model", "")),
            display_name=str(data.get("display_name", "") or ""),
            api_protocol=str(data.get("api_protocol", "") or ""),
        )

    def to_mapping(self, masked: bool = False) -> dict:
        d = asdict(self)
        if masked:
            d["api_key"] = _MASK if d.get("api_key") else ""
            d["api_key_configured"] = bool(self.api_key)
        else:
            d["api_key_configured"] = bool(self.api_key)
        return d


class _Cipher:
    """AES-256-GCM 加解密（与 db_config_store 同实现）。"""

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
            return blob  # 明文（历史值）
        raw = base64.b64decode(blob[4:])
        nonce, ct = raw[:12], raw[12:]
        return self._aesgcm.decrypt(nonce, ct, None).decode("utf-8")


class ModelConfigStore:
    """JSON 文件 + api_key 加密的模型配置存储。"""

    def __init__(self, path: Optional[str] = None, secret: Optional[str] = None) -> None:
        if path:
            self._path = Path(path)
        elif _DEFAULT_PATH:
            self._path = Path(_DEFAULT_PATH)
        else:
            # 由 WorkspaceManager 动态解析（优先工作区级，回退共享）
            from agent.workspace_manager import get_workspace_manager
            self._path = get_workspace_manager().model_config_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        secret = (
            secret
            or os.getenv("MODEL_CONFIG_SECRET", "")
            or os.getenv("DB_CONFIG_SECRET", "")
            or _DEV_FALLBACK_SECRET
        )
        if not os.getenv("MODEL_CONFIG_SECRET") and not os.getenv("DB_CONFIG_SECRET"):
            _logger.warning(
                "[model_config] 未配置 MODEL_CONFIG_SECRET/DB_CONFIG_SECRET，"
                "使用开发回退密钥（仅限本地开发）"
            )
        self._cipher = _Cipher(secret)

    # ── 读 ──────────────────────────────────────────────
    def _read_raw(self) -> dict:
        if not self._path.exists():
            return {"version": 1, "active": "", "providers": []}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            _logger.warning("[model_config] 读取 %s 失败: %s，按空配置处理", self._path, e)
            return {"version": 1, "active": "", "providers": []}

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
            return [
                ModelConfig.from_mapping(item).to_mapping(masked=masked)
                for item in raw.get("providers", [])
            ]

    def get(self, name: str) -> ModelConfig:
        """按 name 取配置（api_key 已解密）。未找到抛 KeyError。"""
        with _LOCK:
            raw = self._read_raw()
            for item in raw.get("providers", []):
                cfg = ModelConfig.from_mapping(item)
                if cfg.name == name:
                    cfg.api_key = self._cipher.decrypt(cfg.api_key)
                    return cfg
            raise KeyError(f"模型配置 '{name}' 未在 model_config.json 中配置")

    def get_all_decrypted(self) -> list[ModelConfig]:
        """返回全部配置（api_key 解密）。供 create_model 用。"""
        with _LOCK:
            raw = self._read_raw()
            out = []
            for item in raw.get("providers", []):
                cfg = ModelConfig.from_mapping(item)
                cfg.api_key = self._cipher.decrypt(cfg.api_key)
                out.append(cfg)
            return out

    def get_active(self) -> str:
        """当前激活 provider 名（空串 = 未设置，调用方回退第一个）。"""
        with _LOCK:
            return str(self._read_raw().get("active", "") or "")

    def set_active(self, name: str) -> None:
        with _LOCK:
            raw = self._read_raw()
            names = {p.get("name") for p in raw.get("providers", [])}
            if name not in names:
                raise KeyError(f"模型配置 '{name}' 不存在")
            raw["active"] = name
            self._write_raw(raw)
            _logger.info("[model_config] 激活 provider '%s'", name)

    def upsert(self, cfg: ModelConfig) -> None:
        """新增或更新。api_key 为明文时自动加密落盘；为空表示保留原值。"""
        if not cfg.name:
            raise ValueError("模型配置 name 不能为空")
        if not cfg.base_url:
            raise ValueError("base_url 不能为空")
        model_ids = {m.get("id") for m in cfg.models if isinstance(m, dict) and m.get("id")}
        if cfg.default_model and model_ids and cfg.default_model not in model_ids:
            # 允许 default_model 不在 models 里（手动填的模型名），仅对齐时校验宽松
            pass

        with _LOCK:
            raw = self._read_raw()
            providers = raw.get("providers", [])
            existing = next((p for p in providers if p.get("name") == cfg.name), None)
            data = cfg.to_mapping(masked=False)
            if cfg.api_key:
                data["api_key"] = self._cipher.encrypt(cfg.api_key)
            if existing:
                if not cfg.api_key:
                    data["api_key"] = existing.get("api_key", "")
                existing.update(data)
            else:
                providers.append(data)
            raw["providers"] = providers
            # 首个 provider 自动设为激活（后续由前端显式切换）
            if not raw.get("active"):
                raw["active"] = cfg.name
            self._write_raw(raw)
            _logger.info("[model_config] upsert provider '%s'", cfg.name)

    def delete(self, name: str) -> bool:
        with _LOCK:
            raw = self._read_raw()
            providers = raw.get("providers", [])
            before = len(providers)
            raw["providers"] = [p for p in providers if p.get("name") != name]
            if len(raw["providers"]) == before:
                return False
            if raw.get("active") == name:
                raw["active"] = raw["providers"][0]["name"] if raw["providers"] else ""
            self._write_raw(raw)
            _logger.info("[model_config] 删除 provider '%s'", name)
            return True

    # ── 密钥一致性 ─────────────────────────────────────
    def _ensure_consistent(self) -> None:
        """加密值在当前密钥下无法解密（InvalidTag）→ 从 .env 重建。"""
        with _LOCK:
            raw = self._read_raw()
            for item in raw.get("providers", []):
                key = item.get("api_key", "")
                if not key or not key.startswith("enc:"):
                    continue
                try:
                    self._cipher.decrypt(key)
                except Exception:  # noqa: BLE001
                    _logger.warning(
                        "[model_config] 检测到加密密钥变更，已加密 api_key 不可解密；"
                        "清空存储（前端需重新配置模型，不再从 .env 重建）"
                    )
                    self._write_raw({"version": 1, "active": "", "providers": []})
                    return

    # ── .env 迁移（已停用：不再自动调用，模型配置唯一来源是前端 CRUD）──
    def migrate_from_env(self) -> int:
        """把 .env 的 LLM_* 导入 store（幂等：已存在同名跳过）。

        已停用自动调用（见 get_store）；保留仅供显式手动迁移存量部署。
        """
        from agent.settings.setting import settings

        if not (settings.LLM_API_KEY and settings.LLM_BASE_URL and settings.LLM_MODEL):
            return 0
        with _LOCK:
            raw = self._read_raw()
            existing_names = {p.get("name") for p in raw.get("providers", [])}
            if ENV_PROVIDER_NAME in existing_names:
                return 0
            raw.setdefault("providers", []).append(
                ModelConfig(
                    name=ENV_PROVIDER_NAME,
                    base_url=settings.LLM_BASE_URL,
                    api_key=self._cipher.encrypt(settings.LLM_API_KEY),
                    models=[{"id": settings.LLM_MODEL}],
                    default_model=settings.LLM_MODEL,
                ).to_mapping(masked=False)
            )
            if not raw.get("active"):
                raw["active"] = ENV_PROVIDER_NAME
            self._write_raw(raw)
            _logger.info("[model_config] 从 .env 迁移 provider '%s'", ENV_PROVIDER_NAME)
            return 1


# 单例（agent 进程与 API 进程共用同一文件，支持工作区切换）
_default_store: Optional[ModelConfigStore] = None
_default_store_path: Optional[str] = None


def get_store() -> ModelConfigStore:
    global _default_store, _default_store_path
    try:
        from agent.workspace_manager import get_workspace_manager
        current_path = str(get_workspace_manager().model_config_path)
    except Exception:
        current_path = str(_DEFAULT_PATH or "")
    if _default_store is None or _default_store_path != current_path:
        _default_store = ModelConfigStore(path=current_path if current_path else None)
        _default_store_path = current_path
        try:
            _default_store._ensure_consistent()
        except Exception as e:  # noqa: BLE001
            _logger.warning("[model_config] 密钥一致性检查跳过: %s", e)
        # 不再自动从 .env 迁移 LLM_*：模型配置唯一来源是前端 CRUD。
        # 历史版本已迁移进 store 的 "default" provider 不受影响。
    return _default_store


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = get_store()
    print(json.dumps(s.list_configs(masked=True), ensure_ascii=False, indent=2))
    print("active:", s.get_active())
