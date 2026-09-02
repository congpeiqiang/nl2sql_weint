#!/bin/bash
# Langfuse v4 fresh-deploy verification on 192.168.25.64
set -e
cd /home/weint/apps/nl2sql/langfuse

echo "== 1. containers =="
docker-compose ps --format 'table {{.Name}}\t{{.Status}}'

echo "== 2. health =="
curl -s --max-time 8 http://127.0.0.1:3010/api/public/health; echo

echo "== 3. UI =="
curl -s -o /dev/null -w 'ui:%{http_code}\n' --max-time 8 http://127.0.0.1:3010/

echo "== 4. e2e trace =="
PK="pk-c64d8d357fa8e8b90e8aefb8183a2cea"
SK="sk-3791dd2504c88ca7502c987768ac35e907366db9260eec07"
AUTH=$(printf '%s:%s' "$PK" "$SK" | base64 -w0)
TS=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)
curl -s --max-time 10 -X POST http://127.0.0.1:3010/api/public/traces \
  -H "Authorization: Basic $AUTH" -H "Content-Type: application/json" \
  -d "{\"name\":\"deploy-test\",\"timestamp\":\"$TS\",\"input\":\"hello\",\"output\":\"world\"}"; echo
sleep 12
curl -s --max-time 10 "http://127.0.0.1:3010/api/public/traces?name=deploy-test" \
  -H "Authorization: Basic $AUTH" | head -c 300; echo

echo "== 5. clickhouse events =="
docker exec $(docker ps --format '{{.Names}}' | grep clickhouse | head -1) \
  clickhouse-client -q "SELECT 'events_core' t, count() FROM events_core UNION ALL SELECT 'events_full', count() FROM events_full" 2>/dev/null || echo "(clickhouse query skipped)"

echo "== 6. worker errors =="
docker logs $(docker ps --format '{{.Names}}' | grep langfuse-worker | head -1) 2>&1 | grep -iE 'error|fatal' | tail -3 || echo "no errors"
echo VERIFY_DONE
