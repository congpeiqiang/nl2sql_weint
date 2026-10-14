"""功能开关集中管理——启动期校验，运行时只读。

**为什么需要**：env 开关散落在 100+ 处 `os.getenv(...)` 调用点，非法值全部**静默
回落默认值**——`NL2SQL_RETRIEVAL=hybird`（拼错）不会报错，只是查询悄悄降级纯 FTS；
`NL2SQL_DRAIN_SECS=abc` 悄悄按默认 180s 排空。生产上「以为开了、其实没开」的事故
几乎都是这么来的。

**做法**（本模块只管**校验**，不强制迁移所有读取点）：
  1. 启动期集中读一遍核心开关，非法值**直接抛错**（`start_server.py` 里 catch 后退出）。
  2. 各模块的读取点按需逐步迁移到 `get_flags()`（Task 2-5 Step 3，非强制、非一次到位）。

**不覆盖**：调参类开关（阈值/超时/采样率）留在各模块自读——它们非法时回落默认值的
危害小得多，全量收敛的收益不抵迁移成本与回归风险。
"""
import logging
import os

_logger = logging.getLogger(__name__)

# ── 合法值集合（与各模块的常量保持同源，改这边必须同步改那边）──────
#: 检索模式。真身见 `agent/retrieval/schema.py:_MODES`（MODE_OFF/MODE_FTS/MODE_HYBRID）。
_RETRIEVAL_MODES = {"off", "fts", "hybrid"}
#: 检索后端。真身见 `agent/retrieval/schema.py:_BACKENDS`。
_RETRIEVAL_BACKENDS = {"lance", "jsonl"}

#: 取真值的字面量（与 failure_circuit.py / retention.py 的口径一致）
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}


def _env_bool(name: str, default: bool) -> bool:
    """读布尔开关。未设 → default；设了但既非真值也非假值 → 抛错。

    **不静默回落**：`NL2SQL_RETENTION_ENABLED=ture` 拼错时要吵，不能悄悄按默认跑。
    """
    raw = (os.getenv(name) or "").strip().lower()
    if raw == "":
        return default
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    raise ValueError(
        f"{name}={raw!r} 不是合法布尔值；允许 {sorted(_TRUTHY)} / {sorted(_FALSY - {''})}"
    )


def _env_bool_one(name: str, default: bool) -> bool:
    """读**严格 `1`/`0`** 的布尔开关。

    专供 `NL2SQL_AUTH_DISABLED`：它的两个读取点（`auth/token.py:140`、
    `settings/setting.py:102`）都是 `== "1"` 的严格比较，所以 `=true` 在那边**不生效**。
    如果用宽松布尔校验，`NL2SQL_AUTH_DISABLED=true` 会被本模块判为「已关闭」而实际
    鉴权照常开启 —— 这正是本模块要消灭的那类**两层口径不一致**。这里不跟着宽松，
    也不跟着静默，而是**直接报错**逼操作者用两边都认的那一个拼写。
    """
    raw = (os.getenv(name) or "").strip()
    if raw == "":
        return default
    if raw == "1":
        return True
    if raw == "0":
        return False
    raise ValueError(f"{name}={raw!r} 不合法；该开关只认 '1'（开）/ '0'（关）")


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """读整数开关。未设 → default；非法或越界 → 抛错。"""
    raw = (os.getenv(name) or "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} 不是整数") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"{name}={value} 不能小于 {minimum}")
    return value


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    """读枚举开关。未设 → default；非法 → 抛错并列出允许值。"""
    raw = (os.getenv(name) or "").strip().lower()
    if raw == "":
        return default
    if raw not in allowed:
        raise ValueError(f"{name}={raw!r} 不合法；允许值: {sorted(allowed)}")
    return raw


class FeatureFlags:
    """功能开关快照——`get_flags()` 在首次调用时构建并校验。"""

    def __init__(self) -> None:
        # ── 检索类 ──
        self.retrieval = _env_choice("NL2SQL_RETRIEVAL", "off", _RETRIEVAL_MODES)
        self.retrieval_backend = _env_choice(
            "NL2SQL_RETRIEVAL_BACKEND", "lance", _RETRIEVAL_BACKENDS
        )
        # ── 门禁止（默认开，=off 一键回退）──
        self.fail_circuit = _env_bool("NL2SQL_FAIL_CIRCUIT", True)
        self.cube_route_gate = _env_bool("NL2SQL_CUBE_ROUTE_GATE", True)
        self.knowledge_trim = _env_bool("NL2SQL_KNOWLEDGE_TRIM", True)
        # ── 运维类 ──
        self.drain_secs = _env_int("NL2SQL_DRAIN_SECS", 180, minimum=0)
        self.retention_enabled = _env_bool("NL2SQL_RETENTION_ENABLED", True)
        self.auth_disabled = _env_bool_one("NL2SQL_AUTH_DISABLED", False)
        self.db_mcp_enabled = _env_bool("NL2SQL_DBMCP_ENABLED", True)
        # ── 并发类 ──
        # 默认 6 / 合法下界 0 —— 与 `utils/llm_gate.py:208,225` 同源（`0` = 关闸，不是非法值）
        self.llm_max_concurrency = _env_int("NL2SQL_LLM_MAX_CONCURRENCY", 6, minimum=0)

    def as_dict(self) -> dict:
        return dict(vars(self))


# 进程级单例
_flags: FeatureFlags | None = None


def get_flags() -> FeatureFlags:
    """取功能开关单例（首次调用时构建并校验，非法值抛 ValueError）。"""
    global _flags
    if _flags is None:
        _flags = FeatureFlags()
        _logger.info("[FeatureFlags] 已校验: %s", _flags.as_dict())
    return _flags


def reset_flags_for_tests() -> None:
    """仅供测试：清空单例（改 env 后重新读取）。"""
    global _flags
    _flags = None