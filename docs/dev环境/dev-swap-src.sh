#!/bin/bash
# dev 环境 src 换装助手 —— 在 25.34 上执行（由 dev-release-backend.ps1 /
# dev-release-backend-bluegreen.ps1 自动送上来）。
#
# 为什么需要它：dev 的 /mydata/nl2sql/src 是容器的 bind 挂载，而容器以 root 运行，
# 于是它往 src 里写出了 root 属主的 __pycache__/*.pyc。weint 删不掉这些文件，
# 所以「在宿主机上 find src -mindepth 1 -delete」必然半途失败（Permission denied），
# 而且失败发生在清空**之后**——线上 src 会被删成半残。2026-09-21 实测踩到：
# 278 个 .py 全没了，只剩 123 个 root 属主的 __pycache__ 目录，靠备份才恢复。
#
# 解法：文件操作用**应用镜像起的一次性 root 容器**来做（目录挂进去），
# 全程不需要 sudo、不需要额外口令。注意应用镜像里**只有 tar 没有 find**。
#
# 用法：
#   dev-swap-src.sh check                    只报告现状与容器内可用工具
#   dev-swap-src.sh backup  <stamp>          备份到 .tmp/src-prev-<stamp>.tgz
#   dev-swap-src.sh swap    <tar 路径>       校验 → 清空 → 解包（tar 顶层须含 src/）
#   dev-swap-src.sh restore <备份 tgz 路径>  从备份恢复（与 swap 同一条路径）
#   dev-swap-src.sh stage   <tar> [目标目录] 校验 → 暂存到**另一个目录**（默认 $DEV/src-next）
#                                            —— 蓝绿第二色的代码就位；**绝不动 src**
#   dev-swap-src.sh rotate  <提升目录> <当前 src 的新名字>
#                                            目录轮换：src → <新名字>、<提升目录> → src
#                                            —— 蓝绿发版末段把新代码变成常驻色
set -u

DEV=/mydata/nl2sql
SRC=$DEV/src
XFER=$DEV/.tmp
IMG=${APP_IMAGE:-nl2sql-app_langgraph-api:latest}
SENTINEL='src/agent/main_agent.py'
NEXT=${SRC_NEXT:-$DEV/src-next}

die() { echo "ERROR: $*" >&2; exit 1; }

# 用一次性 root 容器对**宿主机某个目录**做文件操作；$1 = 宿主目录, $2 = 容器内要跑的 sh
# （$2 里的 /app/src 就是 $1；/mnt/xfer 是共享的 .tmp）
in_root_at() {
    docker run --rm --entrypoint sh \
        -v "$1:/app/src" \
        -v "$XFER:/mnt/xfer" \
        "$IMG" -c "$2"
}

# 对 $SRC 做文件操作（最常用）
in_root() { in_root_at "$SRC" "$1"; }

# 校验包 + 清空目标目录 + 解包，全程在 root 容器内；$1 = tar 路径, $2 = 宿主目标目录
# 「先解到暂存区再清空真目录」是关键：解包失败时目标目录原封不动，不会重演半残。
stage_into() {
    TGZ=$1; TGT=$2
    [ -f "$TGZ" ] || die "找不到 $TGZ"
    BASE=$(basename "$TGZ")
    # ① 宿主机先校验包（上传件是 weint 自己的，读得了）
    tar -tzf "$TGZ" | grep -qx "$SENTINEL" || die "包里没找到 $SENTINEL（tar 顶层应当含 src/）"
    # ② 目标目录必须**先存在**（weint 可建），才挂得进容器
    mkdir -p "$TGT" || die "建不了 $TGT"
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
    OUT=$(in_root_at "$TGT" "$INNER") || die "容器内换装失败（$TGT 未被改动）"
    echo "$OUT" | grep -q SWAP_OK || die "换装未确认，输出：$OUT"
    echo "换装完成：$TGT 下 $(find "$TGT" -name '*.py' | wc -l) 个 .py"
}

case "${1:-}" in
check)
    echo "src 顶层      : $(ls -1 "$SRC" | tr '\n' ' ')"
    echo "src 里 .py    : $(find "$SRC" -name '*.py' | wc -l)"
    echo "非 weint 属主 : $(find "$SRC" ! -user weint | wc -l)"
    echo "应用镜像      : $IMG"
    for d in "$SRC" "$NEXT"; do
        [ -d "$d" ] && echo "存在的目录    : $d" || true
    done
    ls -1d "$DEV"/src-prev-* 2>/dev/null | tail -3 | sed 's/^/最近备份      : /' || true
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
    [ -f "$2" ] || die "找不到 $2"
    echo "包校验通过：$(basename "$2") ($(du -h "$2" | cut -f1))"
    stage_into "$2" "$SRC"
    ;;

stage)
    # 蓝绿：把待发布代码暂存到**另一个目录**（绝不碰正在接流的 src）
    [ $# -ge 2 ] || die "需要 tar 路径"
    TGT=${3:-$NEXT}
    [ "$TGT" != "$SRC" ] || die "stage 的目标不许是 $SRC（那是 swap/restore 的活）"
    echo "包校验通过：$(basename "$2") ($(du -h "$2" | cut -f1))"
    stage_into "$2" "$TGT"
    echo "STAGED=$TGT"
    ;;

rotate)
    # 目录轮换：<提升目录> 变成 $SRC，原 $SRC 改名成 <当前 src 的新名字>
    [ $# -ge 3 ] || die "需要 <提升目录名> <当前 src 的新名字>"
    NEW=$2; PREVNAME=$3
    [ -d "$DEV/$NEW" ] || die "找不到 $DEV/$NEW（先 stage）"
    [ -d "$SRC" ] || die "找不到 $SRC"
    [ -e "$DEV/$PREVNAME" ] && die "$DEV/$PREVNAME 已存在，换一个名字（否则会覆盖历史版本）"
    # ⚠️ 只 mv **目录本身**（改名）⇒ 只需要父目录 $DEV 的写权限；src 里的
    #    root 属主文件一个都不碰（weint 删不掉它们，但改目录名不需要删）。
    mv "$SRC" "$DEV/$PREVNAME" || die "备份 $SRC 失败（src 原状保留，未做任何改动）"
    mv "$DEV/$NEW" "$SRC" || die "提升 $DEV/$NEW 失败 —— **src 现在是 $DEV/$PREVNAME**，手工 mv 回来即可：mv $DEV/$PREVNAME $SRC"
    echo "SRC_PREV=$PREVNAME"
    echo "现在 src = $NEW 的代码（$(find "$SRC" -name '*.py' | wc -l) 个 .py）"
    ;;

*)
    sed -n '2,26p' "$0"
    exit 1
    ;;
esac
