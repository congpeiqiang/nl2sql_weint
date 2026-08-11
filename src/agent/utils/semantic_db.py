"""SemanticDbDetector — 动态发现 Wren 项目中已建模（语义层）的数据库集合。

背景：WrenAI 语义层绑定一个 Wren 项目；「单项目多数据源」下，
`config/connection_*.json` 每个文件代表一个已接入的数据源，
其 `properties.database` 即物理库名。只要某个库在 Wren 项目里建模过，
前端选它时就走 WrenAI 语义层（`wrenai_run_sql`），否则走 db_mcp_server 直连。

用法：
    detector = SemanticDbDetector()
    detector.discover()          # → {"imdb"}（已建模物理库名集合）
    detector.is_modeled("imdb")  # → True

新增 wren 语义库（往项目加 connection_*.json + models）后无需改代码，
重启或重新 discover 即自动感知。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)


class SemanticDbDetector:
    """从 Wren 项目目录扫描已建模数据源。"""

    def __init__(self, project_path: Optional[str] = None) -> None:
        from agent.settings.setting import settings

        self._project_path = Path(project_path or settings.WREN_PROJECT_PATH)
        self._lock = threading.RLock()
        self._cache: set[str] = set()
        self._cache_valid = False

    def _scan(self) -> set[str]:
        """扫描 Wren 项目 config/connection_*.json，取 properties.database。"""
        names: set[str] = set()
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
            _logger.info("[semantic_db] 已建模数据源: %s", sorted(names))
        return names

    def discover(self, refresh: bool = False) -> set[str]:
        """返回已建模物理库名集合（带缓存）。"""
        with self._lock:
            if not self._cache_valid or refresh:
                self._cache = self._scan()
                self._cache_valid = True
            return set(self._cache)

    def is_modeled(self, db_name: str) -> bool:
        """判断某库是否已建模（语义层）。"""
        return db_name in self.discover()


# 单例
_detector: Optional[SemanticDbDetector] = None


def get_detector() -> SemanticDbDetector:
    global _detector
    if _detector is None:
        _detector = SemanticDbDetector()
    return _detector
