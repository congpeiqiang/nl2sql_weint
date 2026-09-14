#!/bin/bash
# v4 SDK e2e: nested chain + generation via context managers
cd /home/weint/apps/nl2sql/langfuse
cat > sdk_test.py <<'PY'
import time
from langfuse import Langfuse

langfuse = Langfuse(
    public_key="pk-c64d8d357fa8e8b90e8aefb8183a2cea",
    secret_key="sk-3791dd2504c88ca7502c987768ac35e907366db9260eec07",
    host="http://127.0.0.1:3010",
)
with langfuse.start_as_current_observation(name="v4-sdk-e2e", as_type="chain", input="ping", output="pong"):
    with langfuse.start_as_current_observation(name="gen-sql", as_type="generation", model="gpt-4o",
                                               input={"q": "count?"}, output="42"):
        pass
langfuse.flush()
time.sleep(3)
print("TRACE_SENT")
PY
./venv/bin/python sdk_test.py 2>&1 | tail -3
echo "== wait 20s for worker =="
sleep 20
echo "== ClickHouse traces/observations =="
docker exec langfuse_clickhouse_1 clickhouse-client -q "SELECT name, count() FROM traces WHERE name LIKE 'v4-sdk%' GROUP BY name" 2>&1
docker exec langfuse_clickhouse_1 clickhouse-client -q "SELECT type, name, count() FROM observations WHERE name IN ('v4-sdk-e2e','gen-sql') GROUP BY type, name" 2>&1
echo "== events =="
docker exec langfuse_clickhouse_1 clickhouse-client -q "SELECT (SELECT count() FROM events_core) AS core, (SELECT count() FROM events_full) AS full" 2>&1
docker exec langfuse_clickhouse_1 clickhouse-client -q "SELECT type, count() FROM events_core GROUP BY type ORDER BY count() DESC" 2>&1
echo SDK_E2E_DONE
