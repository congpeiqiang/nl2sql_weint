#!/bin/bash
# Pull langfuse v4 stack from daocloud mirror + tag to standard names
cd /home/weint/apps/nl2sql/langfuse
docker pull docker.m.daocloud.io/clickhouse/clickhouse-server:25.12
docker tag docker.m.daocloud.io/clickhouse/clickhouse-server:25.12 clickhouse/clickhouse-server:25.12
docker pull docker.m.daocloud.io/langfuse/langfuse:4
docker tag docker.m.daocloud.io/langfuse/langfuse:4 langfuse/langfuse:4
docker pull docker.m.daocloud.io/langfuse/langfuse-worker:4
docker tag docker.m.daocloud.io/langfuse/langfuse-worker:4 langfuse/langfuse-worker:4
echo "=== resulting images ==="
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -E 'clickhouse|langfuse'
echo PULL2_DONE
