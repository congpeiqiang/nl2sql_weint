#!/usr/bin/env bash
# 北京时间视图层对照检查：同一批数据在 nl2sql（UTC）与 langfuse_bj（北京时间）下的读法
set -u
cd "$(dirname "$0")" || exit 1
set -a; . ./.env; set +a
CHDB="${CLICKHOUSE_DB:-nl2sql}"
BJDB="${CLICKHOUSE_BJ_DB:-langfuse_bj}"
CH() { docker exec langfuse_clickhouse_1 clickhouse-client \
        --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" -q "$1"; }

echo "=== 原始库 ${CHDB}.traces（UTC） ==="
CH "SELECT name, timestamp, toUnixTimestamp64Milli(timestamp) AS epoch_ms FROM ${CHDB}.traces ORDER BY timestamp DESC LIMIT 3"

echo
echo "=== 视图库 ${BJDB}.traces（北京时间） ==="
CH "SELECT name, timestamp, toUnixTimestamp64Milli(timestamp) AS epoch_ms FROM ${BJDB}.traces ORDER BY timestamp DESC LIMIT 3"

echo
echo "=== 参考：宿主机时间 / ClickHouse 容器内时间 ==="
date '+host      : %F %T %Z'
CH "SELECT concat('clickhouse: ', toString(now()), '  (timezone=', timezone(), ')')"

echo
echo "=== events_core 抽样（视图层同样已按北京时间渲染） ==="
CH "SELECT type, name, start_time FROM ${BJDB}.events_core ORDER BY start_time DESC LIMIT 3"
