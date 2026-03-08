# UniGraph Documentation

This folder documents the current UniGraph backend as it exists in the codebase.

Files in this folder:

- `deployment-ec2.md`: production deployment runbook for Docker Compose on EC2 (security, scaling, health checks).
- `openapi.md`: HTTP API surface, routes, request and response contracts, and OpenAPI usage.
- `security.md`: authentication, authorization, guardrails, rate limiting, and data protection.
- `caching.md`: response cache behavior, key structure, and operational considerations.
- `short-term-memory.md`: short-term memory lifecycle, compaction, summarization, and async updates.
- `long-term-memory.md`: long-term retrieval architecture, pgvector experiments, and HNSW rationale.
- `redis.md`: Redis topology, namespaces, clients, key patterns, and queue usage.
- `ops.md`: operational status endpoint and the metrics it exposes.
- `strategy.md`: current architecture state and the recommended next build steps.

Recommended reading order:

1. `deployment-ec2.md`
2. `openapi.md`
3. `security.md`
4. `short-term-memory.md`
5. `long-term-memory.md`
6. `redis.md`
7. `ops.md`
8. `strategy.md`
