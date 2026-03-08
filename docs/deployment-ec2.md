# EC2 Deployment Guide (Docker Compose)

This is the production runbook for deploying UniGraph on a single EC2 host with Docker Compose.

It covers:

- secure production image usage
- required environment variables
- API/worker scaling strategy
- Redis TLS and distributed middleware controls
- health checks and post-deploy verification
- safe local Redis profile behavior

## Deployment Topology

Runtime services:

- `api`: FastAPI + Uvicorn
- `worker`: summary queue worker

Data services (recommended AWS managed):

- Redis: ElastiCache Valkey/Redis (TLS enabled)
- Postgres: RDS PostgreSQL

## 1) Host Preparation (EC2)

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
```

Clone the repository, then run all commands from project root.

## 2) Image and Dependency Model

The container image is production-hardened to install only runtime dependencies.

- `requirements-prod.txt`: runtime packages only
- `requirements-dev.txt`: test/lint/dev tooling
- `requirements.txt`: points to dev requirements for local development workflows

Docker builds use `requirements-prod.txt` only.

## 3) Required Environment Variables

Create `.env` in project root:

```bash
# OpenAI
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://<your-azure-openai-endpoint>/
AZURE_OPENAI_API_VERSION=2024-12-01-preview
AZURE_OPENAI_PRIMARY_DEPLOYMENT=gpt-5.2-chat
AZURE_OPENAI_FALLBACK_DEPLOYMENT=gpt-4o-mini

# App runtime
APP_DOCS_ENABLED=false
API_WORKERS=2
APP_METRICS_JSON_ENABLED=true
APP_METRICS_JSON_DIR=data/metrics

# Postgres
POSTGRES_ENABLED=true
POSTGRES_HOST=<rds-endpoint>
POSTGRES_PORT=5432
POSTGRES_DATABASE=unigraph
POSTGRES_USERNAME=unigraph
POSTGRES_PASSWORD=<postgres-password>
POSTGRES_SSL_MODE=require

# Redis app role
REDIS_APP_HOST=<elasticache-endpoint>
REDIS_APP_PORT=6379
REDIS_APP_DB=0
REDIS_APP_USERNAME=
REDIS_APP_PASSWORD=
REDIS_APP_TLS=true
REDIS_APP_SSL_CERT_REQS=required
REDIS_APP_SSL_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

# Redis worker role
REDIS_WORKER_HOST=<elasticache-endpoint>
REDIS_WORKER_PORT=6379
REDIS_WORKER_DB=0
REDIS_WORKER_USERNAME=
REDIS_WORKER_PASSWORD=
REDIS_WORKER_TLS=true
REDIS_WORKER_SSL_CERT_REQS=required
REDIS_WORKER_SSL_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

# Security (required)
SECURITY_AUTH_ENABLED=true
SECURITY_JWT_SECRET=<long-random-secret-at-least-32-chars>
SECURITY_JWT_ISSUER=ai-system
SECURITY_JWT_EXP_MINUTES=60
MEMORY_ENCRYPTION_KEY=<separate-long-random-secret-at-least-32-chars>

# Distributed middleware (recommended)
MIDDLEWARE_ENABLE_DISTRIBUTED_RATE_LIMIT=true
MIDDLEWARE_DISTRIBUTED_RATE_LIMIT_PREFIX=ratelimit
MIDDLEWARE_ENABLE_DISTRIBUTED_BACKPRESSURE=true
MIDDLEWARE_DISTRIBUTED_BACKPRESSURE_KEY=backpressure:inflight
MIDDLEWARE_DISTRIBUTED_BACKPRESSURE_LEASE_SECONDS=45
MIDDLEWARE_TRUSTED_PROXY_CIDRS=172.31.0.0/16

# Summary queue recovery
MEMORY_SUMMARY_QUEUE_CLAIM_IDLE_MS=60000
MEMORY_SUMMARY_QUEUE_CLAIM_BATCH_SIZE=50
```

Metrics JSON notes:

- `APP_METRICS_JSON_ENABLED=true` writes per-request chat metrics and rolling aggregates to disk.
- `APP_METRICS_JSON_DIR` is resolved relative to project root unless absolute.
- Current files:
  - `<dir>/chat_metrics_requests.jsonl`
  - `<dir>/chat_metrics_aggregate.json`
- `docker-compose.yml` mounts `./data/metrics:/app/data/metrics` so metrics are visible on host and survive container restarts/recreates.

Proxy trust note:

- set `MIDDLEWARE_TRUSTED_PROXY_CIDRS` only to known proxy/load-balancer CIDRs
- if unset, `X-Forwarded-For` is ignored to prevent spoofing

Lock down file permissions:

```bash
chmod 600 .env
```

## 4) Pre-Deploy Validation

Validate Compose and merged runtime config:

```bash
docker compose config
```

## 5) Deploy

Build and start core services:

```bash
docker compose up -d --build api worker
```

Tail logs:

```bash
docker compose logs -f api worker
```

## 6) Scaling Guidance

Single EC2 host API scaling (vertical process scaling):

- increase `API_WORKERS` (for example `2`, `4`, `6` depending on vCPU and memory)

Worker scaling (horizontal on same host):

```bash
docker compose up -d --build --scale worker=2
```

API horizontal scaling:

- use multiple EC2 instances behind an ALB target group
- keep per-instance `API_WORKERS` tuned to host size

## 7) Health and Smoke Tests

Health endpoint (unauthenticated):

```bash
curl -fsS http://127.0.0.1:8000/healthz
```

Expected response:

```json
{"status":"ok"}
```

Redis connectivity from API container:

```bash
docker compose exec api python -c "from app.infra.redis_client import app_redis_client; print(app_redis_client.ping())"
```

Expected output: `True`

## 8) Production Security Checklist

- `APP_DOCS_ENABLED=false` in production
- `SECURITY_JWT_SECRET` set to strong random value
- `MEMORY_ENCRYPTION_KEY` set and different from JWT secret
- `.env` not committed and permissions set to `600`
- security groups restrict:
  - inbound `8000` to ALB or trusted CIDR only
  - Redis/Postgres access only from app nodes

## 9) Optional Local Redis Profile (Non-Production)

Start local Redis + app stack:

```bash
docker compose --profile local-redis up -d redis api worker
```

Local profile safety:

- Redis port binds only to localhost: `127.0.0.1:6379:6379`
- do not use `local-redis` profile on production EC2

Suggested local overrides:

```bash
REDIS_APP_HOST=redis
REDIS_WORKER_HOST=redis
REDIS_APP_TLS=false
REDIS_WORKER_TLS=false
APP_DOCS_ENABLED=true
```

## 10) Rollback

If deployment is unhealthy:

```bash
docker compose logs --tail 200 api worker
docker compose down
```

Revert to previous version and redeploy:

```bash
docker compose up -d --build api worker
```
