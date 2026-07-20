"""
版权所有 (c) 2023-2026 北京慧测信息技术有限公司(但问智能) 保留所有权利。

本代码版权归北京慧测信息技术有限公司(但问智能)所有，仅用于学习交流目的，未经公司商业授权，
不得用于任何商业用途，包括但不限于商业环境部署、售卖或以任何形式进行商业获利。违者必究。

授权商业应用请联系微信：huice666
"""

import os
import logging
from typing import Any, Dict, List

from dotenv import load_dotenv
from pydantic_settings import BaseSettings
load_dotenv()
logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    # 数据库配置
    DB_TYPE: str = os.getenv("DB_TYPE", "mysql")
    SQLITE_DB_PATH: str = os.getenv("SQLITE_DB_PATH", "data/db.sqlite")
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: int = int(os.getenv("DB_PORT", "5432"))
    DB_NAME: str = os.getenv("DB_NAME", "")
    DB_USER: str = os.getenv("DB_USER", "")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_EXTRA_CONFIG: str = os.getenv("DB_EXTRA_CONFIG", "")

    # MCP Server 传输配置
    NL2SQL_MCP_TRANSPORT: str = os.getenv("NL2SQL_MCP_TRANSPORT", "stdio")
    NL2SQL_MCP_HOST: str = os.getenv("NL2SQL_MCP_HOST", "0.0.0.0")
    NL2SQL_MCP_PORT: int = int(os.getenv("NL2SQL_MCP_PORT", "8000"))

    class Config:
        case_sensitive = True
        env_file = ".env"

    def validate_configuration(self) -> List[str]:
        """
        验证配置的有效性

        Returns:
            配置问题列表，空列表表示配置正常
        """
        issues = []
        # 检查数据库配置
        if self.DB_TYPE == "sqlite":
            if not self.SQLITE_DB_PATH:
                issues.append("SQLITE_DB_PATH is required when DB_TYPE=sqlite")
        else:
            if not self.DB_HOST:
                issues.append("DB_HOST is required for non-SQLite databases")
            if not self.DB_NAME:
                issues.append("DB_NAME is required for non-SQLite databases")
            if not self.DB_USER:
                issues.append("DB_USER is required for non-SQLite databases")
            if not self.DB_PASSWORD:
                issues.append("DB_PASSWORD is required for non-SQLite databases")

        return issues

    def get_safe_config(self) -> Dict[str, Any]:
        """
        获取安全的配置信息（隐藏敏感信息）

        Returns:
            安全的配置字典
        """
        config = self.model_dump()

        # 隐藏敏感信息
        sensitive_keys = [
            "SECRET_KEY", "DB_PASSWORD"
        ]

        for key in sensitive_keys:
            if key in config and config[key]:
                config[key] = "***" + config[key][-4:] if len(config[key]) > 4 else "***"

        return config


def create_settings() -> Settings:
    """创建并验证设置"""
    settings = Settings()

    # 验证配置
    issues = settings.validate_configuration()
    if issues:
        logger.warning("Configuration issues found:")
        for issue in issues:
            logger.warning(f"  - {issue}")

    return settings

# 该代码会自动执行，并且只会执行一次（单例设计模式）
settings = create_settings()

