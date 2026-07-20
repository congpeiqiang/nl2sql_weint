"""
@File    :  setting.py
@Author  :  CongPeiQiang
@Time    :  2026/7/19 11:06
@Desc    :  
"""
import os
import logging
from typing import Any, Dict, List

from dotenv import load_dotenv
from pydantic_settings import BaseSettings
load_dotenv()
logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    # LLM配置
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-v4-flash")

    # 日志配置
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", "agentic_rag.log")
    ENABLE_DETAILED_LOGGING: bool = os.getenv("ENABLE_DETAILED_LOGGING", "true").lower() == "true"

    # MySQL 多数据库配置（支持 _N 后缀）
    @staticmethod
    def get_databases() -> list[dict]:
        """从 .env 读取所有数据库配置，返回列表"""
        dbs = []
        i = 1
        while True:
            name = os.getenv(f"DB_{i}_NAME")
            if not name:
                break
            dbs.append({
                "name": name,
                "host": os.getenv(f"DB_{i}_HOST", "localhost"),
                "port": int(os.getenv(f"DB_{i}_PORT", "3306")),
                "user": os.getenv(f"DB_{i}_USER", "root"),
                "password": os.getenv(f"DB_{i}_PASSWORD", ""),
            })
            i += 1
        return dbs

    # 默认数据库（取 DB_1_* 作为默认值）
    DB_HOST: str = os.getenv("DB_1_HOST", "localhost")
    DB_PORT: int = int(os.getenv("DB_1_PORT", "3306"))
    DB_NAME: str = os.getenv("DB_1_NAME", "")
    DB_USER: str = os.getenv("DB_1_USER", "root")
    DB_PASSWORD: str = os.getenv("DB_1_PASSWORD", "")

    # LangSmith配置
    LANGSMITH_TRACING: bool = os.getenv("LANGSMITH_TRACING", "false").lower() == "true"
    LANGSMITH_ENDPOINT: str = os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    LANGSMITH_API_KEY: str = os.getenv("LANGSMITH_API_KEY", "")
    LANGSMITH_PROJECT: str = os.getenv("LANGSMITH_PROJECT", "default")


    class Config:
        case_sensitive = True
        env_file = ".env"
        extra = "ignore"  # 允许 .env 中的 DB_N_* 等未定义字段

    def validate_configuration(self) -> List[str]:
        """
        验证配置的有效性

        Returns:
            配置问题列表，空列表表示配置正常
        """
        issues = []

        # 检查必需的API密钥
        if not self.LLM_API_KEY:
            issues.append("LLM_API_KEY is required for DeepSeek models")

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
            "LLM_API_KEY", "SECRET_KEY"
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