#!/usr/bin/env bash
# 校验 Langfuse 的 ClickHouse 数据是否按 .env 的 CLICKHOUSE_DB（默认 nl2sql）落库
# 用法：cd /home/weint/apps/nl2sql/langfuse && bash verify_ch_db.sh
set -u
cd "$(dirname "$0")" || exit 1
set -a; . ./.env; set +a
CHDB="${CLICKHOUSE_DB:-nl2sql}"
CH() { docker exec langfuse_clickhouse_1 clickhouse-client \
        --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" -q "$1"; }

echo "=== 1. 容器状态 ==="
docker-compose ps 2>/dev/null | tail -n +2

echo
echo "=== 2. 容器内 CLICKHOUSE_DB（三处应一致） ==="
for c in langfuse_clickhouse_1 langfuse_langfuse-web_1 langfuse_langfuse-worker_1; do
  printf '  %-28s %s\n' "$c" "$(docker exec "$c" printenv CLICKHOUSE_DB 2>&1)"
done

echo
echo "=== 3. 数据库列表 ==="
CH "SHOW DATABASES"

echo
echo "=== 4. ${CHDB} 库对象（期望 13 个） ==="
CH "SELECT name, engine FROM system.tables WHERE database='${CHDB}' ORDER BY name"

echo
echo "=== 5. ${CHDB}.schema_migrations 行数（期望 92） ==="
CH "SELECT count() FROM ${CHDB}.schema_migrations"

echo
echo "=== 6. ${CHDB} 各表行数 ==="
CH "SELECT table, sum(rows) AS rows FROM system.parts WHERE active AND database='${CHDB}' GROUP BY table ORDER BY rows DESC"

echo
echo "=== 7. default 库残留（切换前建的旧表，可 DROP） ==="
CH "SELECT name FROM system.tables WHERE database='default' ORDER BY name"

echo
echo "=== 8. langfuse-web 启动日志尾部（含迁移结果） ==="
docker logs --tail 25 langfuse_langfuse-web_1 2>&1 | tail -25
