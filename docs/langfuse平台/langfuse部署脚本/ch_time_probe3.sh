#!/usr/bin/env bash
# 排查 3：session_timezone 的读/写语义（修正 -q 参数顺序后的版本）
set -u
cd "$(dirname "$0")" || exit 1
set -a; . ./.env; set +a
CH() { docker exec langfuse_clickhouse_1 clickhouse-client \
        --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" "$@"; }

echo "=== 1. 默认（服务器 UTC）读取 ==="
CH -q "SELECT timestamp, toUnixTimestamp64Milli(timestamp) AS epoch_ms FROM nl2sql.traces ORDER BY timestamp DESC LIMIT 2"

echo "=== 2. session_timezone=Asia/Shanghai 读取（纯读，不改数据） ==="
CH --multiquery -q "SET session_timezone='Asia/Shanghai'; SELECT timestamp, toUnixTimestamp64Milli(timestamp) AS epoch_ms FROM nl2sql.traces ORDER BY timestamp DESC LIMIT 2"

echo "=== 3. 写侧语义：同一朴素字符串 '2026-09-14 03:30:14.503' ==="
for tz in UTC Asia/Shanghai; do
  CH -q "DROP TABLE IF EXISTS default.tz_probe3"
  CH -q "CREATE TABLE default.tz_probe3 (ts DateTime64(3)) ENGINE = Memory"
  CH --multiquery -q "SET session_timezone='${tz}'; INSERT INTO default.tz_probe3 VALUES ('2026-09-14 03:30:14.503')"
  printf '  写入 session_timezone=%-14s 落库 epoch_ms=' "$tz"
  CH -q "SELECT toUnixTimestamp64Milli(ts) FROM default.tz_probe3"
  printf '%*s读回(默认UTC会话)=' 30 ' '
  CH -q "SELECT ts FROM default.tz_probe3"
  CH -q "DROP TABLE default.tz_probe3"
done

echo "=== 4. 结论对照：真实瞬时 = 03:30:14.503Z（= 北京 11:30:14.503） ==="
CH -q "SELECT toUnixTimestamp64Milli(toDateTime64('2026-09-14 03:30:14.503','Asia/Shanghai')) AS naive_as_bj_ms, toUnixTimestamp64Milli(toDateTime64('2026-09-14 03:30:14.503','UTC')) AS naive_as_utc_ms"
