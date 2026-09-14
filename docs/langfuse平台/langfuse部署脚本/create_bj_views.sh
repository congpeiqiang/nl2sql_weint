#!/usr/bin/env bash
# 生成「北京时间视图层」：把 nl2sql（Langfuse 数据）里所有 DateTime64 列
# 以 Asia/Shanghai 渲染，供 BI / NL2SQL 应用直接查询，不改动原表、不改数据。
#
# 原理：ClickHouse 的 DateTime64 存的是「瞬时」(epoch)；不带显式时区时，
# 显示时区 = 会话时区（本部署服务器为 UTC，所以原表读出来是 UTC）。
# 视图里用 toTimeZone(col,'Asia/Shanghai') 把列的时区固定为北京时间 →
# 同一行数据的 epoch 不变，只是读出来的字符串变成北京时间。
#
# 用法：cd /home/weint/apps/nl2sql/langfuse && bash create_bj_views.sh
# 撤销：DROP DATABASE langfuse_bj
set -u
cd "$(dirname "$0")" || exit 1
set -a; . ./.env; set +a

SRC_DB="${CLICKHOUSE_DB:-nl2sql}"
BJ_DB="${CLICKHOUSE_BJ_DB:-langfuse_bj}"
TZ_BJ="Asia/Shanghai"

CH() { docker exec langfuse_clickhouse_1 clickhouse-client \
        --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" "$@"; }

echo "== 1. 创建目标库 ${BJ_DB} =="
if ! CH -q "CREATE DATABASE IF NOT EXISTS ${BJ_DB}" 2>&1; then
  echo "无法创建库 ${BJ_DB}（权限不足），中止。"
  exit 1
fi

echo
echo "== 2. 为 ${SRC_DB} 中每个含 DateTime 列的对象建视图 =="
# 表（含 MergeTree 系）与视图都要覆盖；物化视图 events_core_mv 跳过（视图层不复制 MV）
for t in $(CH -q "SELECT name FROM system.tables WHERE database='${SRC_DB}' AND engine != 'MaterializedView' ORDER BY name"); do
  cols=$(CH -q "SELECT column FROM system.columns WHERE database='${SRC_DB}' AND table='${t}' AND type LIKE 'DateTime%' ORDER BY column")
  repl=""
  for c in $cols; do
    repl="${repl}toTimeZone(\`${c}\`,'${TZ_BJ}') AS \`${c}\`, "
  done
  repl="${repl%, }"

  if [ -n "$repl" ]; then
    sql="CREATE OR REPLACE VIEW ${BJ_DB}.\`${t}\` AS SELECT * REPLACE (${repl}) FROM ${SRC_DB}.\`${t}\`"
  else
    sql="CREATE OR REPLACE VIEW ${BJ_DB}.\`${t}\` AS SELECT * FROM ${SRC_DB}.\`${t}\`"
  fi
  if CH -q "$sql" 2>&1; then
    printf '  OK   %-28s 时间列: %s\n' "$t" "${cols//$'\n'/, }"
  else
    printf '  FAIL %-28s\n' "$t"
  fi
done

echo
echo "== 3. 对照验证：同一行数据，两个库读出来的时间字符串 =="
echo "-- ${SRC_DB}.traces（服务器会话 UTC） --"
CH -q "SELECT timestamp, toUnixTimestamp64Milli(timestamp) AS epoch_ms FROM ${SRC_DB}.traces ORDER BY timestamp DESC LIMIT 3"
echo "-- ${BJ_DB}.traces（视图，北京时间） --"
CH -q "SELECT timestamp, toUnixTimestamp64Milli(timestamp) AS epoch_ms FROM ${BJ_DB}.traces ORDER BY timestamp DESC LIMIT 3"

echo
echo "== 4. 视图清单 =="
CH -q "SELECT database, name, engine FROM system.tables WHERE database IN ('${SRC_DB}','${BJ_DB}') ORDER BY database, name"
