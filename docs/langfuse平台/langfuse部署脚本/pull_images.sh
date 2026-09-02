#!/bin/bash
# Pull langfuse stack images with mirror fallback
cd /home/weint/apps/nl2sql/langfuse || exit 1
mkdir -p /home/weint/apps/nl2sql/langfuse

pull_or() {
  local primary="$1"; shift
  if docker pull "$primary" >/dev/null 2>&1; then
    echo "OK $primary"; return 0
  fi
  for m in "$@"; do
    if docker pull "$m" >/dev/null 2>&1; then
      echo "OK $m"; return 0
    fi
  done
  echo "FAIL $primary"; return 1
}

pull_or clickhouse/clickhouse-server:25.12 docker.m.daocloud.io/clickhouse/clickhouse-server:25.12
pull_or langfuse/langfuse:3 docker.m.daocloud.io/langfuse/langfuse:3
pull_or langfuse/langfuse-worker:3 docker.m.daocloud.io/langfuse/langfuse-worker:3

echo "=== resulting images ==="
docker images --format '{{.Repository}}:{{.Tag}}' | grep -E 'clickhouse|langfuse' || true
echo PULL_DONE
