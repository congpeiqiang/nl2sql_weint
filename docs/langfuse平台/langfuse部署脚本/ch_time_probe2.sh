#!/usr/bin/env bash
# 排查 2：读侧转换（toTimeZone / session_timezone）与写侧解析的语义差异
set -u
cd "$(dirname "$0")" || exit 1
set -a; . ./.env; set +a
CH() { docker exec langfuse_clickhouse_1 clickhouse-client \
        --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" "$@"; }

echo "=== 1. 读侧：列时区 / toTimeZone / session_timezone ==="
CH -q "SELECT timestamp, toTimeZone(timestamp,'Asia/Shanghai') AS bj_tz, toUnixTimestamp64Milli(timestamp) AS epoch_ms FROM nl2sql.traces ORDER BY timestamp DESC LIMIT 3"
echo "-- 带 session_timezone=Asia/Shanghai 读取（不改数据） --"
CH -q --session_timezone "Asia/Shanghai" "SELECT timestamp, toString(timestamp) AS as_str, toUnixTimestamp64Milli(timestamp) AS epoch_ms FROM nl2sql.traces ORDER BY timestamp DESC LIMIT 3"

echo
echo "=== 2. 写侧：同一朴素字符串在不同 session 时区下的落库 epoch ==="
for tz in UTC Asia/Shanghai; do
  CH -q "DROP TABLE IF EXISTS default.tz_probe3"
  CH -q "CREATE TABLE default.tz_probe3 (ts DateTime64(3)) ENGINE = Memory"
  CH -q --session_timezone "$tz" "INSERT INTO default.tz_probe3 VALUES ('2026-09-14 03:30:14.503')"
  printf 'session_timezone=%-14s -> ' "$tz"
  CH -q "SELECT ts, toUnixTimestamp64Milli(ts) AS epoch_ms FROM default.tz_probe3"
  CH -q "DROP TABLE default.tz_probe3"
done

echo
echo "=== 3. 服务器/列时区现状 ==="
CH -q "SELECT timezone() AS server_tz"
CH -q "SELECT table, column, type FROM system.columns WHERE database='nl2sql' AND table IN ('traces','observations') AND type LIKE 'DateTime%' ORDER BY table, column"
