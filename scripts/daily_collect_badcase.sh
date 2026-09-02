#!/usr/bin/env bash
# 生产每日 BadCase 采集（Linux Docker 容器版）→ Langfuse Dataset:badcase
#
# 生产部署在服务器 Linux Docker 容器（docker-compose v1 env_file: .env.prod 注入
# 真实环境变量）。容器内没有 cron，由宿主机 cron 每日触发本脚本，用 docker exec
# 在容器内跑采集链路——env_file 注入的 LANGFUSE_*（生产项目凭据）+ AGENT_DATA_ROOT
# （/app/data）自动生效，无需在宿主机重复配置 .env.prod。
#
# 宿主机 crontab 配置（root 或可访问 docker 的用户，每日 02:1x 避开整点洪峰）：
#   crontab -e
#   13 2 * * * /home/weint/apps/nl2sql/nl2sql-app/backend/scripts/daily_collect_badcase.sh >> /home/weint/apps/nl2sql/logs/nl2sql_collect_badcase.log 2>&1
#   （日志目录需存在：mkdir -p /home/weint/apps/nl2sql/logs）
#
# 前提：
#   - 宿主机可执行 `docker exec`（docker CLI 权限）
#   - 容器名 `nl2sql-app_langgraph-api_1`（docker-compose v1 默认命名，见
#     docs/weint环境/NL2SQL-部署与更新手册.md；env_file 指向 .env.prod）
#   - 容器内环境由 docker-compose env_file(.env.prod) + environment(DEPLOY_ENV=prod)
#     注入；镜像内不含 .env/.env.prod（.dockerignore 均排除），loader 靠注入值判定。
#     .venv 在镜像内（Dockerfile COPY --from=builder）
set -u

CONTAINER="${NL2SQL_CONTAINER:-nl2sql-app_langgraph-api_1}"
# 容器 venv Python：Dockerfile 用 uv sync --no-install-project 装依赖 → 容器 venv 里
# 没有 dev 机的 _nl2sql_src.pth，/app/src 不在 sys.path → 必须显式 PYTHONPATH=/app/src
# 才能 import agent 模块（否则 ModuleNotFoundError: No module named 'agent'）。
PY="${PY:-/app/.venv/bin/python}"
LOG="$(date '+%Y-%m-%d %H:%M:%S')"

echo "[$LOG] begin docker exec ${CONTAINER}"

# ── 第 1 步：采集 BadCase → Langfuse Dataset:badcase ──────────────
# 扫描近 1 天所有用户会话 trace，按条件筛「差评/低分/异常」：
#   user-feedback=0（用户👎）/ 五维分<0.6 / trace=ERROR / sql_exec_success=0
# 命中 → create_dataset_item 写入 Dataset:badcase（带 source_trace_id 链回 trace）
# 本地 stamp 去重（同 trace 不重复采）。数据源是【Langfuse API】，不是日志。
docker exec "${CONTAINER}" bash -c "cd /app && PYTHONPATH=/app/src ${PY} -m agent.eval.collect_badcase --days 1"
cb=$?
echo "[$LOG] collect_badcase exit=$cb"

# ── 第 2 步：真实反馈门禁（M6 闭环）──────────────────────────────
# 聚合近 7 天用户好评/差评，按 prompt_label 分组比好评率：
#   candidate 好评率低于 reference−阈值 → exit 1（触发回滚/不放量）
#   数据不足 → exit 0（跳过）。只做判断，不写数据。
# M6 闭环：紧随采集跑，采集后紧跟门禁，保证用最新差评做放量决策。
docker exec "${CONTAINER}" bash -c "cd /app && PYTHONPATH=/app/src ${PY} -m agent.eval.feedback_gate --days 7"
fg=$?
echo "[$LOG] feedback_gate exit=$fg"

# ── 第 3 步：状态汇总（P0 闭环）──────────────────────────────────
# 输出本地 badcase_status.json 各状态计数（pending/reviewed/fixed/invalid），
# 提示人工复审待处理数量（人工在 Langfuse 复审后 mark fixed/invalid）。
docker exec "${CONTAINER}" bash -c "cd /app && PYTHONPATH=/app/src ${PY} -m agent.eval.badcase_status summary"
bs=$?
echo "[$LOG] badcase_status exit=$bs"

echo "[$LOG] done (cb=$cb fg=$fg bs=$bs)"
# collect_badcase 失败视为调度失败；门禁回归（1）只是数据提示，不令 cron 告警。
exit $(( cb == 0 ? 0 : 1 ))
