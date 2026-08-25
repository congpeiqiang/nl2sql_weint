# ── Stage 1: 构建依赖 ──
FROM python:3.13-slim AS builder

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 系统依赖：mysqlclient 编译需要 gcc + libmariadb-dev
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libmariadb-dev pkg-config && \
    rm -rf /var/lib/apt/lists/*

# 先复制依赖描述文件，利用 Docker 缓存层
COPY pyproject.toml uv.lock ./

# 同步依赖（--frozen 严格按 lock 文件；如果 lock 未含新依赖则去掉 --frozen）
# 额外索引兜底：langgraph-checkpoint-postgres 等可能在私有镜像中缺失
RUN uv sync --frozen --no-dev --no-install-project \
    --extra-index-url https://pypi.org/simple/ \
    || uv sync --no-dev --no-install-project --extra-index-url https://pypi.org/simple/

# ── Stage 2: 运行时 ──
FROM python:3.13-slim

# 系统依赖：mysqlclient 运行时 + Node.js 20（chart MCP 需要 npx）+ curl（健康检查）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmariadb3 curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制 Python 虚拟环境（从 builder 阶段）
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# 复制项目源码
COPY . .

# 默认环境变量（可被 docker-compose / .env.prod 覆盖）
ENV WREN_BIN_PATH=wren \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8

EXPOSE 2026

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -sf http://localhost:2026/ok || exit 1

CMD ["python", "start_server.py"]
