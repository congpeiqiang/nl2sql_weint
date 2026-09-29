#!/usr/bin/env bash
# ── P2-2 nginx 补丁：X-Request-ID 透传/回显 + 访问日志（服务器侧，手工应用）──
#
# 为什么不能靠发版：发布 tar **排除 docker/**，而 compose 是把服务器上的
# `$AppDir/docker/nginx.conf` 以**文件级**只读方式 bind mount 进容器
# （`./docker/nginx.conf:/etc/nginx/nginx.conf:ro`）。
#
# 三个必须记住的点：
#   ① 文件级挂载 ⇒ 新内容必须**就地写进同一个 inode**。用 `mv` 换文件会让容器继续读旧
#      inode，表现是"改了没生效"。本脚本写入后会把 inode 比对一遍。
#   ② 线上与仓库副本已分叉（线上多一段 P0-3 注释）⇒ 上传的是**仓库副本的当前内容**，
#      所以仓库副本必须包含线上那段注释（`scripts/verify_request_logging.py` ⑥ 有断言防丢）。
#   ③ `nginx -t` 失败会自动回滚；**reload 之前坏文件不影响正在跑的 nginx**，不必重建容器，
#      也不必停服（`nginx -s reload` 不打断已建立的连接）。
#
# 用法（在本机 Git Bash 里跑；服务器需已配好 SSH key ⇒ 全程不弹口令）：
#   bash docs/weint环境/发布脚本/apply-nginx-request-id-patch.sh            # 应用 + 校验
#   bash docs/weint环境/发布脚本/apply-nginx-request-id-patch.sh --dry-run  # 只看将要写入的指令
#   bash docs/weint环境/发布脚本/apply-nginx-request-id-patch.sh --rollback # 回滚到最近一次备份
set -euo pipefail

SSH_TARGET="${NL2SQL_SSH:-weint@192.168.25.64}"
APP_DIR="${NL2SQL_APP_DIR:-/home/weint/apps/nl2sql/nl2sql-app}"
CONF_REL="docker/nginx.conf"
NGINX_CT="nl2sql-app_nginx_1"
HTTP_PORT="${NL2SQL_NGINX_PORT:-8080}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
LOCAL_CONF="$REPO_ROOT/$CONF_REL"
REMOTE_CONF="$APP_DIR/$CONF_REL"

mode="apply"
case "${1:-}" in
  --dry-run)  mode="dry" ;;
  --rollback) mode="rollback" ;;
  "")         ;;
  *) echo "未知参数：$1（可用 --dry-run / --rollback）"; exit 2 ;;
esac

# 每个 ssh 调用都走 BatchMode（没配 key 就快速失败，不弹口令）
ssh_do() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$SSH_TARGET" "$@"; }

echo "== 目标：$SSH_TARGET : $REMOTE_CONF（容器 $NGINX_CT）=="

# ── 0. 连通性 + 前置事实核对（只读）──────────────────────────
ssh_do bash -s <<EOF
set -u
d="$APP_DIR"
[ -f "$REMOTE_CONF" ] || { echo "XX 服务器上没有 $REMOTE_CONF"; exit 1; }
mnt=\$(docker inspect $NGINX_CT --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{end}}')
echo "挂载：\$mnt"
case "\$mnt" in
  *"/etc/nginx/nginx.conf"*) ;;
  *) echo "XX 容器没挂 /etc/nginx/nginx.conf，本脚本前提不成立"; exit 1 ;;
esac
docker exec $NGINX_CT nginx -v 2>&1 | head -1
[ -w "\$d/docker" ] || { echo "XX \$d/docker 不可写（权限/只读）—— 本步只判权限，不写任何文件"; exit 1; }
EOF

# ── 回滚分支 ────────────────────────────────────────────────
if [ "$mode" = "rollback" ]; then
  echo "== 回滚：取最近一次备份 =="
  ssh_do bash -s <<EOF
set -eu
d="$APP_DIR"
bak=\$(ls -1t "\$d/docker/nginx.conf.bak-"* 2>/dev/null | head -1 || true)
[ -n "\$bak" ] || { echo "XX 找不到任何 nginx.conf.bak-* 备份"; exit 1; }
echo "回滚自：\$bak"
cat "\$bak" > "$REMOTE_CONF"
docker exec $NGINX_CT nginx -t 2>&1
docker exec $NGINX_CT nginx -s reload 2>&1 && echo "OK 已回滚并 reload"
EOF
  exit $?
fi

# ── 1. 差异判断（本地 vs 线上）──────────────────────────────
[ -f "$LOCAL_CONF" ] || { echo "XX 本地缺 $LOCAL_CONF"; exit 1; }
local_md5="$(md5sum "$LOCAL_CONF" | awk '{print $1}')"
remote_md5="$(ssh_do "md5sum $REMOTE_CONF" | awk '{print $1}')"
echo "本地 md5=$local_md5"
echo "线上 md5=$remote_md5"
if [ "$local_md5" = "$remote_md5" ]; then
  echo "== 内容已一致：线上补丁已应用（无需操作）=="; exit 0
fi

if [ "$mode" = "dry" ]; then
  echo "== DRY-RUN：未改动服务器任何文件。将写入的内容 = 本地 $CONF_REL =="
  grep -nE 'map \$http_x_request_id|log_format|access_log|add_header X-Request-ID|proxy_set_header X-Request-ID' "$LOCAL_CONF"
  exit 0
fi

# ── 2. 备份（独立一步：只做备份 + 记录 inode）────────────────
echo "== 备份 =="
ssh_do bash -s <<EOF
set -eu
d="$APP_DIR"
bak="\$d/docker/nginx.conf.bak-\$(date +%Y%m%d-%H%M%S)"
cp -p "$REMOTE_CONF" "\$bak"
echo "backup=\$bak  (\$(wc -c < "\$bak") bytes)"
echo "inode_before=\$(stat -c %i "$REMOTE_CONF")"
EOF

# ── 3. 就地写入（单独一步：本步 stdin 全给 cat）─────────────
# 注意别把 `cat > 文件` 和 `bash -s <<heredoc` 混在一个会话里 —— 两者抢同一个 stdin，
# 会把脚本正文当内容写进配置文件（第一版脚本就是这么写错的）。
echo "== 就地写入（stdin = 本地 $CONF_REL）=="
ssh_do "cat > $REMOTE_CONF" < "$LOCAL_CONF"

# ── 4. 校验 inode + 内容一致，再 nginx -t → reload ──────────
echo "== 校验写入结果 =="
ssh_do bash -s <<EOF
set -eu
d="$APP_DIR"
echo "inode_after=\$(stat -c %i "$REMOTE_CONF")   （必须与上面 inode_before 相同）"
echo "线上 md5=\$(md5sum "$REMOTE_CONF" | awk '{print \$1}')   （必须等于本地 $local_md5）"
EOF

echo "== nginx -t =="
if ! ssh_do "docker exec $NGINX_CT nginx -t 2>&1"; then
  echo "XX 配置校验失败 → 自动回滚"
  ssh_do bash -s <<EOF
set -eu
d="$APP_DIR"
bak=\$(ls -1t "\$d/docker/nginx.conf.bak-"* | head -1)
cat "\$bak" > "$REMOTE_CONF"
echo "已回滚自 \$bak"
EOF
  exit 1
fi
ssh_do "docker exec $NGINX_CT nginx -s reload 2>&1 && echo 'OK reloaded'"

# ── 5. 端到端校验（补丁不等后端发版即应生效）───────────────
echo "== 校验：响应头 X-Request-ID =="
ssh_do bash -s <<EOF
set -u
echo "--- 不带入站 id（应由 nginx 生成 32 位 hex）---"
curl -s -o /dev/null -D - "http://127.0.0.1:$HTTP_PORT/ok" | grep -i '^x-request-id' || echo "XX 响应头没有 X-Request-ID"
echo "--- 带入站 id（应原样沿用）---"
curl -s -o /dev/null -D - -H 'X-Request-ID: rid-probe-12345' "http://127.0.0.1:$HTTP_PORT/ok" | grep -i '^x-request-id' || echo "XX 没有沿用入站 id"
echo "--- nginx 访问日志（最近 2 行，应含 rid= 与耗时）---"
docker logs --tail 2 $NGINX_CT 2>&1
EOF

echo
echo "✅ 完成。回滚：bash $0 --rollback"
