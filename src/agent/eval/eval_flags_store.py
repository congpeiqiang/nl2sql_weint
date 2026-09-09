# -*- coding: utf-8 -*-
"""评估开关的运行时覆盖层（前端「设置 → 评估」写盘，2026-09-09）。

落盘位置：``{AGENT_DATA_ROOT}/shared/eval_flags.json``
（``get_workspace_manager().shared_data_root``——全局共享，不随工作区切换）::

    {"NL2SQL_EVAL_JUDGE_ENABLED": "0", "NL2SQL_EVAL_JUDGE_SAMPLE": "0.5"}

语义：
- **只存被显式覆盖的键**。未出现在文件里的键继续走 ``os.environ`` → 代码默认
  （见 ``eval_flags``），所以「清空文件 = 回到 .env 行为」，.env/.env.prod 的改动
  仍然生效，不会因为前端点过一次保存就被永久钉死。
- 读取带 **失效缓存**（mtime + size）：多进程 / 多 worker 一致，写盘后下一次读取
  即生效（所有评估器都是读时求值 → 下一次查询、下一轮 drain 轮询即生效，无需重启）。
- IO 全部兜异常：**读失败按「无覆盖」处理**并 WARNING，绝不因运维文件损坏影响主链路；
  **写失败抛给调用方**（API 返回 500，前端可见）。

依赖方向：本模块**不 import** ``eval_flags``（避免 import 环）；键白名单与归一化
在此自持，``eval_flags`` 与 ``api.eval_flags`` 复用本模块的 ``KEYS`` / ``normalize``。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

_logger = logging.getLogger(__name__)

# 允许被前端覆盖的键（与 eval_flags 的开关一一对应）
KEYS: tuple[str, ...] = (
    "NL2SQL_EVAL_ENABLED",
    "NL2SQL_EVAL_JUDGE_ENABLED",
    "NL2SQL_EVAL_SUBJECT",
    "NL2SQL_EVAL_JUDGE_SAMPLE",
)

_BOOL_KEYS = frozenset(KEYS[:3])
_FALSY = frozenset({"0", "false", "no", "off"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_FILE_NAME = "eval_flags.json"

_lock = threading.RLock()
_cache: dict[str, str] = {}
_cache_sig: tuple[float, int] = (-1.0, 0)  # 文件不存在时的稳态签名
_path_cache: Path | None = None


def path() -> Path:
    """覆盖层文件路径（首次调用解析并缓存；运行期不变）。"""
    global _path_cache
    with _lock:
        if _path_cache is None:
            from agent.workspace_manager.manager import get_workspace_manager

            _path_cache = get_workspace_manager().shared_data_root / _FILE_NAME
        return _path_cache


def _signature(p: Path) -> tuple[float, int]:
    """文件失效签名 ``(mtime, size)``；不存在 → ``(-1.0, 0)``。

    带上 size 是为了兜住「同一时钟刻度内被改写」的极端情况。
    """
    try:
        st = p.stat()
    except OSError:
        return (-1.0, 0)
    return (st.st_mtime, st.st_size)


def normalize(key: str, value) -> str:
    """校验并归一化单个键值；非法抛 ``ValueError``（API 据此回 400）。

    - 布尔键：``0/1/true/false/yes/no/on/off``（大小写不敏感）→ 统一存 ``"0"``/``"1"``；
    - 采样率：``float`` 且落在 ``[0, 1]`` → 存规范化后的 ``str(float)``。
    """
    if key not in KEYS:
        raise ValueError(f"未知开关: {key}")
    raw = str(value).strip()
    if not raw:
        raise ValueError(f"{key} 取值不能为空")
    if key in _BOOL_KEYS:
        low = raw.lower()
        if low in _FALSY:
            return "0"
        if low in _TRUTHY:
            return "1"
        raise ValueError(f"{key} 只接受 0/1/true/false/yes/no/on/off，收到 {raw!r}")
    try:
        rate = float(raw)
    except ValueError:
        raise ValueError(f"{key} 需为 0~1 的小数，收到 {raw!r}") from None
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"{key} 需落在 0~1，收到 {raw!r}")
    return repr(rate)


def overrides() -> dict[str, str]:
    """当前覆盖项（只含被显式覆盖的键）。读失败 / 文件不存在 → ``{}``。"""
    global _cache, _cache_sig
    p = path()
    sig = _signature(p)
    with _lock:
        if sig == _cache_sig:
            return dict(_cache)
        data: dict[str, str] = {}
        if sig[0] >= 0:
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if k in KEYS and str(v).strip():
                            data[k] = str(v).strip()
                else:
                    _logger.warning("[eval-flags] %s 顶层不是对象，按无覆盖处理", p)
            except Exception as e:  # noqa: BLE001
                _logger.warning("[eval-flags] 读取 %s 失败，按无覆盖处理: %s", p, e)
                data = {}
        _cache, _cache_sig = data, sig
        return dict(data)


def write_overrides(mapping: dict) -> dict[str, str]:
    """整表替换覆盖项（校验 + 归一化 + 落盘 + 刷新缓存），返回落盘后的覆盖项。

    空 ``mapping`` 等价于清空（回到 .env 行为）。写盘走临时文件 + 原子替换，
    避免并发读看到半截 JSON。
    """
    normalized: dict[str, str] = {}
    for k, v in (mapping or {}).items():
        normalized[k] = normalize(k, v)  # 未知键 / 非法值直接抛 ValueError

    global _cache, _cache_sig
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(p)
    with _lock:
        _cache, _cache_sig = dict(normalized), _signature(p)
    return dict(normalized)


def patch_overrides(patch: dict) -> dict[str, str]:
    """增量修改覆盖项（API PUT 用）：只动 ``patch`` 里出现的键。

    值为空串 → **删除该键的覆盖**（回到 .env / 默认），因此前端每个开关都能
    「单独恢复默认」，不必整表清空。返回落盘后的完整覆盖项。
    """
    current = overrides()
    for k, v in (patch or {}).items():
        if k not in KEYS:
            raise ValueError(f"未知开关: {k}")
        if str(v).strip() == "":
            current.pop(k, None)
        else:
            current[k] = normalize(k, v)
    return write_overrides(current)


def clear_overrides() -> None:
    """清空覆盖项（等价于写空表：所有开关回到 .env / 默认）。"""
    write_overrides({})
