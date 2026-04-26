# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

UniGraph is an AI-powered backend system for university/research discovery with RAG (Retrieval Augmented Generation). The system uses AWS Bedrock for LLM generation and embeddings, PostgreSQL with pgvector for long-term memory, Redis for short-term memory and queues, and SQS/DynamoDB for async job processing.

## Architecture

### Service Layers
- **API Layer**: FastAPI app (`app/main.py`) with routers in `app/api/v1/` (chat, auth, evaluation, ops)
- **Service Layer**: Business logic in `app/services/` including:
  - `llm_service.py` - Main LLM orchestration and response generation
  - `web_retrieval_service.py` - Tavily web search and page fetching
  - `long_term_memory_service.py` - pgvector retrieval
  - `embedding_service.py` - Bedrock embeddings
  - `llm_async_queue_service.py` - SQS job management
- **Infrastructure**: AWS clients and limiters in `app/infra/`
- **Workers**: Background processors in `app/scripts/`:
  - `llm_async_worker.py` - Processes chat jobs from SQS
  - `summary_worker.py` - Compacts memory via Redis Streams
  - `metrics_aggregation_worker.py` - Aggregates metrics to DynamoDB
  - `eval_dynamodb_worker.py` - Offline evaluation judge

### Data Flow
1. User query → `POST /api/v1/chat/stream`
2. Job enqueued to SQS (`unigraph-llm-jobs`)
3. LLM worker dequeues and processes:
   - Input guardrails
   - Cache lookup
   - Short-term memory build (Redis)
   - Long-term retrieval (pgvector with Bedrock embeddings)
   - Web fallback via Tavily (when confidence is low)
   - LLM generation via Bedrock (Claude 3.5 Sonnet primary, Haiku fallback)
   - Output guardrails
   - Memory update and cache write
4. Results stored in DynamoDB, metrics queued
5. API polls DynamoDB and streams via SSE

### Key Technologies
- **LLM**: AWS Bedrock Claude 3.5 Sonnet (`us.anthropic.claude-3-5-sonnet-20240620-v1:0`)
- **Embeddings**: Bedrock Titan (`amazon.titan-embed-text-v2:0`, 1024 dims)
- **Vector DB**: PostgreSQL + pgvector with HNSW index
- **Cache/Memory**: Redis with encrypted payloads
- **Queue**: SQS for async jobs, Redis Streams for memory compaction
- **Storage**: DynamoDB for metrics and evaluation results
- **Web Search**: Tavily API with async page fetching

### Configuration
All service configs are in `app/config/*.yaml`:
- `app_config.yaml` - Application settings
- `bedrock_config.yaml` - AWS Bedrock models
- `tavily_config.yaml` - Web search settings
- `prompt.yaml` - LLM system prompts and citation policies
- `security_config.yaml` - JWT and auth
- `redis_config.yaml`, `postgres_config.yaml` - Data stores

Configuration merges YAML with env vars. AWS Secrets Manager integration available via `AWS_SECRETS_MANAGER_SECRET_ID`.

## Development Commands

### Setup
```bash
# Install dependencies
pip install -r requirements-dev.txt

# Frontend setup
cd frontend && npm install
```

### Running Locally
```bash
# Start all services (API + worker + frontend)
docker-compose up

# Start with optional workers
docker-compose --profile llm-async up  # Add LLM async worker
docker-compose --profile eval-queue up  # Add evaluation worker
docker-compose --profile metrics-queue up  # Add metrics worker

# API only (port 8000)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Frontend dev server (port 5173)
cd frontend && npm run dev
```

### Testing
```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_llm_service.py

# Run with coverage
pytest --cov=app --cov-report=xml

# Run single test
pytest tests/test_llm_service.py::test_function_name -v
```

### Linting and Formatting
```bash
# Format code
black app/ tests/

# Lint
flake8 app/ tests/
```

### Database and Infrastructure
```bash
# Check Postgres connection
python -m app.scripts.check_postgres

# Ingest documents and create embeddings
python -m app.scripts.chunk_documents
python -m app.scripts.embed_chunks
python -m app.scripts.ingest_embeddings

# Rebuild vector index
python -m app.scripts.rebuild_chunk_vector_index

# Create auth user
python -m app.scripts.upsert_auth_user

# Generate JWT token
python -m app.scripts.generate_jwt
```

### Workers
```bash
# Run individual workers
python -m app.scripts.llm_async_worker
python -m app.scripts.summary_worker
python -m app.scripts.metrics_aggregation_worker
python -m app.scripts.eval_dynamodb_worker
```

### Frontend
```bash
cd frontend

# Development
npm run dev

# Build for production
npm run build

# Preview production build
npm run preview

# Deploy to S3/CloudFront
../scripts/frontend-deploy.sh
```

## Important Patterns

### Service Dependencies
- Services use dependency injection via function parameters (not global state)
- Infrastructure clients (`bedrock_client`, `redis_client`, `postgres_client`) are module-level singletons in `app/infra/`
- Config is loaded once via `get_settings()` from `app/core/config.py`

### Async Workers
- Workers are long-running processes that poll queues
- SQS workers: `llm_async_worker.py`, `eval_dynamodb_worker.py`, `metrics_aggregation_worker.py`
- Redis Streams worker: `summary_worker.py`
- Workers use graceful shutdown with `SIGTERM` handling

### Memory Architecture
- **Short-term**: Redis with encrypted JSON, recent window + summary strategy, TTL-based expiry
- **Long-term**: pgvector semantic search, HNSW index, top_k=2 default
- Memory compaction runs async via Redis Streams queue

### Web Retrieval Strategy
- Triggered when vector similarity is below threshold OR when `always_web_retrieval_enabled=true`
- Generates multiple query variants, fetches top pages, extracts clean text chunks
- Ranks and deduplicates chunks, optionally reranks with Bedrock
- Citations are validated - answers must reference allowed URLs only
- System abstains with "Sorry, no relevant information is found." if evidence is weak

### German University Search Enhancements
- **Domain Prioritization**: Direct German university domains (uni-*.de, tu-*.de, fh-*.de, hs-*.de) get highest authority scores (0.92)
- **Official Education Portals**: DAAD.de, study-in-germany.de, hochschulkompass.de prioritized (0.90 authority)
- **German Query Variants**: Automatically adds German terminology:
  - Deadline queries → "bewerbungsfrist", "wintersemester/sommersemester"
  - Requirements → "zulassungsvoraussetzungen", "auswahlsatzung", "mindestnote"
  - Language → "sprachnachweis"
  - Curriculum → "modulhandbuch", "prüfungsordnung"
- **German Data Extraction**: Enhanced patterns for:
  - German date formats (DD.MM.YYYY)
  - German grading system (1.0-4.0 scale)
  - Semester intakes (Wintersemester/Sommersemester)
  - Module handbooks and examination regulations
- **Evidence Validation**: Pre-generation checks for specific data (dates, scores, thresholds)
- **Answer Validation**: Post-generation quality checks ensure answers contain specific values, not vague placeholders
- **Configuration Flags** (in `tavily_config.yaml`):
  - `german_university_mode_enabled: true` - Enable German enhancements
  - `german_university_authority_boost: 0.15` - Boost for .de/.eu domains
  - `german_specific_extraction_enabled: true` - German data patterns
  - `evidence_specificity_validation_enabled: true` - Validate evidence quality
  - `answer_validation_enabled: true` - Validate answer quality

### Testing Patterns
- Tests use `pytest` with `pytest-asyncio` for async tests
- Mock AWS services (Bedrock, SQS, DynamoDB) in tests
- Config overrides via monkeypatch or test fixtures
- Integration tests should mock external APIs (Tavily, Bedrock)

## Environment Variables

Key env vars for development:
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` - AWS credentials
- `WEB_SEARCH_API_KEY` - Tavily API key
- `TAVILY_WEB_SEARCH` - Enable/disable Tavily
- `SECURITY_JWT_SECRET` - JWT signing key
- `MEMORY_ENCRYPTION_KEY` - Redis memory encryption
- `POSTGRES_PASSWORD` - Database password
- `BEDROCK_PRIMARY_MODEL_ID`, `BEDROCK_FALLBACK_MODEL_ID` - LLM models
- `API_WORKERS` - Number of Uvicorn workers
- `REDIS_RUNTIME_ROLE` - Set to `app` or `worker` for different Redis clients

See `docker-compose.yml` for full list of configuration flags.

## Deployment

### Docker Compose
Production deployment uses `docker-compose.prod.yml` with ECR images on EC2. See `docs/deployment-ec2.md`.

### CI/CD
- GitHub Actions workflows in `.github/workflows/`
- `ci.yml` - Runs tests on PRs
- `cd.yml` - Builds images, pushes to ECR, deploys to EC2, uploads frontend to S3/CloudFront
- Uses OIDC for AWS authentication

### Infrastructure
- API: Uvicorn with multiple workers behind ALB
- Frontend: S3 + CloudFront
- Workers: Multiple containers (worker, llm-worker, eval-worker, metrics-worker)
- Data: RDS Postgres, ElastiCache Redis, SQS queues, DynamoDB tables

## Documentation

Comprehensive docs in `docs/`:
- `system-overview.md` - End-to-end architecture
- `fastapi-architecture.md` - Component diagram
- `website-search.md` - Tavily integration and citation grounding
- `short-term-memory.md`, `long-term-memory.md` - Memory systems
- `redis.md` - Redis topology and key patterns
- `security.md` - Auth, guardrails, rate limiting
- `evaluation-pipeline.md` - Offline evaluation architecture
- `deployment-ec2.md`, `cicd-github-actions.md` - Production setup

## Common Tasks

### Adding a new LLM feature
1. Update `app/services/llm_service.py`
2. Modify prompt in `app/config/prompt.yaml` if needed
3. Add config fields to `app/config/bedrock_config.yaml` and corresponding schema
4. Update tests in `tests/test_llm_service.py`

### Adding a new API endpoint
1. Create route in `app/api/v1/{router}.py`
2. Add service logic in `app/services/`
3. Define request/response schemas in `app/schemas/`
4. Add tests in `tests/test_{router}.py`

### Modifying web retrieval
1. Update `app/services/web_retrieval_service.py`
2. Adjust config in `app/config/tavily_config.yaml`
3. Update citation policy in `app/config/prompt.yaml` if needed
4. Test with `tests/test_web_retrieval_service.py`

### Changing vector retrieval
1. Modify `app/services/long_term_memory_service.py`
2. Update embedding config in `app/config/embedding_config.yaml`
3. Rebuild index if schema changes: `python -m app.scripts.rebuild_chunk_vector_index`

## Troubleshooting

### API not starting
- Check Redis connectivity: Redis client pings on startup
- Verify AWS credentials are configured
- Check `SECURITY_JWT_SECRET` is set
- Review logs for configuration validation errors

### Worker not processing jobs
- Verify SQS queue exists and worker has permissions
- Check `REDIS_RUNTIME_ROLE` is set correctly
- Ensure AWS credentials are available
- Check worker logs for queue polling errors

### Tests failing
- Install dev dependencies: `pip install -r requirements-dev.txt`
- Ensure pytest.ini pythonpath is correct
- Mock external services (Bedrock, Tavily, SQS, DynamoDB)
- Check async tests use `pytest-asyncio` markers

### Frontend not connecting to API
- Verify `VITE_DEV_API_TARGET` in `frontend/.env.development`
- Check API is running on correct port (default 8000)
- Review CORS settings in `app/config/middleware_config.yaml`
- Check browser network tab for API errors
