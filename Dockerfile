# ── Stage 1: 构建依赖 ──
FROM python:3.13-slim AS builder

WORKDIR /app

# 系统依赖：mysqlclient 编译需要 gcc + libmariadb-dev
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libmariadb-dev pkg-config && \
    rm -rf /var/lib/apt/lists/*

# 复制依赖描述文件，利用 Docker 缓存层
COPY pyproject.toml uv.lock ./

# 用 pip 安装 uv（避免 ghcr.io 国内慢），再用 uv sync 安装依赖
RUN pip install --no-cache-dir uv -i https://mirrors.aliyun.com/pypi/simple/ && \
    uv sync --no-dev --no-install-project --extra-index-url https://pypi.org/simple/ \
    || uv sync --no-dev --no-install-project

# ── Stage 2: 运行时 ──
FROM python:3.13-slim

# 系统依赖：mysqlclient 运行时 + curl（健康检查）+ xz-utils（解压 Node.js）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmariadb3 curl xz-utils && \
    rm -rf /var/lib/apt/lists/*

# Node.js 20（chart MCP 需要 npx）—— 用 npmmirror 加速
RUN curl -fsSL https://npmmirror.com/mirrors/node/v20.18.0/node-v20.18.0-linux-x64.tar.xz \
    | tar -xJ -C /usr/local --strip-components=1

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
