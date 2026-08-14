"""SemanticDbDetector — 解析「数据库 → Wren 项目」映射，判断某库是否已建模（语义层）。

背景：WrenAI 语义层按 Wren 项目绑定数据源。每个库可在 db_config.json 里配置
`wren_project`（前端「数据库配置管理」下拉选择），配了就启动该库专属的 wrenai
MCP server（工具名带库名前缀，如 `wrenai_imdb_run_sql`）；未配置的库走
db_mcp_server 直连。

已建模库的来源（两层，兜底兼容存量）：
1. **显式配置**：db_config.json 中 `wren_project` 非空的库（key = DBConfig.name，
   与前端 configurable.db_name 一致）。
2. **兜底（存量迁移零成本）**：默认 `WREN_PROJECT_PATH` 项目里
   `config/connection_*.json` 建模过的物理库名（imdb 无 wren_project 字段仍识别为
   已建模）。

用法：
    detector = SemanticDbDetector()
    detector.discover()                    # → {"imdb", "aix_report"}（已建模库名集合）
    detector.is_modeled("imdb")            # → True
    detector.project_path_for("imdb")      # → "…/imdb_project"
    wrenai_server_name("imdb")             # → "wrenai_imdb"（server / 工具前缀）
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)


def wrenai_server_name(db_name: str) -> str:
    """由 db_name 推导 wrenai MCP server 名 / 工具前缀（纯函数，调用方统一用）。

    `tool_name_prefix`（langchain_mcp_adapters）把 server 名与工具名纯拼接
    （`f"{server_name}_{tool.name}"`，无 sanitize），故 server 名必须合法标识符：
    保留 unicode 字母数字与下划线，其余字符（空格/斜杠/点等）替换为下划线。
    """
    return "wrenai_" + re.sub(r"\W+", "_", db_name or "").strip("_")


class SemanticDbDetector:
    """解析 db_config 中每库的 wren_project 配置 + 默认项目兜底扫描。"""

    def __init__(self, project_path: Optional[str] = None) -> None:
        from agent.settings.setting import settings

        # 默认项目可能未配置（.env 无 WREN_PROJECT_PATH）——为 None 时跳过 legacy
        # 兜底扫描，不能直接 Path(None)（会 TypeError）。显式 wren_project 不受影响。
        raw = project_path or settings.WREN_PROJECT_PATH
        self._project_path = Path(raw) if raw else None
        self._lock = threading.RLock()
        self._cache: dict[str, str] = {}  # db_name(name) → wren 项目绝对路径
        self._cache_valid = False

    # ── 数据源 ────────────────────────────────────────────

    def _load_mapping(self) -> dict[str, str]:
        """显式映射：db_config 中 wren_project 非空的库（key = DBConfig.name）。

        延迟 import db_config_store，避免 mcp_server↔agent 包循环依赖
        （db_config_store 本身不 import agent 包，无循环）。
        """
        mapping: dict[str, str] = {}
        try:
            from mcp_server.db_mcp_server.db.core.db_config_store import get_store

            for cfg in get_store().get_all_decrypted():
                if getattr(cfg, "wren_project", ""):
                    mapping[cfg.name] = cfg.wren_project
        except Exception as e:  # noqa: BLE001  配置读取失败不影响判断（兜底仍生效）
            _logger.warning("[semantic_db] 读取 db_config wren_project 失败: %s", e)
        return mapping

    def _scan_legacy(self) -> set[str]:
        """兜底：扫描默认 WREN_PROJECT_PATH 项目 config/connection_*.json。

        与旧版 `_scan()` 同逻辑——默认项目里建模过的物理库名集合，
        保证 imdb（无 wren_project 字段的存量配置）仍被识别为已建模。
        """
        names: set[str] = set()
        if not self._project_path:  # 默认项目未配置 → 无 legacy 兜底
            return names
        config_dir = self._project_path / "config"
        if not config_dir.is_dir():
            return names
        for f in sorted(config_dir.glob("connection_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                props = data.get("properties", {}) or {}
                db = props.get("database") or props.get("database_name") or ""
                if db:
                    names.add(str(db))
            except (json.JSONDecodeError, OSError) as e:
                _logger.warning("[semantic_db] 读取 %s 失败: %s", f, e)
        if names:
            _logger.info("[semantic_db] 默认项目已建模数据源: %s", sorted(names))
        return names

    # ── 解析 ──────────────────────────────────────────────

    def _resolve(self) -> dict[str, str]:
        """合并显式映射 + 默认项目兜底，得 db_name(name) → 项目路径。"""
        mapping = self._load_mapping()
        legacy = self._scan_legacy()
        for db in legacy:
            mapping.setdefault(db, str(self._project_path))
        return mapping

    def _refresh(self) -> None:
        with self._lock:
            if not self._cache_valid:
                self._cache = self._resolve()
                self._cache_valid = True
                if self._cache:
                    _logger.info(
                        "[semantic_db] 已建模库→项目映射: %s",
                        {k: v for k, v in sorted(self._cache.items())},
                    )

    # ── 对外接口 ──────────────────────────────────────────

    def discover(self, refresh: bool = False) -> set[str]:
        """返回已建模库名（name）集合（带缓存）。"""
        with self._lock:
            if refresh:
                self._cache_valid = False
            self._refresh()
            return set(self._cache)

    def invalidate(self) -> None:
        """使缓存失效，下次读取时从 db_config 重建。

        管理 API 在 upsert/delete 后调用，让 semantic 标记即时反映 wren_project
        变更，无需重启进程（detector 是进程级单例，_cache_valid 置 False 即重建）。
        """
        with self._lock:
            self._cache_valid = False
            self._cache = {}

    def is_modeled(self, db_name: str) -> bool:
        """判断某库是否已建模（语义层）——严格按 name 判定。

        路由场景（dynamic_prompt / _inject_db_name）必须精确匹配 name：
        若放宽到 physical database 兜底，name 与 database 不同名时（如
        name=clickhouse, database=aix_jinkoxn）会误判 modeled，而实际启动的是
        wrenai_<database> server，导致 LLM 按 wrenai_<name>_ 找不到工具。
        前端 semantic 标记如需 database 兜底，由调用方 `is_modeled(name) or
        is_modeled(database)` 显式做。
        """
        return db_name in self.discover()

    def project_path_for(self, db_name: str) -> Optional[str]:
        """返回 db_name 对应的 Wren 项目绝对路径；未建模返回 None。

        解析顺序：
        1. `_cache` 按 name（或默认项目 connection 的物理库名）命中；
        2. 该库的 physical database 字段在 `_cache` 中命中（name 与 database
           不同名时，用于前端 semantic 标记等宽口径场景）。
        """
        if not db_name:
            return None
        with self._lock:
            self._refresh()
            if db_name in self._cache:
                return self._cache[db_name]
        # database 字段兜底（宽口径）：connection 里可能用 physical database 建模
        try:
            from mcp_server.db_mcp_server.db.core.db_config_store import get_store

            cfg = get_store().get(db_name)
            if getattr(cfg, "wren_project", ""):
                return cfg.wren_project
            if cfg.database and cfg.database in self._cache:
                return self._cache[cfg.database]
        except Exception:  # noqa: BLE001  找不到配置记录则按未建模处理
            pass
        return None


# 单例
_detector: Optional[SemanticDbDetector] = None


def get_detector() -> SemanticDbDetector:
    global _detector
    if _detector is None:
        _detector = SemanticDbDetector()
    return _detector
