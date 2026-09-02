---
name: langfuse-deploy
description: "Deploy self-hosted Langfuse (v3 or v4) on a Linux server with Docker Compose: full stack web + worker + dedicated PostgreSQL + ClickHouse + Redis + MinIO containers, headless admin/project initialization, and end-to-end verification. Covers image sourcing under restricted networks (existing local images first, then direct pull with mirror fallback, then offline docker save/load relay through a bridge host). Use when installing or upgrading a self-hosted Langfuse observability stack on bare-metal/VM servers."
---

# Langfuse Self-Hosted Deployment (v3 / v4)

Deploy the complete Langfuse stack (web UI + worker + PostgreSQL + ClickHouse + Redis + MinIO) on a Linux server via Docker Compose. This skill encodes a **production-proven** procedure (validated on Ubuntu 22.04/24.04, Langfuse v3.225.x and v4.x).

> [!IMPORTANT]
> **Scope & access.** This skill assumes normal production access (root or passwordless sudo, no artificial write restrictions). If the target environment DOES impose file-write restrictions (e.g. "only write under `/home/user/apps/xxx`"), honor them: keep compose files, `.env`, and any scripts inside the allowed directory and ask before touching anything else.
>
> **Do not touch existing resources.** Never `docker rm`/`docker rmi`/`docker volume rm`/prune other projects' containers, images, volumes, or compose stacks. Deploy Langfuse as a **fully separate project** with its own dedicated middleware containers (fresh PostgreSQL/Redis/MinIO/ClickHouse) — do not point Langfuse at existing middleware instances (they usually belong to other applications and have different credentials/schemas).

## 1. Environment Probe (run first)

```bash
# OS / resources / disk
grep PRETTY /etc/os-release; uname -m; free -h | head -2; nproc; df -h / | tail -1
# docker + compose tooling (note: docker-compose v1.29 requires `version: "3.8"` in the file;
# `docker compose` v2 plugin does not)
docker --version; docker-compose --version 2>/dev/null; docker compose version 2>/dev/null
# existing images (reuse them first!) and running containers
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}'
docker ps --format '{{.Names}} | {{.Image}} | {{.Ports}}'
# port availability — pick FREE ports for web / minio api / minio console / postgres / clickhouse
ss -tln | grep -E ':(3000|3001|3010|5432|5433|6379|8123|9000|9001|9090|9091)\s' || echo "all free"
```

**Ports used by this stack (defaults; adapt when busy):** web `3010` (or 3000/3001), minio api `9090` + console `127.0.0.1:9091`, postgres `127.0.0.1:5433`, clickhouse `127.0.0.1:18123` (diag only). Existing services on 3000/3001/5432/6379/9000/9001 are common — always verify.

## 2. Version Selection

| Version | Notes |
|---|---|
| **v4** (current) | Same compose skeleton as v3 + **one extra env var** `LANGFUSE_MIGRATION_V4_WRITE_MODE` (proven value: `dual`). Image tags `langfuse/langfuse:4` + `langfuse/langfuse-worker:4`. Migrations run automatically on startup. |
| **v3** (legacy, still fine) | Tags `:3`. No v4 migration vars needed. |

Both use the same middleware: ClickHouse 25.12, Redis 7, MinIO, PostgreSQL 14+.

## 3. Image Sourcing (restricted networks included)

Priority order:

1. **Existing local images** — `docker images` and reuse (e.g. `postgres:14`, `redis:7`, `minio` already present).
2. **Direct pull** — `docker pull docker.io/<image>`; on failure try mirror hosts used by the environment (daocloud `docker.m.daocloud.io/...`, aliyuncs, 1panel, nju). Verify reachability first:
   ```bash
   for m in https://registry-1.docker.io/v2/ https://docker.m.daocloud.io/v2/ https://docker.nju.edu.cn/v2/; do
     echo "$m -> $(timeout 8 curl -s -o /dev/null -w '%{http_code}' $m)"
   done
   ```
3. **Offline relay (no registry reachable):** if the target server cannot reach ANY registry but a **bridge host** can reach both the target and a source machine that already has the images:
   ```bash
   # on SOURCE (has images + reachable from bridge): docker save to tar
   docker save -o /tmp/lf_img.tar <repo>:<tag>
   # bridge: scp source:/tmp/lf_img.tar -> bridge -> scp bridge:/tmp/lf_img.tar target:/tmp/
   # on TARGET: docker load -i /tmp/lf_img.tar   # adds images, never touches existing ones
   ```
   Relay per-image with checkpoints (skip already-loaded images) for resumability. `docker save` tars are typically much smaller than `docker images` sizes (layers are already compressed).

Needed images for this stack: `langfuse/langfuse:4` (or `:3`), `langfuse/langfuse-worker:4`, `clickhouse/clickhouse-server:25.12`, `redis:7`, `minio/minio:RELEASE.*`, `postgres:14`.

## 4. Deployment Directory

```
/opt/langfuse/          # production default (any dir is fine)
├── docker-compose.yml
├── .env                # chmod 600
└── (backups of previous compose/.env when upgrading)
```

## 5. docker-compose.yml (v4, proven)

> Use `version: "3.8"` ONLY when the host has legacy `docker-compose` v1.29 (it requires it). With the v2 plugin, omit it.

```yaml
version: "3.8"   # only for docker-compose v1

services:
  langfuse-worker:
    image: langfuse/langfuse-worker:4
    restart: always
    depends_on:
      postgres:   { condition: service_healthy }
      clickhouse: { condition: service_healthy }
      minio:      { condition: service_healthy }
      redis:      { condition: service_healthy }
    environment: &env-common
      NEXTAUTH_URL: ${NEXTAUTH_URL}
      NEXTAUTH_SECRET: ${NEXTAUTH_SECRET}
      SALT: ${SALT}
      ENCRYPTION_KEY: ${ENCRYPTION_KEY}
      DATABASE_URL: ${DATABASE_URL}
      CLICKHOUSE_MIGRATION_URL: ${CLICKHOUSE_MIGRATION_URL}
      CLICKHOUSE_URL: ${CLICKHOUSE_URL}
      CLICKHOUSE_USER: ${CLICKHOUSE_USER}
      CLICKHOUSE_PASSWORD: ${CLICKHOUSE_PASSWORD}
      CLICKHOUSE_CLUSTER_ENABLED: "false"
      REDIS_HOST: redis
      REDIS_PORT: "6379"
      REDIS_AUTH: ${REDIS_AUTH}
      LANGFUSE_S3_EVENT_UPLOAD_BUCKET: langfuse
      LANGFUSE_S3_EVENT_UPLOAD_REGION: auto
      LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID: ${MINIO_ROOT_USER}
      LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY: ${MINIO_ROOT_PASSWORD}
      LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT: http://minio:9000
      LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE: "true"
      LANGFUSE_S3_MEDIA_UPLOAD_BUCKET: langfuse
      LANGFUSE_S3_MEDIA_UPLOAD_REGION: auto
      LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID: ${MINIO_ROOT_USER}
      LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY: ${MINIO_ROOT_PASSWORD}
      LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT: http://minio:9000
      LANGFUSE_S3_MEDIA_UPLOAD_FORCE_PATH_STYLE: "true"
      LANGFUSE_S3_BATCH_EXPORT_ENABLED: "false"
      TELEMETRY_ENABLED: "false"
      NODE_OPTIONS: --max-old-space-size=512
      LANGFUSE_MIGRATION_V4_WRITE_MODE: ${LANGFUSE_MIGRATION_V4_WRITE_MODE}

  langfuse-web:
    image: langfuse/langfuse:4
    restart: always
    depends_on:
      postgres:   { condition: service_healthy }
      clickhouse: { condition: service_healthy }
      minio:      { condition: service_healthy }
      redis:      { condition: service_healthy }
    ports:
      - 3010:3000            # pick a FREE host port
    environment:
      <<: *env-common
      NODE_OPTIONS: --max-old-space-size=2048
      LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT: ${LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT}
      # headless initialization: auto-create admin user, org and project
      LANGFUSE_INIT_ORG_ID: ${LANGFUSE_INIT_ORG_ID}
      LANGFUSE_INIT_ORG_NAME: ${LANGFUSE_INIT_ORG_NAME}
      LANGFUSE_INIT_PROJECT_ID: ${LANGFUSE_INIT_PROJECT_ID}
      LANGFUSE_INIT_PROJECT_NAME: ${LANGFUSE_INIT_PROJECT_NAME}
      LANGFUSE_INIT_PROJECT_PUBLIC_KEY: ${LANGFUSE_INIT_PROJECT_PUBLIC_KEY}
      LANGFUSE_INIT_PROJECT_SECRET_KEY: ${LANGFUSE_INIT_PROJECT_SECRET_KEY}
      LANGFUSE_INIT_USER_EMAIL: ${LANGFUSE_INIT_USER_EMAIL}
      LANGFUSE_INIT_USER_NAME: ${LANGFUSE_INIT_USER_NAME}
      LANGFUSE_INIT_USER_PASSWORD: ${LANGFUSE_INIT_USER_PASSWORD}

  postgres:
    image: postgres:14          # dedicated, fresh instance
    restart: always
    environment:
      POSTGRES_USER: langfuse
      POSTGRES_PASSWORD: ${PG_PW}
      POSTGRES_DB: langfuse
    volumes:
      - langfuse_postgres_data:/var/lib/postgresql/data
    ports:
      - 127.0.0.1:5433:5432    # local-admin only; not required by langfuse
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U langfuse -d langfuse"]
      interval: 5s
      timeout: 5s
      retries: 15
      start_period: 5s

  clickhouse:
    image: clickhouse/clickhouse-server:25.12
    restart: always
    user: "101:101"
    environment:
      CLICKHOUSE_DB: default
      CLICKHOUSE_USER: ${CLICKHOUSE_USER}
      CLICKHOUSE_PASSWORD: ${CLICKHOUSE_PASSWORD}
    volumes:
      - langfuse_clickhouse_data:/var/lib/clickhouse
      - langfuse_clickhouse_logs:/var/log/clickhouse-server
    ports:
      - 127.0.0.1:18123:8123   # diag only
    healthcheck:
      test: wget --no-verbose --tries=1 --spider http://localhost:8123/ping || exit 1
      interval: 5s
      timeout: 5s
      retries: 15
      start_period: 5s

  minio:
    image: minio/minio:RELEASE.2024-05-28T17-19-04Z   # any recent RELEASE.* works
    restart: always
    entrypoint: sh
    command: -c 'mkdir -p /data/langfuse && minio server --address ":9000" --console-address ":9001" /data'
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
    ports:
      - 9090:9000               # S3 API — must be reachable from browsers for media
      - 127.0.0.1:9091:9001     # console
    volumes:
      - langfuse_minio_data:/data
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 5s
      timeout: 5s
      retries: 10
      start_period: 5s

  redis:
    image: redis:7
    restart: always
    command: >
      --requirepass ${REDIS_AUTH}
      --maxmemory-policy noeviction
    volumes:
      - langfuse_redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_AUTH}", "ping"]
      interval: 5s
      timeout: 10s
      retries: 10

volumes:
  langfuse_postgres_data:
  langfuse_clickhouse_data:
  langfuse_clickhouse_logs:
  langfuse_minio_data:
  langfuse_redis_data:
```

> If host port 6379 is already taken, **do not publish redis** (internal network only) — Langfuse reaches it as `redis:6379`.

## 6. .env (generate fresh secrets)

```bash
openssl rand -hex 32   # NEXTAUTH_SECRET, SALT, ENCRYPTION_KEY (64 hex chars each)
openssl rand -hex 16   # REDIS_AUTH, CLICKHOUSE_PASSWORD
# strong passwords for PG / MinIO / admin user (mixed case + digits + symbol)
```

```dotenv
NEXTAUTH_URL=http://<SERVER_IP_OR_DOMAIN>:3010
NEXTAUTH_SECRET=<hex64>
SALT=<hex64>
ENCRYPTION_KEY=<hex64>
DATABASE_URL=postgresql://langfuse:<PG_PW>@postgres:5432/langfuse
CLICKHOUSE_URL=http://clickhouse:8123
CLICKHOUSE_MIGRATION_URL=clickhouse://clickhouse:9000
CLICKHOUSE_USER=clickhouse
CLICKHOUSE_PASSWORD=<hex32>
REDIS_AUTH=<hex32>
MINIO_ROOT_USER=minio
MINIO_ROOT_PASSWORD=<strong>
LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT=http://<SERVER_IP_OR_DOMAIN>:9090
# headless init
LANGFUSE_INIT_ORG_ID=org_<8alnum>
LANGFUSE_INIT_ORG_NAME=AgentLab
LANGFUSE_INIT_PROJECT_ID=proj_<8alnum>
LANGFUSE_INIT_PROJECT_NAME=default
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-<hex>
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-<hex>
LANGFUSE_INIT_USER_EMAIL=admin@langfuse.local
LANGFUSE_INIT_USER_NAME=Admin
LANGFUSE_INIT_USER_PASSWORD=<strong, min 8 with mixed case/digit/symbol>
# v4 migration mode (dual = keep v3 read API + v4 event dual-write; fresh installs can also use "events")
LANGFUSE_MIGRATION_V4_WRITE_MODE=dual
LANGFUSE_MIGRATION_V4_ALLOW_PREVIEW_OPT_IN=false
LANGFUSE_BACKGROUND_MIGRATION_V4_ENABLE_HISTORIC_BACKFILL=false
# postgres service password (compose interpolation)
PG_PW=<same as in DATABASE_URL>
```

`chmod 600 .env`.

## 7. Deploy & Verify

```bash
cd <deploy_dir>
docker-compose config          # v1: validates; also add version: "3.8" if you see schema errors
docker-compose up -d
# wait until web is Ready (v4 runs migrations on first boot; give it up to ~5-8 min on small VMs)
for i in $(seq 1 60); do
  curl -s -o /dev/null http://127.0.0.1:3010/api/public/health && break; sleep 10
done
curl -s http://127.0.0.1:3010/api/public/health          # expect {"status":"OK","version":"4.x.x"}
docker-compose ps                                          # all healthy/up
docker-compose logs --tail=50 langfuse-web-1               # check for errors/OOM
```

**End-to-end trace check** (proves ingestion works, using the headless-created pk/sk):

```bash
PK=<pk-...>; SK=<sk-...>
AUTH=$(printf '%s:%s' "$PK" "$SK" | base64 -w0)
curl -s -X POST http://127.0.0.1:3010/api/public/traces \
  -H "Authorization: Basic $AUTH" -H "Content-Type: application/json" \
  -d '{"name":"deploy-test","timestamp":"'"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"'","input":"hi","output":"ok"}'
sleep 10
curl -s "http://127.0.0.1:3010/api/public/traces?name=deploy-test" -H "Authorization: Basic $AUTH"
```

Also verify the UI: `curl -s -o /dev/null -w '%{http_code}' http://<SERVER_IP>:3010/` → 200. If media won't display in the browser, the `LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT` must be a URL reachable from the browser (not `http://minio:9000`).

> [!NOTE] v4 事件表（events_core/events_full）只由 **v4 SDK** 上报填充
> The curl call above hits the v3-compatible endpoint and only lands in ClickHouse `traces`/`observations` — the v4 typed-event tables stay at 0. To verify the full v4 pipeline, send a real v4 SDK trace (the v4 SDK API differs from v3: no `client.trace()`, use `start_as_current_observation` with `as_type`, nesting via `with` blocks):

```bash
pip install langfuse        # SDK >= 4.x
```

```python
from langfuse import Langfuse
langfuse = Langfuse(public_key="pk-...", secret_key="sk-...", host="http://<SERVER_IP>:3010")
with langfuse.start_as_current_observation(name="chain-name", as_type="chain", input="q", output="a"):
    with langfuse.start_as_current_observation(name="gen", as_type="generation", model="gpt-4o",
                                               input={"q": "count?"}, output="42"):
        pass
langfuse.flush()
```

After ~20 s, `SELECT type, count() FROM events_core GROUP BY type` must show `CHAIN`/`GENERATION` events — that confirms ingestion → ClickHouse events → traces/observations views all work. (Posting a bare `trace-create` via `/api/public/ingestion` gets a 201 but routes to the legacy path and does not populate events.)

## 8. Ops & Troubleshooting

```bash
docker-compose ps / logs -f langfuse-web-1 / logs -f langfuse-worker-1
docker-compose restart          # restart all
docker-compose down && docker-compose up -d   # recreate; DATA LIVES IN VOLUMES
# OOM on small VMs: raise NODE_OPTIONS heap; v3 init needs >512MB heap
# "Peer authentication failed": you're not the postgres OS user — use the container's langfuse role + password
# ClickHouse tables events_core/events_full: check ingestion landed
docker exec -it <clickhouse-container> clickhouse-client -q "SELECT count() FROM events_full"
```

**Upgrade v3 → v4:** backup PG dump + compose + .env, swap image tags `:3`→`:4`, add `LANGFUSE_MIGRATION_V4_WRITE_MODE=dual` to .env and the compose env block, `docker-compose up -d` (migrations run automatically), then verify health + a sample trace round-trip.

**Security notes (production):** bind admin-only ports (postgres/clickhouse/redis console) to 127.0.0.1; keep `.env` 600; restrict web + minio API ports at the firewall/security-group level; rotate `NEXTAUTH_SECRET`/`SALT`/`ENCRYPTION_KEY` only when re-initializing (they encrypt stored data).
