# -*- coding: utf-8 -*-
"""统一环境加载（eval / prompt 运维脚本共用，消除各脚本硬编码 `.env` 的漂移）。

背景（2026-08-29）：生产部署在服务器 Linux Docker 容器，docker-compose `env_file:
.env.prod` 注入真实环境变量；但 collect_badcase / feedback_gate / badcase_status /
run_experiment / sync_prompts 的 `_load_env()` 此前硬编码 `load_dotenv(项目根/.env)`
——容器内因 override=False 不覆盖已注入 env 而侥幸正确，但**宿主机 / 本机手动跑**
时会连到 dev 项目（LANGFUSE_PUBLIC_KEY 是 dev 凭据），生产反馈/差评进不了生产
Dataset:badcase。

规则（2026-08-30 显式化：DEPLOY_ENV 门控，优先级 高→低）：
  0. `DEPLOY_ENV=prod`（docker-compose environment 注入 / 手动 export）才启用
     `.env.prod` 叠加；未设默认 dev → 只读 `.env`，不碰 `.env.prod`。
  1. 已注入的真实 env（docker env_file / 手动 export）恒优先——不覆盖。
  2. `.env.prod` 的 `LANGFUSE_*`：`DEPLOY_ENV=prod` 下用它覆盖 `.env` 的 dev 值，
     让脚本连生产项目。**仅 LANGFUSE_***——`.env.prod` 的 `AGENT_DATA_ROOT=/app/data`
     是容器路径，宿主机/Windows 无效，绝不能让它覆盖 `.env` 的本机路径。
  3. `.env`：开发基线（本机 AGENT_DATA_ROOT=D:\\nl2sql_data、dev 凭据等）。

用法：
    from agent.settings.env_loader import load_env
    load_env()
"""
from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]  # src/agent/settings/env_loader.py → 项目根


def load_env() -> None:
    """加载项目环境变量：`.env`（dev 基线）为底，`DEPLOY_ENV=prod` 时叠加 `.env.prod` 的 LANGFUSE_*。

    - 容器内（DEPLOY_ENV=prod）：docker env_file 已注入 .env.prod 的 LANGFUSE_*（含在
      pre 里）→ 跳过 → 注入值保持；`.env` 镜像内不存在 → 无操作。
    - 宿主机/本机手动连生产（`export DEPLOY_ENV=prod`）：LANGFUSE_* 不在 pre 里 →
      `.env` 先设 dev 值，再被 `.env.prod` 覆盖为生产值 → 连生产项目；本机
      AGENT_DATA_ROOT 保持不动。
    - dev（DEPLOY_ENV 未设或 =dev）：只读 `.env`，不碰 `.env.prod`。
    - 显式 export LANGFUSE_*：在 pre 里 → 尊重用户指定，不被任何文件覆盖。
    """
    from dotenv import dotenv_values, load_dotenv

    # DEPLOY_ENV 必须来自「启动前已注入的进程环境」，不能从要选择的文件里读回
    deploy_env = os.environ.get("DEPLOY_ENV", "dev")
    # 记录脚本启动前已存在的真实 env（容器注入 / 手动 export）——之后不覆盖
    pre = {k for k in os.environ}
    load_dotenv(_PROJECT_ROOT / ".env", override=False)  # dev 基线，填缺不覆盖
    # Langfuse trace Environment 属性（UI Environment 列）对接到 DEPLOY_ENV：
    # 显式 export / .env 文件的 LANGFUSE_TRACING_ENVIRONMENT 恒优先（rule #1）；
    # 否则从 DEPLOY_ENV 推导，prod→production，其余→development。
    # 离线实验 worker 会显式覆盖为 experiment（run_experiment._run_worker）。
    if not os.environ.get("LANGFUSE_TRACING_ENVIRONMENT"):
        os.environ["LANGFUSE_TRACING_ENVIRONMENT"] = (
            "production" if deploy_env == "prod" else "development"
        )
    if deploy_env != "prod":
        return
    prod = _PROJECT_ROOT / ".env.prod"
    if prod.exists():
        prod_cfg = dotenv_values(prod) or {}
        for key, val in prod_cfg.items():
            if (
                key.startswith("LANGFUSE_")
                and val not in (None, "")
                and key not in pre
            ):
                os.environ[key] = str(val)
