#!/usr/bin/env bash
# 排查 ClickHouse 时间语义：服务器时区、列时区、写入解析规则、实际数据
# 用法：cd /home/weint/apps/nl2sql/langfuse && bash ch_time_probe.sh
set -u
cd "$(dirname "$0")" || exit 1
set -a; . ./.env; set +a
CH() { docker exec langfuse_clickhouse_1 clickhouse-client \
        --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" -q "$1"; }

echo "=== 1. 服务器时区 / 当前时间 ==="
CH "SELECT timezone() AS server_tz, now() AS now_raw, toString(now()) AS now_str, toString(toTimeZone(now(),'Asia/Shanghai')) AS now_bj, version() AS ch_version"

echo
echo "=== 2. nl2sql 中的 DateTime 列（type 里带列时区） ==="
CH "SELECT table, column, type FROM system.columns WHERE database='nl2sql' AND type LIKE 'DateTime%' ORDER BY table, column"

echo
echo "=== 3. traces 实际数据样本 ==="
CH "SELECT timestamp, created_at, event_ts, toTimeZone(timestamp,'Asia/Shanghai') AS ts_as_bj FROM nl2sql.traces ORDER BY timestamp DESC LIMIT 5"

echo
echo "=== 4. 写入解析实验：Z 后缀 vs 朴素字符串（列时区 = Asia/Shanghai） ==="
CH "DROP TABLE IF EXISTS default.tz_probe"
CH "CREATE TABLE default.tz_probe (tag String, ts DateTime64(3, 'Asia/Shanghai')) ENGINE = Memory"
CH "INSERT INTO default.tz_probe VALUES ('z_suffix','2026-09-14T03:30:14.503Z'),('naive','2026-09-14 03:30:14.503'),('offset','2026-09-14T11:30:14.503+08:00')"
CH "SELECT tag, ts, toUnixTimestamp64Milli(ts) AS epoch_ms, toTimeZone(ts,'UTC') AS as_utc FROM default.tz_probe ORDER BY tag"
CH "DROP TABLE default.tz_probe"

echo
echo "=== 5. 同一实验：列不带显式时区（= 跟随服务器时区） ==="
CH "DROP TABLE IF EXISTS default.tz_probe2"
CH "CREATE TABLE default.tz_probe2 (tag String, ts DateTime64(3)) ENGINE = Memory"
CH "INSERT INTO default.tz_probe2 VALUES ('z_suffix','2026-09-14T03:30:14.503Z'),('naive','2026-09-14 03:30:14.503')"
CH "SELECT tag, ts, toUnixTimestamp64Milli(ts) AS epoch_ms FROM default.tz_probe2 ORDER BY tag"
CH "SELECT 'column_type' AS k, type FROM system.columns WHERE database='default' AND table='tz_probe2' AND column='ts'"
CH "DROP TABLE default.tz_probe2"
