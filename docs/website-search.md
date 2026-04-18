# Website Search (Tavily + Evidence-Grounded Answers)

## 1) Purpose
Enable live website fallback when vector retrieval is weak, while keeping answers grounded to evidence URLs.

## 2) Runtime Flow
`User question`
-> `Short-term context build`
-> `Retrieval fan-out (optional): speculative vector prefetch while context is loading`
-> `Vector retrieval (pgvector, reused from prefetch when query matches)`
-> `Web retrieval gate`
-> `Tavily fallback (only when needed, or always when hybrid mode is enabled)`
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
- Tavily client:
  - `app/services/tavily_search_service.py`
  - Async single and batch query support.
- Web retrieval pipeline:
  - `app/services/web_retrieval_service.py`
  - Multi-query search variants.
  - Domain allowlist filtering (for example `.de`, `.eu`).
  - Async top-page fetch.
  - Published date extraction from search rows and HTML metadata.
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
- `app/config/tavily_config.yaml`

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
- `WEB_SEARCH_RETRIEVAL_FANOUT_ENABLED`
- `WEB_SEARCH_ALWAYS_WEB_RETRIEVAL_ENABLED`
- `WEB_SEARCH_FALLBACK_ENABLED`
- `WEB_SEARCH_FALLBACK_SIMILARITY_THRESHOLD`

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

## 8) Trust and Selectivity Upgrades
- Query strategy upgrades:
  - Classifies user intent (`comparison` / `deadline` / `strategy` / `fact_lookup` / `exploration`).
  - Decomposes retrieval into query variants by intent.
  - Executes vector retrieval variants in parallel and fans in deduped results.
  - Emits `query_intent_classified` and `retrieval_query_decomposed`.
- Selective retrieval (post-rerank):
  - `llm_service` now applies an evidence selectivity filter before grounding.
  - Keeps higher-quality rows, preserves minimum diversity, and drops low-signal rows.
  - Emits `retrieval_selective_filter` trace with before/after counts.
  - Quality score combines: trust score, retrieval similarity, reranker score, authority, recency, agreement.
- Source quality diagnostics:
  - Computes answer-time trust signals from evidence:
    - confidence,
    - freshness (`fresh` / `recent` / `stale` / `unknown`),
    - contradiction flag,
    - authority and agreement aggregate scores.
  - Emits `evidence_trust_scored` and persists trust fields in metrics payloads.
- Claim-level citation linkage:
  - Verifier now checks claim-citation coverage (inline claim citation ratio),
    not only "any citation exists".
  - Low coverage is tracked as `weak_claim_citation_linkage`.
- Claim-to-snippet grounding:
  - Maps major answer claims to 1-2 supporting snippets from selected evidence.
  - Tracks grounding coverage and potential date conflicts across snippets.
  - Emits `claim_grounding_evaluated`.
- Verification gate before finalize:
  - Runs contradiction and missing-evidence-snippet checks prior to finalization.
  - Raises verifier issues such as `missing_evidence_snippets` and `contradiction_detected`.
  - If at least one authoritative source exists, verification can return a partial answer
    with an explicit `Missing info:` section instead of forcing full abstain.
- Explicit uncertainty handling:
  - For weak/conflicting evidence, final answers include an `Uncertainty:` section
    with short, concrete limitations.
  - Emits `answer_uncertainty_flagged` trace.
- Answer policy:
  - Final output is normalized to a concise first answer, then `Evidence and caveats`.
  - Evidence section references supporting snippets/URLs, caveats summarize uncertainty.
- UI trust cues:
  - Assistant messages show compact confidence/freshness/conflict badges.
  - Activity panel defaults to **essential events only** with a `Show all` toggle.
  - Sources panel is compact by default with optional expansion.
- Fast path with refinement:
  - Streams a draft as early as possible.
  - In fast mode, runs a post-stream refinement pass to tighten citations/consistency.
  - Emits `fast_refine_started` / `fast_refine_completed`.
- Deep retrieval guarantees:
  - In `deep` mode, web retrieval is always attempted when Tavily is available,
    not only fallback.
  - If vector top similarity is below `0.45` or evidence domain count is below `2`,
    deep mode runs extra web query expansion and fans in those results.
- Cache safety:
  - Abstain-like responses are not cached (including expanded variants, not only exact-match abstain text).
- Evaluation clarity:
  - Benchmark report now includes `abstain_reason` per row
    (`no_web`, `low_similarity`, `insufficient_domains`, `verifier_blocked`)
    and `abstain_reason_counts` in summary.
  - Report metrics are normalized to concrete defaults to avoid `null` fields in result rows.

## 9) Known Gaps to Reach Perplexity-Grade Quality
- Conflict detection is still heuristic (agreement/date-value checks), not full claim graph contradiction resolution.
- Claim-level citation coverage uses line heuristics; it does not yet run semantic claim alignment.
- Freshness scoring depends on published-date availability from pages/metadata.
- Authority scoring is domain-feature based; no learned credibility model is used yet.

## 10) Quick Validation
Local search fetch:
```bash
./venv/bin/python -m app.scripts.fetch_tavily_search \
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
