#!/bin/bash
echo "=== registry /v2/ reachability ==="
for m in \
  "https://registry-1.docker.io/v2/" \
  "https://docker.m.daocloud.io/v2/" \
  "https://docker.nju.edu.cn/v2/" \
  "https://docker.mirrors.sjtug.sjtu.edu.cn/v2/" \
  "https://dockerproxy.com/v2/" \
  "https://docker.mirrors.ustc.edu.cn/v2/" \
  "https://hub-mirror.c.163.com/v2/" \
  "https://mirror.baidubce.com/v2/" \
  "https://docker.1panel.live/v2/" \
  "https://docker.langfuse.com/v2/" \
  "https://ghcr.io/v2/" \
  "https://quay.io/v2/" ; do
  code=$(timeout 8 curl -s -o /dev/null -w '%{http_code}' "$m" 2>/dev/null)
  echo "$m -> $code"
done

echo "=== manifest inspect candidates ==="
for img in \
  "docker.m.daocloud.io/clickhouse/clickhouse-server:25.12" \
  "docker.m.daocloud.io/langfuse/langfuse:3" \
  "docker.m.daocloud.io/langfuse/langfuse-worker:3" \
  "docker.nju.edu.cn/langfuse/langfuse:3" \
  "docker.1panel.live/langfuse/langfuse:3" \
  "docker.langfuse.com/langfuse/langfuse:3" \
  "ghcr.io/langfuse/langfuse:3" \
  "quay.io/langfuse/langfuse:3" ; do
  if timeout 30 docker manifest inspect "$img" >/dev/null 2>&1; then
    echo "EXISTS $img"
  else
    echo "MISS  $img"
  fi
done
echo DIAG_DONE
