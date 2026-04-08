# Website Search (SerpAPI + Evidence-Grounded Answers)

## 1) Purpose
Enable live website fallback when vector retrieval is weak, while keeping answers grounded to evidence URLs.

## 2) Runtime Flow
`User question`
-> `Short-term context build`
-> `Retrieval fan-out (optional): speculative vector prefetch while context is loading`
-> `Vector retrieval (pgvector, reused from prefetch when query matches)`
-> `Web retrieval gate`
-> `SerpAPI fallback (only when needed, or always when hybrid mode is enabled)`
-> `Web retrieval fan-out (optional): web prefetch while vector retrieval is in-flight`
-> `Top pages fetch (async)`
-> `HTML cleanup + boilerplate stripping`
-> `Chunking + ranking + near-duplicate removal`
-> `Reranking (optional Bedrock reranker)`
-> `Fan-in merge of vector + web evidence`
-> `Citation-grounded prompt + allowed URLs`
-> `Model answer`
-> `Citation validation`
-> `Abstain if weak evidence or missing citations`

Abstain message:
`Sorry, no relevant information is found.`

## 3) Search + Retrieval Components
- SerpAPI client:
  - `app/services/serpapi_search_service.py`
  - Async single and batch query support.
- Web retrieval pipeline:
  - `app/services/web_retrieval_service.py`
  - Multi-query search variants.
  - Domain allowlist filtering (for example `.de`, `.eu`).
  - Async top-page fetch.
  - Published date extraction from SerpAPI rows and HTML metadata.
  - Clean-text chunking and relevance scoring.
  - Near-duplicate chunk filtering.

## 4) Citation-Grounded Answering
- Prompt policy:
  - `app/config/prompt.yaml`
  - Requires evidence-only answers and URL citations.
- Runtime enforcement:
  - `app/services/llm_service.py`
  - Injects citation policy + allowed URLs into system messages.
  - Blocks/abstains when:
    - no evidence,
    - no evidence URLs,
    - answer does not cite allowed URLs.

## 5) Configuration
Main config:
- `app/config/serpapi_config.yaml`

Important knobs:
- `enabled`
- `always_web_retrieval_enabled`
- `retrieval_fanout_enabled`
- `fallback_enabled`
- `fallback_similarity_threshold`
- `multi_query_enabled`
- `max_query_variants`
- `allowed_domain_suffixes`
- `query_planner_enabled`
- `query_planner_use_llm`
- `retrieval_loop_enabled`
- `retrieval_loop_use_llm`
- `retrieval_loop_max_steps`
- `retrieval_min_unique_domains`
- `retrieval_gap_min_token_coverage`
- `max_pages_to_fetch`
- `page_fetch_timeout_seconds`
- `strip_boilerplate`
- `page_chunk_chars`
- `page_chunk_overlap_chars`
- `max_chunks_per_page`
- `chunk_dedupe_similarity`
- `trust_relevance_weight`
- `trust_authority_weight`
- `trust_recency_weight`
- `trust_agreement_weight`

Environment overrides:
- `SERPAPI_RETRIEVAL_FANOUT_ENABLED`
- `SERPAPI_ALWAYS_WEB_RETRIEVAL_ENABLED`
- `SERPAPI_FALLBACK_ENABLED`
- `SERPAPI_FALLBACK_SIMILARITY_THRESHOLD`

## 6) Evaluation and Tracking
Tracked for web fallback quality:
- retrieval strategy
- source count
- groundedness
- citation accuracy
- user feedback
- trace timeline events (planner, search, verification, finalization)

Where:
- Request metrics:
  - `app/services/llm_service.py`
  - `app/services/metrics_dynamodb_service.py`
- Evaluation traces and report summary:
  - `app/services/evaluation_service.py`
  - `GET /api/v1/eval/report`
- Feedback labeling:
  - `POST /api/v1/eval/conversations/{conversation_id}/label`
  - fields: `user_feedback`, `user_feedback_score` (-1/0/1)

## 7) Chat UI Work Log (Compact Mode)
- The chat frontend renders retrieval traces in a collapsed row:
  - `Worked for <duration>` (for example `Worked for 40s`).
- Detailed view opens only when clicked.
- Expanded panel is intentionally scoped to essential signals:
  - planner mode/skip reason,
  - query count + websites searched,
  - retrieval/answer verification status,
  - final answer status,
  - top queries and key sources.
- Full raw trace event history remains available via API (`trace_events`) for debugging/export.

## 8) Quick Validation
Local search fetch:
```bash
./venv/bin/python -m app.scripts.fetch_serpapi_google \
  --query "Oxford AI masters admission" \
  --query "TU Munich AI lab"
```

Focused tests:
```bash
./venv/bin/pytest -q \
  tests/test_web_retrieval_service.py \
  tests/test_llm_service.py \
  tests/test_evaluation_service.py \
  tests/test_quality_metrics_service.py
```
