#!/usr/bin/env bash
# v4 SDK 端到端上报 + 校验数据确实落在 .env 的 CLICKHOUSE_DB（默认 nl2sql）库
# 用法：cd /home/weint/apps/nl2sql/langfuse && bash sdk_e2e_nl2sql.sh
set -u
cd "$(dirname "$0")" || exit 1
set -a; . ./.env; set +a
CHDB="${CLICKHOUSE_DB:-nl2sql}"
CH() { docker exec langfuse_clickhouse_1 clickhouse-client \
        --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" -q "$1"; }

TAG="v4-sdk-$(date +%m%d-%H%M%S)"
export PK="${LANGFUSE_INIT_PROJECT_PUBLIC_KEY:?missing LANGFUSE_INIT_PROJECT_PUBLIC_KEY in .env}"
export SK="${LANGFUSE_INIT_PROJECT_SECRET_KEY:?missing LANGFUSE_INIT_PROJECT_SECRET_KEY in .env}"
export TAG

echo "== 上报 trace：$TAG =="
./venv/bin/python - <<'PY'
import os, time
from langfuse import Langfuse

lf = Langfuse(
    public_key=os.environ["PK"],
    secret_key=os.environ["SK"],
    host="http://127.0.0.1:3010",
)
tag = os.environ["TAG"]
with lf.start_as_current_observation(name=tag, as_type="chain", input="ping", output="pong"):
    with lf.start_as_current_observation(
        name=tag + "-gen", as_type="generation", model="gpt-4o",
        input={"q": "how many traces?"}, output="42",
    ):
        pass
lf.flush()
time.sleep(3)
print("TRACE_SENT", tag)
PY

echo
echo "== 等待 worker 落库（20s） =="
sleep 20

echo "== ${CHDB}.traces 中的本次上报 =="
CH "SELECT name, count() AS c FROM ${CHDB}.traces WHERE name LIKE '${TAG}%' GROUP BY name"

echo "== ${CHDB}.observations 中的本次上报 =="
CH "SELECT type, name, count() AS c FROM ${CHDB}.observations WHERE name LIKE '${TAG}%' GROUP BY type, name ORDER BY type"

echo "== ${CHDB} 事件表 =="
CH "SELECT (SELECT count() FROM ${CHDB}.events_core) AS core, (SELECT count() FROM ${CHDB}.events_full) AS full"
CH "SELECT type, count() AS c FROM ${CHDB}.events_core GROUP BY type ORDER BY c DESC"

echo "== ${CHDB} 全表行数 =="
CH "SELECT table, sum(rows) AS rows FROM system.parts WHERE active AND database='${CHDB}' GROUP BY table ORDER BY rows DESC"

echo "== default 库行数（应保持 0，证明没有再往 default 写） =="
CH "SELECT table, sum(rows) AS rows FROM system.parts WHERE active AND database='default' GROUP BY table ORDER BY rows DESC"

echo SDK_E2E_NL2SQL_DONE
