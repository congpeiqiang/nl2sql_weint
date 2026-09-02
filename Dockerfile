# ── 阿里云 Debian 镜像源（python:3.13-slim 基于 Debian Trixie）──
ARG DEBIAN_MIRROR="mirrors.aliyun.com"

# ── Stage 1: 构建依赖 ──
FROM python:3.13-slim AS builder
ARG DEBIAN_MIRROR

# 替换 apt 源为阿里云镜像（加速国内下载）
RUN sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list 2>/dev/null; \
    true

WORKDIR /app

# 系统依赖：mysqlclient 编译需要 gcc + libmariadb-dev
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libmariadb-dev pkg-config && \
    rm -rf /var/lib/apt/lists/*

# 复制依赖描述文件，利用 Docker 缓存层
COPY pyproject.toml uv.lock ./

# Docker 不需要 memory extra（WREN_MEMORY_BACKEND=grep），去掉可省 1.3GB（PyTorch+CUDA）
RUN sed -i 's/wrenai\[clickhouse,memory,postgres\]/wrenai[clickhouse,postgres]/' pyproject.toml

# 用 pip 安装 uv（避免 ghcr.io 国内慢），再用 uv sync 安装依赖
# 不加 --frozen（pyproject.toml 被 sed 修改，需要重新解析）
RUN pip install --no-cache-dir uv -i https://mirrors.aliyun.com/pypi/simple/ && \
    uv sync --no-dev --no-install-project --extra-index-url https://pypi.org/simple/ \
    || uv sync --no-dev --no-install-project

# Qwen 支持：ChatQwen 需要 langchain_qwq（前端切 qwen provider 时报
# "No module named 'langchain_qwq'"）。单独装进 builder venv（不进 pyproject/uv.lock，
# 避免依赖解析影响既有锁定版本）；随 .venv 一并 COPY 到运行时。
RUN uv pip install --python /app/.venv/bin/python langchain-qwq \
    --extra-index-url https://pypi.org/simple/

# ── Stage 2: 运行时 ──
FROM python:3.13-slim
ARG DEBIAN_MIRROR

# 替换 apt 源为阿里云镜像
RUN sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list 2>/dev/null; \
    true

# 系统依赖：mysqlclient 运行时 + curl（健康检查）+ xz-utils（解压 Node.js）
# + tzdata（设置容器时区，修复日志时间显示 UTC 的问题）
# + git（语义库 git 版本化/推送：push_to_git 用 subprocess 调系统 git）
# + openssh-client（GitLab 实例禁 HTTP git 访问，语义库需走 ssh:// 推送）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmariadb3 curl xz-utils tzdata vim-tiny git openssh-client && \
    rm -rf /var/lib/apt/lists/*

# 创建 vi 软链接（-f 强制：vim-tiny 的 alternatives 可能已建 /usr/bin/vi，
# 硬 ln -s 会因 File exists 失败挂掉构建）
RUN ln -sf /usr/bin/vim.tiny /usr/bin/vi

# 容器时区：Asia/Shanghai（否则 Python logging 的 asctime 显示 UTC，比北京慢 8 小时）
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Node.js 20（chart MCP 需要 npx）—— 用 npmmirror 加速
RUN curl -fsSL https://npmmirror.com/mirrors/node/v20.18.0/node-v20.18.0-linux-x64.tar.xz \
    | tar -xJ -C /usr/local --strip-components=1

# ECharts MCP（chart MCP 需要全局 bin mcp-echarts）
RUN npm install -g --registry=https://registry.npmmirror.com mcp-echarts

WORKDIR /app

# 复制 Python 虚拟环境（从 builder 阶段）
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# 复制项目源码
COPY . .

# 默认环境变量（可被 docker-compose / .env.prod 覆盖）
# LANG/LC_ALL=C.UTF-8：容器 shell 的 UTF-8 locale，否则 ls/cat 对中文文件名
# 显示 octal 转义（$'\345...'）。PYTHONUTF8=1 只管 Python 进程，管不了 shell。
ENV WREN_BIN_PATH=wren \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

EXPOSE 2026

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -sf http://localhost:2026/ok || exit 1

CMD ["python", "start_server.py"]
