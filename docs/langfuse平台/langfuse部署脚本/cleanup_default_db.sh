#!/usr/bin/env bash
# 删除 default 库中「切换目标库之前」遗留的 Langfuse 旧表。
# 背景：2026-09-14 之前 CLICKHOUSE_DB 未传给 langfuse-web/worker，表建在 default；
#       现全部落在 .env 的 CLICKHOUSE_DB（nl2sql），default 里的旧表已不再被读写。
# 用法：cd /home/weint/apps/nl2sql/langfuse && bash cleanup_default_db.sh [--yes]
set -u
cd "$(dirname "$0")" || exit 1
set -a; . ./.env; set +a
CHDB="${CLICKHOUSE_DB:-nl2sql}"
CH() { docker exec langfuse_clickhouse_1 clickhouse-client \
        --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" -q "$1"; }

echo "== 删除前：default 库对象 =="
CH "SELECT name, engine, (SELECT sum(rows) FROM system.parts p WHERE p.database='default' AND p.table=system.tables.name AND p.active) AS rows FROM system.tables WHERE database='default' ORDER BY engine, name"

if [ "${1:-}" != "--yes" ]; then
  printf '确认删除以上 default 库对象？(yes/no) '
  read -r a
  [ "$a" = "yes" ] || { echo "已取消"; exit 0; }
fi

# 先视图/物化视图，后普通表，避免依赖报错
for t in $(CH "SELECT name FROM system.tables WHERE database='default' AND engine LIKE '%View%'"); do
  CH "DROP TABLE IF EXISTS default.\`$t\`" && echo "  dropped view  default.$t"
done
for t in $(CH "SELECT name FROM system.tables WHERE database='default'"); do
  CH "DROP TABLE IF EXISTS default.\`$t\`" && echo "  dropped table default.$t"
done

echo
echo "== 删除后 =="
echo -n "default 库对象数："; CH "SELECT count() FROM system.tables WHERE database='default'"
echo -n "${CHDB} 库对象数："; CH "SELECT count() FROM system.tables WHERE database='${CHDB}'"
echo -n "${CHDB}.schema_migrations："; CH "SELECT count() FROM ${CHDB}.schema_migrations"
