#!/bin/bash
for p in 3002 5433 9090 9091 18123 8123; do
  if ss -tln 2>/dev/null | grep -q ":$p "; then
    echo "$p BUSY"
  else
    echo "$p free"
  fi
done
echo "=== who listens on 3000/3001/9000/9001 ==="
ss -tlnp 2>/dev/null | grep -E ':(3000|3001|9000|9001)\s' | head -6
