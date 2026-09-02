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
from pathlib import Path

# 从项目根目录按 DEPLOY_ENV 选择环境文件（无论从哪里运行）。
# DEPLOY_ENV 来自「启动前已注入的进程环境」（docker-compose environment: DEPLOY_ENV=prod
# / 手动 export），绝不能从要选择的文件里读回（先有鸡先有蛋）；未设默认 dev。
# override=False：已注入的进程环境恒优先，杜绝 prod 值被文件里的 dev 值覆盖——
# 旧实现 override=True 读 .env 依赖 .dockerignore 排除 .env 兜底，属结构隐患。
_deploy_env = os.getenv("DEPLOY_ENV", "dev")
_env_path = Path(__file__).resolve().parents[3] / (".env.prod" if _deploy_env == "prod" else ".env")
load_dotenv(_env_path, override=False)
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
    DB_HOST: str = os.getenv("DB_3_HOST", "localhost")
    DB_PORT: int = int(os.getenv("DB_3_PORT", "3306"))
    DB_NAME: str = os.getenv("DB_3_NAME", "")
    DB_USER: str = os.getenv("DB_3_USER", "root")
    DB_PASSWORD: str = os.getenv("DB_3_PASSWORD", "")

    # LangSmith配置
    LANGSMITH_TRACING: bool = os.getenv("LANGSMITH_TRACING", "false").lower() == "true"
    LANGSMITH_ENDPOINT: str = os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    LANGSMITH_API_KEY: str = os.getenv("LANGSMITH_API_KEY", "")
    LANGSMITH_PROJECT: str = os.getenv("LANGSMITH_PROJECT", "default")

    # Wren 项目地址（缺省用空串而非 None，避免 pydantic str 字段收到 None 直接崩；
    # 语义层 default 项目未配时走显式 wren_project，见 semantic_db._scan_legacy）
    WREN_PROJECT_PATH: str = os.getenv("WREN_PROJECT_PATH", "")
    WREN_BIN_PATH: str = os.getenv("WREN_BIN_PATH", "")
    # Wren MCP server 记忆检索后端：grep（token-overlap，毫秒级）或 lancedb
    # （sentence-transformers + LanceDB 向量检索，首次 3-10s）。默认 grep，
    # 知识库规模扩大后可切 lancedb。
    WREN_MEMORY_BACKEND: str = os.getenv("WREN_MEMORY_BACKEND", "grep")

    # db_mcp_server 直连通道（子智能体第二路 MCP：dbmcp_run_sql，按 db_name 路由）
    NL2SQL_DBMCP_ENABLED: bool = os.getenv("NL2SQL_DBMCP_ENABLED", "1").lower() not in (
        "0", "false", "no", "off"
    )

    # 图表引擎（仅支持 echarts）
    CHART_ENGINE: str = os.getenv("CHART_ENGINE", "echarts")

    CHECKPOINT_DB_PATH: str = os.getenv("CHECKPOINT_DB_PATH", "")
    # PostgreSQL checkpoint URI（Docker 生产环境使用；为空则回退 SQLite）
    CHECKPOINT_DB_URI: str = os.getenv("CHECKPOINT_DB_URI", "")

    # ── 工作区配置 ──────────────────────────────────────────
    WORKSPACE_REGISTRY_PATH: str = os.getenv("WORKSPACE_REGISTRY_PATH", "")
    WORKSPACE_PATH: str = os.getenv("WORKSPACE_PATH", "")
    # 共享资源目录（memory/、skills/ 的父目录），默认由 WorkspaceManager 推导
    SHARED_RESOURCES_PATH: str = os.getenv("SHARED_RESOURCES_PATH", "")
    # 外部基础目录（项目外）：shared + 默认工作区统一放这里（代码根退出 VFS），
    # 首次运行自动从仓库 src/agent/{shared,workspace} 拷贝种子；为空则回退仓库内
    AGENT_DATA_ROOT: str = os.getenv("AGENT_DATA_ROOT", "")

    class Config:
        case_sensitive = True
        # 与模块顶部 load_dotenv 同源（prod → .env.prod，dev → .env）；绝对路径避免
        # 依赖 CWD。pydantic-settings 以进程 env 优先，env_file 仅补缺。
        env_file = str(_env_path)
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