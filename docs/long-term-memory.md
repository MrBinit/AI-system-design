# Long-Term Memory And Retrieval

This document describes how long-term memory is implemented in UniGraph, what retrieval experiments were run, and why the current default uses HNSW.

## Scope

Long-term memory in this project is retrieval-oriented. It stores embedded document chunks and retrieves relevant context before model generation.

It is separate from short-term chat memory:

- short-term memory: rolling conversation state in Redis
- long-term memory: embedded knowledge chunks in Postgres + pgvector

## Data Flow

Current request flow in the live chat path:

1. user question
2. short-term memory context build
3. query embedding
4. long-term vector retrieval
5. retrieved context + conversation history assembly
6. LLM call
7. answer

## Storage

Primary retrieval table:

- `unigraph.document_chunks`

Core fields:

- `chunk_id`
- `document_id`
- `content`
- `metadata` (`JSONB`)
- `embedding` (`vector(1024)`)

Embeddings are generated with Bedrock Titan text embeddings (`amazon.titan-embed-text-v2:0`, 1024 dimensions).

## Retrieval Strategy

Current runtime strategy:

- no metadata filters: ANN vector search (configured index type, currently HNSW)
- selective metadata filters: metadata-first + exact vector rerank on filtered subset

This prevents common ANN post-filter misses on selective filters.

## Experiments Run

### 1) Exact Search Baseline (`Seq Scan`)

Dataset scale during performance experiments: approximately 113k rows (synthetic duplicated expansion to stress latency paths).

Observed behavior:

- unfiltered exact search: very slow (multi-second, warm runs still high)
- filtered exact search (`country=Germany`, `entity_type=lab`): improved but still multi-second

Conclusion:

- exact full-table vector search is too slow for interactive use at this scale
- metadata filtering helps but is not enough by itself

### 2) IVFFlat Experiments

Index was built and tested with probe tuning.

Key findings:

- query shape matters: KNN-friendly `ORDER BY embedding <=> query_vector` is required
- when query shape was wrong (extra secondary sorting patterns), planner fell back to slower plans
- with correct KNN shape, unfiltered latency improved significantly

Hybrid caveat observed:

- ANN + metadata post-filter can return empty results at low search effort if nearest neighbors do not satisfy the filter
- increasing probes improves recall but increases latency

### 3) HNSW Experiments

HNSW index was created and tested on the same dataset and query patterns.

Build note seen during index creation:

- graph exceeded `maintenance_work_mem` midpoint; build still completed but slower

Query behavior:

- unfiltered latency was competitive with the best IVFFlat settings
- filtered query latency improved relative to earlier higher-effort ANN runs

Conclusion:

- HNSW provided a better default tradeoff for the current setup
- metadata-first + exact rerank remains important for selective filters

## Why HNSW Is Used As Primary ANN Index

HNSW is the current primary vector index because:

1. better practical latency/recall behavior in this project’s retrieval path
2. less fragile than relying on IVFFlat probe tuning for every query shape
3. cleaner default for unfiltered semantic retrieval

This does not remove the need for filter-aware routing:

- selective filter queries should still use metadata-first exact reranking

## Current Configuration

Postgres retrieval config includes:

- `vector_index_type: "hnsw"` (default)
- `hnsw_m`
- `hnsw_ef_construction`
- `ivfflat_lists` (kept for fallback comparison)

Embedding config includes:

- async embedding execution with task scheduling
- Redis embedding cache (TTL-based)

## Operational Observations

Latency bottleneck from live metrics typically appears in:

1. retrieval stage
2. model generation stage

Short-term memory build is usually comparatively small.

Because of that, practical latency tuning should prioritize:

1. reducing retrieved context size (`top_k`)
2. retrieval timeout fail-open behavior
3. model output token caps

## Caveats

Performance experiments used synthetic duplicated data to stress latency and planner behavior.

Use this for:

- latency and infrastructure tuning
- index strategy comparisons

Do not use synthetic duplicates to judge final retrieval relevance quality. Relevance quality should be evaluated on realistic, non-duplicated corpora and query sets.

## Next Steps

Recommended next improvements:

1. add retrieval timeout fail-open in chat pipeline
2. reduce default retrieval fanout for interactive requests
3. enforce model output token cap for latency control
4. add retrieval quality evaluation set (recall@k / MRR) on realistic data
5. add per-strategy latency dashboards (`hnsw` vs `filtered_exact`)
