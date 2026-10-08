#!/usr/bin/env python3
"""
Simple LangGraph API Server

A minimal script to start the LangGraph API server directly using uvicorn.
"""

import os
import sys
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

# ── 文件日志（自动轮转、落盘持久目录）─────────────────────────
# P2-2：日志目录**不再写死在仓库根**——容器里仓库根 = 容器可写层，`docker-compose rm`
# 重建容器（每次发版都做）即丢；而「重启后还能查到上一次运行期间的日志」正是 P2-2 的验收。
# 解析顺序（`resolve_log_dir()`）：
#   ① `NL2SQL_LOG_DIR`：显式覆盖（排查/临时用）
#   ② `<AGENT_DATA_ROOT>/logs`：生产 = `/app/data/logs`，**已经是持久卷** → 重建容器不丢
#   ③ `<仓库根>/logs`：兜底（未配置 AGENT_DATA_ROOT 的 dev/CLI 场景，行为与改动前一致）
# 注意 ② 与 `shared/`、`workspace/` 同级：agent 的文件读权限只放行 /shared/** 与
# /workspace/**（agent/settings/file_permissions.py），`/logs/**` 对模型不可读 —— 日志里有
# 别人的请求路径/用户名/会话 id，**别把日志挪进那两个目录**（见 api/request_log.py 文件头）。
LOG_FILE_NAME = "agent-server.log"
# 轮转：每天 0 点轮转一个文件，保留最近 7 个历史文件 + 当前文件
LOG_WHEN = "midnight"
LOG_INTERVAL = 1
LOG_BACKUP_COUNT = 7


def resolve_log_dir() -> Path:
    """解析日志目录（见上方三条顺序）。**必须在 setup_environment() 之后调用** ——
    `.env` / `.env.prod` 里的 AGENT_DATA_ROOT 是那时才进 os.environ 的。"""
    override = (os.environ.get("NL2SQL_LOG_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    data_root = (os.environ.get("AGENT_DATA_ROOT") or "").strip()
    if data_root:
        return Path(data_root) / "logs"
    return Path(__file__).resolve().parent / "logs"


def resolve_log_file() -> Path:
    return resolve_log_dir() / LOG_FILE_NAME

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
            auth_config = config.get("auth")
    
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

    # Custom auth configuration (from graph.json)
    if auth_config:
        env_updates["LANGGRAPH_AUTH"] = json.dumps(auth_config)

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

# ── postgres 版型的 Go core（蓝绿阶段 1，2026-10-05）──────────────────────
# 换到 postgres 版型后，平台存储（threads / runs / assistants / crons）的 CRUD **全部**是对
# Go core 的 gRPC 调用（`langgraph_api/feature_flags.py`：IS_POSTGRES_OR_GRPC_BACKEND →
# `langgraph_api.grpc.ops`，Python 侧没有回退实现）⇒ core 是**必需进程**，不是可选组件。
# 官方镜像用它的 entrypoint 起 core；我们的镜像由本文件负责，两个理由：
#   ① core 必须**继承 `LANGSERVE_GRAPHS`**，而那个值只在 `setup_environment()` 里拼出来。
#      先起 core 再进 Python（官方顺序）在我们这儿会踩：core 拿到空图表 ⇒ 建助手时
#      `NOT_FOUND: graph 'chat_agent' not found` ⇒ lifespan 起不来、整个服务退出。
#      **2026-10-05 阶段 0 实测踩过这条**。
#   ② core 的 stdout 要落进我们的日志文件、退出要随本进程回收（官方是同一进程组里 exec）。
CORE_BINARY = "core-api-grpc"
CORE_READY_TIMEOUT_SECS = 30


def _core_address() -> tuple[str, int]:
    """core 的 gRPC 监听地址（`LSD_GRPC_SERVER_ADDRESS`，默认 `localhost:50051`）。"""
    raw = (os.environ.get("LSD_GRPC_SERVER_ADDRESS") or "localhost:50051").strip()
    if ":" in raw:
        host, _, port = raw.rpartition(":")
    elif raw.isdigit():
        host, port = "localhost", raw      # 只给了端口
    else:
        host, port = raw, "50051"          # 只给了主机名
    try:
        return host, int(port)
    except ValueError:
        print(f"⚠️ LSD_GRPC_SERVER_ADDRESS 解析不了（{raw!r}），按 localhost:50051 处理")
        return "localhost", 50051


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def start_core_api_grpc(log_dir: Path) -> subprocess.Popen | None:
    """postgres 版型：拉起 `core-api-grpc` 并等它开始监听；其它版型直接返回 None。

    **必须在 `setup_environment()` 之后调用**（见上方注释 ①）。
    """
    if (os.environ.get("LANGGRAPH_RUNTIME_EDITION") or "").strip().lower() != "postgres":
        return None

    # 版型对了但库没接上：`setup_environment` 的默认值 `:memory:` 会留在这儿，
    # core 连不上平台库时的报错很难看出是配置问题 —— 直接点名。
    db_uri = (os.environ.get("DATABASE_URI") or "").strip()
    if not db_uri.startswith("postgres"):
        print(f"\n🚫 LANGGRAPH_RUNTIME_EDITION=postgres 但 DATABASE_URI={db_uri!r}（应是 postgresql://…）",
              flush=True)
        print("   ⇒ 平台存储（会话列表/run/助手）需要平台库；请检查 docker-compose 的 DATABASE_URI",
              flush=True)
        sys.exit(1)

    host, port = _core_address()
    # 幂等：地址上已经有监听者（compose 单独起了 core / 上一轮残留）就不再起第二个
    if _tcp_open(host, port):
        print(f"ℹ️ {host}:{port} 已有服务在监听，跳过拉起 {CORE_BINARY}", flush=True)
        return None

    log_path = log_dir / "core-api-grpc.log"
    print(f"🧩 postgres 版型：拉起 {CORE_BINARY}（gRPC {host}:{port}，日志 {log_path}）", flush=True)
    # stdout/stderr 进文件而不是 docker logs：core 启动时会打一坨 Datadog tracer 配置
    # （连不上 localhost:8126 的失败告警），混进主日志会把发版脚本 grep 的关键行淹掉。
    handle = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(  # noqa: S603
            [CORE_BINARY],
            stdout=handle, stderr=subprocess.STDOUT,
            env=os.environ.copy(),          # ← 含 LANGSERVE_GRAPHS / DATABASE_URI / REDIS_URI
        )
    except FileNotFoundError:
        print(f"\n🚫 找不到 {CORE_BINARY} —— postgres 版型要求它随镜像分发（见 Dockerfile 的多阶段 COPY）",
              flush=True)
        sys.exit(1)
    finally:
        handle.close()

    deadline = time.monotonic() + CORE_READY_TIMEOUT_SECS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"\n🚫 {CORE_BINARY} 启动即退出（exit={proc.returncode}），tail 日志：", flush=True)
            print(_tail(log_path), flush=True)
            sys.exit(1)
        if _tcp_open(host, port):
            print(f"✅ {CORE_BINARY} 已就绪（pid={proc.pid}，{host}:{port}）", flush=True)
            threading.Thread(target=_watch_core, args=(proc, log_path), daemon=True).start()
            return proc
        time.sleep(0.2)

    print(f"\n🚫 等 {CORE_READY_TIMEOUT_SECS}s 仍没等到 {CORE_BINARY} 监听 {host}:{port}，tail 日志：",
          flush=True)
    print(_tail(log_path), flush=True)
    stop_core_api_grpc(proc)
    sys.exit(1)


def _tail(path: Path, lines: int = 20) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except Exception:  # noqa: BLE001  排障信息读不到不该盖住原错误
        return "(读不到日志)"


def _watch_core(proc: subprocess.Popen, log_path: Path) -> None:
    """core 是「没了就全废」的进程（平台存储全走它），退出必须显式吵一声。"""
    rc = proc.wait()
    print(f"\n🚨 [core-api-grpc] 进程退出（exit={rc}）—— 会话列表/run/助手的读写会全部失败；"
          f"日志见 {log_path}", flush=True)


def stop_core_api_grpc(proc: subprocess.Popen | None, timeout: float = 10.0) -> None:
    """回收 core（SIGTERM → 到点 SIGKILL）。它是我们的子进程，容器停时不该留孤儿。"""
    if proc is None or proc.poll() is not None:
        return
    print(f"🛑 回收 {CORE_BINARY}（pid={proc.pid}）", flush=True)
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"⚠️ {CORE_BINARY} {timeout}s 内没退出，SIGKILL", flush=True)
        proc.kill()


def preflight_check():
    """就绪门控：在启动服务前验证关键依赖是否就绪。"""
    print("\n🔍 执行启动预检...", flush=True)

    # ── 检查 MCP 工具 ──
    try:
        from agent.tools.mcp_tool import (
            tools, _mcp_server_results, _mcp_server_tool_counts,
            evaluate_mcp_preflight, MCPToolsLoadError,
        )
    except ImportError as e:
        print(f"\n🚫 预检失败: MCP 工具模块导入失败（缺少依赖）: {e}", flush=True)
        sys.exit(1)
    except Exception as e:
        print(f"\n🚫 预检失败: MCP 工具模块加载异常: {e}", flush=True)
        sys.exit(1)

    if not tools:
        print(f"\n⚠️ MCP 工具列表为空（所有 server 加载失败或被跳过），服务仍启动但功能受限。", flush=True)
        print("   请通过前端 UI 配置数据库/语义库/模型后，对应 MCP 工具将自动加载。\n", flush=True)

    # 报告各服务器状态（带工具数）
    for name, status in _mcp_server_results.items():
        icon = "✅" if status == "ok" else "❌"
        label = f"正常 ({_mcp_server_tool_counts.get(name, 0)} tools)" if status == "ok" else status
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

    # ── 按 server 的预检判决（2026-09-08 教训：wrenai_WIT 加载失败被总数
    #    "✅ 预检通过: 20" 掩盖 → 语义层静默缺失，建模库查询全线退化成
    #    10 分钟撞墙 + 僵尸续跑。关键 server（wrenai_*/dbmcp）缺失 = 拒绝启动；
    #    MCP_ALLOW_DEGRADED=1 可显式降级）──
    v = evaluate_mcp_preflight()
    # 报告被跳过的 server（无配置 → 不加载，区别于加载失败）
    for n in v.get("skipped", []):
        if n == "dbmcp":
            print("  ⏭ dbmcp: 未配置任何数据库，已跳过（通过 UI 添加后自动加载）", flush=True)

    if v["ok"]:
        breakdown = ", ".join(f"{n}={c}" for n, c in sorted(v["counts"].items())) or "-"
        skipped_note = f"，跳过 {', '.join(v['skipped'])}" if v["skipped"] else ""
        print(f"✅ 预检通过: {len(tools)} 个 MCP 工具就绪（{breakdown}{skipped_note}）\n", flush=True)
        return

    print("\n" + "!" * 64, flush=True)
    print("❌❌❌ MCP 启动预检失败 —— 以下 server 未就绪 ❌❌❌", flush=True)
    for n, reason in v["failed"].items():
        crit = "  [CRITICAL]" if n in v["critical_failed"] else ""
        print(f"  ❌ {n}{crit}: {reason}", flush=True)
    print("!" * 64, flush=True)
    print("  排障提示:", flush=True)
    print("  - wrenai_*: 检查语义库项目目录（前端设置→语义库面板）target/mdl.json 是否存在；", flush=True)
    print("    缺失 → 「🔄 更新」/「从远程仓库拉取」或 wren context build 后重启后端", flush=True)
    print("  - dbmcp: 检查 db_config 与 fastmcp 依赖", flush=True)
    print("  - mcp-server-echarts: 检查 node/npx 可用性（非关键，可降级）", flush=True)

    if v["block_startup"]:
        print(f"\n🚫 预检失败: 关键 MCP server 不可用（{', '.join(v['critical_failed'])}），服务不启动。", flush=True)
        print("   确需降级启动（不建议：语义层/SQL 执行不可用，建模库查询整体失效）:", flush=True)
        print("   设置 MCP_ALLOW_DEGRADED=1 后重启\n", flush=True)
        sys.exit(1)
    print(f"\n⚠️ 预检失败(降级启动): 非关键 server 缺失（{', '.join(v['failed'])}），"
          f"可用工具 {len(tools)} 个\n", flush=True)


def build_log_config(log_file: Path) -> dict:
    """uvicorn 的 log_config（抽成函数是为了让验证脚本能直接断言这份配置本身）。

    P2-2 的两处关键：① formatter 带 `%(request_id)s` + 两个 handler 都挂
    `RequestIdFilter` → 每条日志（含业务日志与 langgraph 自己的）都带 rid；
    ② file handler 是 TimedRotatingFileHandler，落盘到**持久目录**（见 resolve_log_dir）。

    访问日志的归属：**不走 uvicorn 的 access logger**（`access_log=False`），由
    `api/request_log.RequestContextMiddleware` 另记一份带耗时/rid 的 —— 见该模块文件头。
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            # P2-2：把当前请求的 rid 注入每条日志记录（无请求上下文 → "-"）。
            # 挂在 handler 上，覆盖所有 logger（含 langgraph 自己的）。
            "request_id": {"()": "api.request_log.RequestIdFilter"},
        },
        "formatters": {
            "default": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - [rid=%(request_id)s] %(message)s",
            }
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "filters": ["request_id"],
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "formatter": "default",
                "filters": ["request_id"],
                "class": "logging.handlers.TimedRotatingFileHandler",
                "filename": str(log_file),
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
            # 自带 access logger 关掉（本文件已用 request_log 替代，见 access_log 注释）
            "uvicorn.access": {"level": "WARNING"},
        },
    }


def main():
    """Start the server"""
    print("🚀 Starting API Server...")

    # Setup environment
    setup_environment()

    # 文件日志目录：确保存在（uvicorn log_config 的 FileHandler 需要；core 的日志也落这儿）
    log_file = resolve_log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # postgres 版型：先拉起 Go core 再进预检 —— 预检（加载 MCP 工具）要跑几秒，
    # 正好给 core 冷启当并行窗口（core 未就绪时平台存储的任何调用都会被拒）。
    core_proc = start_core_api_grpc(log_file.parent)

    # 就绪门控：验证关键依赖
    preflight_check()

    # Print server information
    print("\n" + "="*60)
    print("📍 Server URL: http://localhost:2026")
    print("📚 API Documentation: http://localhost:2026/docs")
    print("🎨 Studio UI: http://localhost:2026/ui")
    print("💚 Health Check: http://localhost:2026/ok")
    print(f"📜 File Log: {log_file}")
    print(f"   (每天轮转，保留最近 {LOG_BACKUP_COUNT} 个历史文件；P2-2 起落在持久目录)")
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
            # P2-2：**保持 False** —— 访问日志由 `api/request_log.RequestContextMiddleware`
            # 自己记（那才是带耗时/rid 的那份）。开成 True 只会让每个请求多一行无语义重复的
            # 日志（uvicorn 自带格式里没有 duration，也没有 rid）。
            access_log=False,
            log_config=build_log_config(log_file),
        )
    except KeyboardInterrupt:
        print("\n🛑 Server stopped by user")
    except Exception as e:
        print(f"❌ Server failed to start: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        # uvicorn 退出（正常停机 / 异常）后回收 core：它是本进程的子进程，
        # 留成孤儿会让容器迟迟不退出（docker stop 的宽限期白等）。
        stop_core_api_grpc(core_proc)
if __name__ == "__main__":
    main()
