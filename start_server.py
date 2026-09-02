#!/usr/bin/env python3
"""
Simple LangGraph API Server

A minimal script to start the LangGraph API server directly using uvicorn.
"""

import os
import sys
import json
from pathlib import Path

# ── 文件日志（自动轮转、落盘固定路径）─────────────────────────
# 日志目录固定在项目根下 logs/，无论从哪个工作目录启动都解析到同一位置。
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "agent-server.log"
# 轮转：每天 0 点轮转一个文件，保留最近 7 个历史文件 + 当前文件
LOG_WHEN = "midnight"
LOG_INTERVAL = 1
LOG_BACKUP_COUNT = 7

def setup_environment():
    """Setup required environment variables"""
    # 固定 CWD 为仓库根：inmem 版型的线程/run 注册表落盘路径
    # (.langgraph_api/.langgraph_ops.pckl) 是相对 CWD 的，换目录启动会拿到一份
    # 全新的注册表 → 重启后旧线程在注册表里查不到，前端复用旧 threadId 就 404。
    # 统一 CWD 让注册表永远落在同一位置，跨重启保留（配合前端"线程失效自动开新会话"）。
    script_dir = Path(__file__).resolve().parent
    if os.getcwd() != str(script_dir):
        os.chdir(script_dir)

    # Add src to Python path

    src_path = Path(__file__).parent / "src"
    sys.path.insert(0, str(src_path))
    
    # Load graphs and checkpointer from graph.json
    config_path = Path(__file__).parent / "graph.json"
    graphs = {}
    checkpointer_config = None
    store_config = None

    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
            graphs = config.get("graphs", {})
            checkpointer_config = config.get("checkpointer")
            store_config = config.get("store")
    
    # Force UTF-8 encoding for all file I/O (fixes GBK decode errors on Windows)
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"

    # Build environment dict
    env_updates = {
        "DATABASE_URI": ":memory:",
        "REDIS_URI": "fake",
        "MIGRATIONS_PATH": "__inmem",
        # Server configuration
        "ALLOW_PRIVATE_NETWORK": "true",
        "LANGGRAPH_UI_BUNDLER": "true",
        "LANGGRAPH_RUNTIME_EDITION": "inmem",
        "LANGGRAPH_DISABLE_FILE_PERSISTENCE": "false",
        "LANGGRAPH_ALLOW_BLOCKING": "true",
        "LANGGRAPH_API_URL": "http://localhost:2026",
        # Graphs configuration
        "LANGSERVE_GRAPHS": json.dumps(graphs) if graphs else "{}",
        # Worker configuration
        "N_JOBS_PER_WORKER": "10",
        "LANGGRAPH_RECURSION_LIMIT": "500",
    }

    # Custom checkpointer configuration (from graph.json)
    if checkpointer_config:
        env_updates["LANGGRAPH_CHECKPOINTER"] = json.dumps(checkpointer_config)

    # Custom store configuration (from graph.json)
    if store_config:
        env_updates["LANGGRAPH_STORE"] = json.dumps(store_config)

    # 仅设置默认值，不覆盖 Docker / 外部已传入的环境变量
    for k, v in env_updates.items():
        os.environ.setdefault(k, v)
    
    # Load .env file if exists
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file)
            print(f"✅ Loaded environment from .env")
        except ImportError:
            print("⚠️  python-dotenv not installed, skipping .env file")

    # DEPLOY_ENV 显式化：打印当前环境 + prod 下校验 checkpoint 后端，
    # 避免排查时靠 CHECKPOINT_DB_URI 的值猜环境。
    deploy_env = os.environ.get("DEPLOY_ENV", "dev")
    if deploy_env == "prod":
        if not os.environ.get("CHECKPOINT_DB_URI", "").startswith("postgresql://"):
            print("⚠️ DEPLOY_ENV=prod 但 CHECKPOINT_DB_URI 未指向 PostgreSQL，请检查 docker-compose / 环境配置")
    print(f"🌍 Deploy env: {deploy_env}")

def preflight_check():
    """就绪门控：在启动服务前验证关键依赖是否就绪。"""
    print("\n🔍 执行启动预检...", flush=True)

    # ── 检查 MCP 工具 ──
    try:
        from agent.tools.mcp_tool import tools, _mcp_server_results, MCPToolsLoadError
    except ImportError as e:
        print(f"\n🚫 预检失败: MCP 工具模块导入失败（缺少依赖）: {e}", flush=True)
        sys.exit(1)
    except Exception as e:
        print(f"\n🚫 预检失败: MCP 工具模块加载异常: {e}", flush=True)
        sys.exit(1)

    if not tools:
        print(f"\n🚫 就绪门控: MCP 工具列表为空，服务不启动。", flush=True)
        sys.exit(1)

    # 报告各服务器状态
    for name, status in _mcp_server_results.items():
        icon = "✅" if status == "ok" else "❌"
        label = "正常" if status == "ok" else status
        print(f"  {icon} MCP [{name}]: {label}", flush=True)

    # ── 检查 Langfuse 连通性（总开关关闭则跳过；告警不阻断启动）──
    try:
        from agent.trace.langfuse_client import auth_check, langfuse_enabled
        if not langfuse_enabled():
            print("  ⏭ Langfuse: LANGFUSE_ENABLE 未开启，跳过埋点预检", flush=True)
        elif auth_check():
            print("  ✅ Langfuse: 云端连通", flush=True)
        else:
            print("  ⚠️ Langfuse: auth_check 失败（仍启动，trace 可能不落库）", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ Langfuse: 预检异常（仍启动）: {e}", flush=True)

    print(f"✅ 预检通过: {len(tools)} 个 MCP 工具就绪\n", flush=True)


def main():
    """Start the server"""
    print("🚀 Starting API Server...")

    # Setup environment
    setup_environment()

    # 就绪门控：验证关键依赖
    preflight_check()

    # 文件日志目录：确保存在（uvicorn log_config 的 FileHandler 需要）
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Print server information
    print("\n" + "="*60)
    print("📍 Server URL: http://localhost:2026")
    print("📚 API Documentation: http://localhost:2026/docs")
    print("🎨 Studio UI: http://localhost:2026/ui")
    print("💚 Health Check: http://localhost:2026/ok")
    print(f"📜 File Log: {LOG_FILE}")
    print(f"   (每天轮转，保留最近 {LOG_BACKUP_COUNT} 个历史文件)")
    print("="*60)
    
    try:
        # Import uvicorn after environment setup
        import uvicorn
        
        # Start the server directly
        uvicorn.run(
            "langgraph_api.server:app",
            host="0.0.0.0",
            port=2026,
            reload=False,
            access_log=False,
            log_config={
                "version": 1,
                "disable_existing_loggers": False,
                "formatters": {
                    "default": {
                        "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    }
                },
                "handlers": {
                    "default": {
                        "formatter": "default",
                        "class": "logging.StreamHandler",
                        "stream": "ext://sys.stdout",
                    },
                    "file": {
                        "formatter": "default",
                        "class": "logging.handlers.TimedRotatingFileHandler",
                        "filename": str(LOG_FILE),
                        "when": LOG_WHEN,
                        "interval": LOG_INTERVAL,
                        "backupCount": LOG_BACKUP_COUNT,
                        "encoding": "utf-8",
                        "delay": True,
                    }
                },
                "root": {
                    "level": "INFO",
                    "handlers": ["default", "file"],
                },
                "loggers": {
                    "uvicorn": {"level": "INFO"},
                    "uvicorn.error": {"level": "INFO"},
                    "uvicorn.access": {"level": "WARNING"},
                }
            }
        )
    except KeyboardInterrupt:
        print("\n🛑 Server stopped by user")
    except Exception as e:
        print(f"❌ Server failed to start: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
if __name__ == "__main__":
    main()
