#!/usr/bin/env bash
# dev 环境（192.168.25.34）：把仓库 `docker/nginx.conf` 整份同步到 34 上的 nginx。
#
# 只是一层「把三个机器相关的变量填好」的壳 —— 真正的逻辑在
# `docs/prod环境/apply-nginx-request-id-patch.sh`（md5 判差异 → 备份 → 就地写**同一 inode**
# → `nginx -t`（失败自动回滚）→ `nginx -s reload`）。两个环境共用同一个 nginx.conf 文件
# 是本轮「dev 与 prod 一致」的一部分：dev 不再有自己的 nginx 专版。
#
# 为什么不能靠发版带过去：发布包**排除 docker/**，而 compose 是把服务器上的
# `$AppDir/docker/nginx.conf` 以**文件级**只读方式 bind mount 进容器。
#
# 与 prod 的差异只有两个（下面两个环境变量）：
#   · 机器/目录：weint@192.168.25.34 : /mydata/nl2sql/nl2sql-app
#   · 容器名：compose **v2** 用连字符 `nl2sql-app-nginx-1`（prod 是下划线 `nl2sql-app_nginx_1`）
#
# 用法（在本机 Git Bash 里跑；34 需已配好 SSH key ⇒ 全程不弹口令）：
#   bash docs/dev环境/dev-apply-nginx-conf.sh            # 应用 + 校验
#   bash docs/dev环境/dev-apply-nginx-conf.sh --dry-run  # 只看将要写入的指令
#   bash docs/dev环境/dev-apply-nginx-conf.sh --rollback # 回滚到最近一次备份
set -euo pipefail

_d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 向上找到含 docker/nginx.conf 的那一层（别写死退几层，搬目录就会坏）
REPO_ROOT="$_d"
while [ "$REPO_ROOT" != "/" ] && [ ! -f "$REPO_ROOT/docker/nginx.conf" ]; do REPO_ROOT="$(dirname "$REPO_ROOT")"; done
[ -f "$REPO_ROOT/docker/nginx.conf" ] || { echo "XX 向上找不到 docker/nginx.conf —— 仓库根判定失败（脚本在 $_d）"; exit 1; }

NL2SQL_SSH="${NL2SQL_SSH:-weint@192.168.25.34}" \
NL2SQL_APP_DIR="${NL2SQL_APP_DIR:-/mydata/nl2sql/nl2sql-app}" \
NL2SQL_NGINX_CT="${NL2SQL_NGINX_CT:-nl2sql-app-nginx-1}" \
NL2SQL_NGINX_PORT="${NL2SQL_NGINX_PORT:-8080}" \
exec bash "$REPO_ROOT/docs/prod环境/apply-nginx-request-id-patch.sh" "$@"
