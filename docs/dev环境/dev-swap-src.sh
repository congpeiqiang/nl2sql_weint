#!/bin/bash
# dev 环境 src 换装助手 —— 在 25.34 上执行（由 dev-release-backend.ps1 自动送上来）。
#
# 为什么需要它：dev 的 /mydata/nl2sql/src 是容器的 bind 挂载，而容器以 root 运行，
# 于是它往 src 里写出了 root 属主的 __pycache__/*.pyc。weint 删不掉这些文件，
# 所以「在宿主机上 find src -mindepth 1 -delete」必然半途失败（Permission denied），
# 而且失败发生在清空**之后**——线上 src 会被删成半残。2026-09-21 实测踩到：
# 278 个 .py 全没了，只剩 123 个 root 属主的 __pycache__ 目录，靠备份才恢复。
#
# 解法：文件操作用**应用镜像起的一次性 root 容器**来做（两个目录都挂进去），
# 全程不需要 sudo、不需要额外口令。注意应用镜像里**只有 tar 没有 find**。
#
# 用法：
#   dev-swap-src.sh check                    只报告现状与容器内可用工具
#   dev-swap-src.sh backup  <stamp>          备份到 .tmp/src-prev-<stamp>.tgz
#   dev-swap-src.sh swap    <tar 路径>       校验 → 清空 → 解包（tar 顶层须含 src/）
#   dev-swap-src.sh restore <备份 tgz 路径>  从备份恢复（与 swap 同一条路径）
set -u

DEV=/mydata/nl2sql
SRC=$DEV/src
XFER=$DEV/.tmp
IMG=${APP_IMAGE:-nl2sql-app_langgraph-api:latest}
SENTINEL='src/agent/main_agent.py'

die() { echo "ERROR: $*" >&2; exit 1; }

# 用一次性 root 容器对 $SRC 做文件操作；$1 = 容器内要跑的 sh 脚本
in_root() {
    docker run --rm --entrypoint sh \
        -v "$SRC:/app/src" \
        -v "$XFER:/mnt/xfer" \
        "$IMG" -c "$1"
}

case "${1:-}" in
check)
    echo "src 顶层      : $(ls -1 "$SRC" | tr '\n' ' ')"
    echo "src 里 .py    : $(find "$SRC" -name '*.py' | wc -l)"
    echo "非 weint 属主 : $(find "$SRC" ! -user weint | wc -l)"
    echo "应用镜像      : $IMG"
    in_root 'command -v tar  >/dev/null && echo "容器内 tar : 有" || echo "容器内 tar : 无"'
    in_root 'command -v grep >/dev/null && echo "容器内 grep: 有" || echo "容器内 grep: 无"'
    in_root 'command -v find >/dev/null && echo "容器内 find: 有" || echo "容器内 find: 无"'
    ;;

backup)
    [ $# -ge 2 ] || die "需要 stamp 参数"
    BK="$XFER/src-prev-$2.tgz"
    ( cd "$DEV" && tar -czf "$BK" src ) || die "备份失败"
    echo "备份 -> $BK ($(du -h "$BK" | cut -f1))"
    ;;

swap | restore)
    [ $# -ge 2 ] || die "需要 tar 路径"
    TGZ=$2
    [ -f "$TGZ" ] || die "找不到 $TGZ"
    BASE=$(basename "$TGZ")

    # ① 宿主机先校验包（上传件是 weint 自己的，读得了）
    tar -tzf "$TGZ" | grep -qx "$SENTINEL" || die "包里没找到 $SENTINEL（tar 顶层应当含 src/）"
    echo "包校验通过：$BASE ($(du -h "$TGZ" | cut -f1))"

    # ② 容器内：先解到暂存区并校验，再清空真目录、拷进去。
    #    「先暂存后清空」是关键——解包失败时线上 src 原封不动，不会重演半残。
    INNER="set -e
rm -rf /mnt/xfer/.stage
mkdir -p /mnt/xfer/.stage
tar -xzf /mnt/xfer/$BASE -C /mnt/xfer/.stage --strip-components=1
test -f /mnt/xfer/.stage/agent/main_agent.py
cd /app/src
rm -rf -- ./* ./.??* 2>/dev/null || true
cp -a /mnt/xfer/.stage/. /app/src/
rm -rf /mnt/xfer/.stage
echo SWAP_OK"

    OUT=$(in_root "$INNER") || die "容器内换装失败（线上 src 未被改动）"
    echo "$OUT" | grep -q SWAP_OK || die "换装未确认，输出：$OUT"
    echo "换装完成：$(find "$SRC" -name '*.py' | wc -l) 个 .py"
    ;;

*)
    sed -n '2,20p' "$0"
    exit 1
    ;;
esac
