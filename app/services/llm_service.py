import asyncio
from contextlib import suppress
import hashlib
import inspect
import json
import logging
import os
import re
import time
from types import SimpleNamespace
from typing import AsyncIterator
from urllib.parse import urlparse
from uuid import uuid4
from redis.exceptions import RedisError
from app.core.config import get_prompts, get_settings
from app.infra.bedrock_chat_client import client
from app.infra.io_limiters import dependency_limiter
from app.infra.redis_client import app_scoped_key, async_redis_client, redis_client
from app.services.chat_trace_service import emit_trace_event
from app.services.evaluation_service import store_chat_trace
from app.services.guardrails_service import (
    apply_context_guardrails,
    guard_model_output,
    guard_user_input,
    refusal_response,
)
from app.services.memory_service import build_context, update_memory
from app.services.quality_metrics_service import citation_accuracy_score, generation_metrics
from app.services.reranker_service import arerank_retrieval_results
from app.services.sqs_event_queue_service import enqueue_metrics_record_event
from app.services.retrieval_service import aretrieve_document_chunks
from app.services.web_retrieval_service import aretrieve_web_chunks

settings = get_settings()
prompts = get_prompts()
logger = logging.getLogger(__name__)

_BACKGROUND_TASKS: set[asyncio.Task] = set()
_RETRIEVAL_QUERY_MAX_CHARS = 900
_RETRIEVAL_CONTEXT_MAX_CHARS = 1500
_RETRIEVAL_CHUNK_MAX_CHARS = 360
_RETRIEVAL_MAX_PROMPT_RESULTS = 3
_RETRIEVAL_EVIDENCE_MAX_ITEMS = 3
_RETRIEVAL_EVIDENCE_CONTENT_MAX_CHARS = 700
_CITATION_URL_RE = re.compile(r"https?://[^\s<>\")\]]+")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)
_STREAM_GUARD_HOLDBACK_CHARS = 120
_LLM_MOCK_MODE_ENV = "LLM_MOCK_MODE"
_LLM_MOCK_TEXT_ENV = "LLM_MOCK_TEXT"
_LLM_MOCK_DELAY_MS_ENV = "LLM_MOCK_DELAY_MS"
_LLM_MOCK_STREAM_CHUNK_CHARS_ENV = "LLM_MOCK_STREAM_CHUNK_CHARS"
_RETRIEVAL_DISABLED_ENV = "RETRIEVAL_DISABLED"
_NO_RELEVANT_INFORMATION_DETAIL = "Sorry, no relevant information is found."
_DEFAULT_EXECUTION_MODE = "deep"
_FAST_MODE = "fast"
_DEEP_MODE = "deep"
_AUTO_MODE = "auto"
_DEADLINE_QUERY_RE = re.compile(
    r"\b(application\s+deadline|deadline|last\s+date|closing\s+date|apply\s+by)\b",
    flags=re.IGNORECASE,
)
_FRESHNESS_QUERY_RE = re.compile(
    r"\b(latest|today|current|recent|news|deadline|updated?)\b",
    flags=re.IGNORECASE,
)
_DEEP_COMPLEXITY_RE = re.compile(
    r"\b(vs|versus|compare|comparison|best|top|rank|ranking|pros and cons|strategy|"
    r"analyz(?:e|ing)|debug|plan|step by step|multi|including|latest|today|deadline)\b",
    flags=re.IGNORECASE,
)
_FOLLOW_UP_REFERENCE_RE = re.compile(
    r"\b(it|its|that|those|them|this|these|former|latter|same|above|previous)\b",
    flags=re.IGNORECASE,
)
_FOLLOW_UP_PREFIXES = (
    "and ",
    "also ",
    "what about",
    "how about",
    "same for",
    "continue",
    "then ",
    "now ",
)
_DATE_LIKE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|"
    r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?\b|"
    r"\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4}\b",
    flags=re.IGNORECASE,
)


def _chat_cache_key(
    user_id: str,
    prompt: str,
    session_id: str | None = None,
    mode: str | None = None,
) -> str:
    """Build a fixed-length chat cache key without embedding raw prompt text."""
    normalized_session = str(session_id or user_id).strip() or str(user_id).strip()
    normalized_mode = _normalized_request_mode(mode)
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if mode is None or normalized_mode == _DEFAULT_EXECUTION_MODE:
        return app_scoped_key("cache", "chat", user_id, normalized_session, f"sha256:{prompt_hash}")
    return app_scoped_key(
        "cache",
        "chat",
        user_id,
        normalized_session,
        f"mode:{normalized_mode}",
        f"sha256:{prompt_hash}",
    )


def _resolve_session_id(user_id: str, session_id: str | None) -> str:
    """Resolve an effective session identifier with a safe fallback."""
    candidate = str(session_id or "").strip()
    return candidate or str(user_id).strip()


def _conversation_user_id(user_id: str, session_id: str) -> str:
    """Return memory key space id; isolate when a distinct session id is provided."""
    normalized_user = str(user_id).strip()
    normalized_session = str(session_id).strip()
    if not normalized_session or normalized_session == normalized_user:
        return normalized_user
    return f"{normalized_user}::session::{normalized_session}"


async def _redis_call(method, *args, **kwargs):
    """Execute a Redis operation using the async client and limiter."""
    async with dependency_limiter("redis"):
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


def _elapsed_ms(started_at: float) -> int:
    """Return elapsed milliseconds from a monotonic start time."""
    return max(0, int((time.perf_counter() - started_at) * 1000))


def _traceable_urls(urls, *, limit: int = 8) -> list[str]:
    if not isinstance(urls, list):
        return []
    collected: list[str] = []
    for item in urls:
        value = _normalized_url(str(item))
        if not value:
            continue
        collected.append(value)
        if len(collected) >= limit:
            break
    return collected


def _safe_int(value) -> int | None:
    """Convert a numeric-like value to int when possible."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value) -> float | None:
    """Convert a numeric-like value to float when possible."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy_env(name: str) -> bool:
    """Parse one boolean feature flag from environment variables."""
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _normalized_request_mode(mode: str | None) -> str:
    candidate = str(mode or "").strip().lower()
    if candidate in {_FAST_MODE, _DEEP_MODE, _AUTO_MODE}:
        return candidate
    return _AUTO_MODE


def _is_complex_query_for_deep(query: str) -> bool:
    compact = " ".join(str(query or "").split())
    if not compact:
        return False
    if len(compact) >= 220:
        return True
    if compact.count("?") >= 2:
        return True
    if _DEEP_COMPLEXITY_RE.search(compact):
        return True
    return False


def _resolve_initial_execution_mode(requested_mode: str, safe_prompt: str) -> str:
    normalized = _normalized_request_mode(requested_mode)
    if normalized == _FAST_MODE:
        return _FAST_MODE
    if normalized == _DEEP_MODE:
        return _DEEP_MODE
    return _DEEP_MODE if _is_complex_query_for_deep(safe_prompt) else _FAST_MODE


def _mode_prompt_config() -> dict:
    config = _chat_config().get("mode_prompts", {})
    return config if isinstance(config, dict) else {}


def _mode_instruction_text(mode: str) -> str:
    config = _mode_prompt_config()
    if mode == _FAST_MODE:
        configured = config.get("fast_system_prompt", "")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return (
            "Execution mode: fast. Prefer one concise pass using strongest available evidence. "
            "Avoid unnecessary planning/verification loops."
        )
    configured = config.get("deep_system_prompt", "")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return (
        "Execution mode: deep. Use multi-hop reasoning over evidence, "
        "verify coverage and citations, and refine once when checks fail."
    )


def _mode_instruction_message(mode: str) -> dict:
    return {"role": "system", "content": _mode_instruction_text(mode)}


def _insert_system_message_before_dialog(messages: list, message: dict) -> list:
    if not isinstance(messages, list) or not isinstance(message, dict):
        return messages
    insert_at = 0
    for item in messages:
        if not isinstance(item, dict) or str(item.get("role", "")).lower() != "system":
            break
        insert_at += 1
    return messages[:insert_at] + [message] + messages[insert_at:]


def _execution_policy(mode: str) -> dict:
    normalized = _normalized_request_mode(mode)
    if normalized == _FAST_MODE:
        return {
            "mode": _FAST_MODE,
            "planner_enabled": False,
            "verifier_enabled": False,
            "max_attempts": 1,
            "web_search_mode": _FAST_MODE,
            "auto_requested": False,
        }
    return {
        "mode": _DEEP_MODE,
        "planner_enabled": _agentic_planner_enabled(),
        "verifier_enabled": _agentic_verifier_enabled(),
        "max_attempts": 1 + (_agentic_max_reflection_rounds() if _agentic_enabled() else 0),
        "web_search_mode": _DEEP_MODE,
        "auto_requested": normalized == _AUTO_MODE,
    }


def _should_escalate_auto_to_deep(*, result: str, state: dict) -> bool:
    if result == _NO_RELEVANT_INFORMATION_DETAIL:
        return True
    issues = state.get("agent_last_issues")
    if isinstance(issues, list) and issues:
        return True
    if bool(state.get("deadline_query", False)):
        return True
    top_similarity = _safe_float(state.get("retrieval_top_similarity"))
    if top_similarity is not None and top_similarity < 0.5:
        return True
    if int(state.get("retrieval_source_count", 0) or 0) <= 0:
        return True
    return False


def _is_citation_grounding_required() -> bool:
    chat_config = prompts.get("chat", {})
    if not isinstance(chat_config, dict):
        return True
    raw = chat_config.get("citation_grounded_required", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _chat_config() -> dict:
    config = prompts.get("chat", {})
    return config if isinstance(config, dict) else {}


def _agentic_config() -> dict:
    config = _chat_config().get("agentic", {})
    return config if isinstance(config, dict) else {}


def _agentic_enabled() -> bool:
    raw = _agentic_config().get("enabled", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _agentic_max_reflection_rounds() -> int:
    raw = _agentic_config().get("max_reflection_rounds", 1)
    value = _safe_int(raw)
    if value is None:
        return 1
    return max(0, min(3, value))


def _agentic_instruction_text() -> str:
    configured = _agentic_config().get("system_prompt", "")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return (
        "Internal workflow: (1) plan from provided evidence, (2) draft answer, "
        "(3) verify each factual claim has allowed URL citations, (4) revise once if needed. "
        "Keep internal reasoning hidden. Do not output chain-of-thought."
    )


def _agentic_instruction_message() -> dict:
    return {"role": "system", "content": _agentic_instruction_text()}


def _agentic_planner_enabled() -> bool:
    raw = _agentic_config().get("planner_enabled", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _agentic_verifier_enabled() -> bool:
    raw = _agentic_config().get("verifier_enabled", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _agentic_verifier_min_coverage_score() -> float:
    raw = _agentic_config().get("verifier_min_coverage_score", 0.75)
    value = _safe_float(raw)
    if value is None:
        return 0.75
    return max(0.0, min(1.0, value))


def _agentic_planner_system_prompt() -> str:
    configured = _agentic_config().get("planner_system_prompt", "")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return (
        "You are an internal answer planner. Think privately and return JSON only with keys: "
        "intent, subquestions, search_queries, success_criteria."
    )


def _agentic_verifier_system_prompt() -> str:
    configured = _agentic_config().get("verifier_system_prompt", "")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return (
        "You are an internal answer verifier. Return JSON only with keys: "
        "pass, coverage_score, issues, missing_points, revision_guidance."
    )


def _extract_json_object(raw: str) -> dict | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_agentic_text_list(values, *, limit: int = 6) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = " ".join(str(value).split()).strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item[:200])
        if len(normalized) >= limit:
            break
    return normalized


def _is_deadline_query(user_prompt: str) -> bool:
    return bool(_DEADLINE_QUERY_RE.search(str(user_prompt or "")))


def _is_freshness_sensitive_query(user_prompt: str) -> bool:
    return bool(_FRESHNESS_QUERY_RE.search(str(user_prompt or "")))


def _has_date_like_value(text: str) -> bool:
    return bool(_DATE_LIKE_RE.search(str(text or "")))


def _llm_mock_delay_seconds() -> float:
    """Return synthetic LLM latency in seconds for mock mode."""
    raw = os.getenv(_LLM_MOCK_DELAY_MS_ENV, "").strip()
    if not raw:
        return 0.0
    try:
        delay_ms = max(0, int(raw))
    except ValueError:
        return 0.0
    return delay_ms / 1000.0


def _llm_mock_stream_chunk_chars() -> int:
    """Return per-chunk character size used by mock streaming responses."""
    raw = os.getenv(_LLM_MOCK_STREAM_CHUNK_CHARS_ENV, "").strip()
    if not raw:
        return 24
    try:
        return max(1, int(raw))
    except ValueError:
        return 24


def _llm_mock_text(messages: list) -> str:
    """Build deterministic synthetic model output for load testing."""
    configured = os.getenv(_LLM_MOCK_TEXT_ENV, "").strip()
    if configured:
        return configured
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = str(message.get("content", "")).strip()
        if content:
            return f"[mock-llm] {content[:240]}"
    return "[mock-llm] synthetic response"


def _mock_completion_response(messages: list):
    """Build one compatibility response object matching Bedrock adapter shape."""
    text = _llm_mock_text(messages)
    prompt_tokens = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = str(message.get("content", "")).strip()
        if content:
            prompt_tokens += max(1, len(content.split()))
    prompt_tokens = max(1, prompt_tokens)
    completion_tokens = max(1, len(text.split()))
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    choice = SimpleNamespace(message=SimpleNamespace(content=text))
    return SimpleNamespace(choices=[choice], usage=usage)


def _extract_llm_usage(response) -> dict:
    """Normalize response usage information into a JSON-safe dictionary."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}

    prompt_tokens = _safe_int(getattr(usage, "prompt_tokens", None))
    completion_tokens = _safe_int(getattr(usage, "completion_tokens", None))
    total_tokens = _safe_int(getattr(usage, "total_tokens", None))
    if prompt_tokens is None or completion_tokens is None or total_tokens is None:
        return {}

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


async def _record_json_metrics(record: dict) -> None:
    """Enqueue per-request metrics for background persistence."""
    if not settings.queue.metrics_aggregation_queue_enabled:
        return
    if not settings.queue.metrics_aggregation_queue_url.strip():
        logger.warning("Metrics queue URL missing; dropping metrics event.")
        return

    async def _enqueue() -> None:
        try:
            await asyncio.to_thread(enqueue_metrics_record_event, record)
        except Exception:
            logger.warning("Metrics event enqueue failed; continuing.")

    _track_background_task(
        asyncio.create_task(_enqueue()),
        label="Metrics event enqueue",
    )
    # Yield control so this helper actually uses async semantics while remaining fire-and-forget.
    await asyncio.sleep(0)


def _build_json_metrics_record(
    *,
    request_id: str,
    started_at: float,
    user_id: str,
    session_id: str | None,
    user_prompt: str,
    safe_user_prompt: str,
    answer: str,
    outcome: str,
    metrics_state: dict | None = None,
    error_message: str = "",
    **legacy_state,
) -> dict:
    """Build the per-request metrics payload persisted to JSON."""
    state: dict = dict(legacy_state)
    if isinstance(metrics_state, dict):
        state.update(metrics_state)

    return {
        "request_id": request_id,
        "user_id": user_id,
        "session_id": str(session_id or user_id),
        "question": user_prompt,
        "question_sanitized": safe_user_prompt,
        "answer": answer,
        "outcome": outcome,
        "timings_ms": {
            "overall_response_ms": _elapsed_ms(started_at),
            "llm_response_ms": state.get("model_ms"),
            "short_term_memory_ms": state.get("build_context_ms"),
            "long_term_memory_ms": state.get("retrieval_ms"),
            "memory_update_ms": state.get("memory_update_ms"),
            "cache_read_ms": state.get("cache_read_ms"),
            "cache_write_ms": state.get("cache_write_ms"),
            "evaluation_trace_ms": state.get("evaluation_trace_ms"),
        },
        "retrieval": {
            "strategy": str(state.get("retrieval_strategy", "none")),
            "result_count": int(state.get("retrieved_count", 0)),
            "source_count": int(state.get("retrieval_source_count", 0)),
            "top_similarity": state.get("retrieval_top_similarity"),
            "reranker_applied": bool(state.get("retrieval_reranker_applied", False)),
            "reranker_ms": state.get("retrieval_reranker_ms"),
            "citation_required": bool(state.get("citation_required", False)),
            "citation_min_hosts": int(state.get("citation_min_hosts", 1) or 1),
            "evidence_urls": state.get("evidence_urls") or [],
            "evidence_domain_count": int(state.get("evidence_domain_count", 0) or 0),
            "evidence": state.get("retrieval_evidence") or [],
        },
        "quality": state.get("quality", {}),
        "hallucination_proxy": (state.get("quality") or {}).get("hallucination_proxy"),
        "llm_usage": state.get("llm_usage", {}),
        "guardrails": {
            "input_reason": str(state.get("input_guard_reason", "")),
            "context_reason": str(state.get("context_guard_reason", "")),
            "output_reason": str(state.get("output_guard_reason", "")),
        },
        "model": {
            "provider": "amazon_bedrock",
            "used_fallback": bool(state.get("used_fallback_model", False)),
            "primary_model_id": settings.bedrock.primary_model_id,
            "fallback_model_id": settings.bedrock.fallback_model_id,
            "requested_mode": str(state.get("requested_mode", _DEFAULT_EXECUTION_MODE)),
            "execution_mode": str(state.get("execution_mode", _DEFAULT_EXECUTION_MODE)),
            "auto_escalated": bool(state.get("auto_escalated", False)),
            "agentic_enabled": bool(state.get("agentic_enabled", False)),
            "agent_rounds": int(state.get("agent_rounds", 0) or 0),
            "agent_last_issues": state.get("agent_last_issues") or [],
        },
        "error": error_message,
    }


def _latency_metrics_key() -> str:
    """Return the Redis key used to store aggregate LLM latency metrics."""
    return app_scoped_key("metrics", "llm", "latency")


async def _record_latency_metrics(started_at: float, outcome: str):
    """Persist request latency metrics for observability and ops reporting."""
    latency_ms = _elapsed_ms(started_at)
    key = _latency_metrics_key()
    try:
        await _redis_call(async_redis_client.hincrby, key, "count", 1)
        await _redis_call(async_redis_client.hincrby, key, "total_ms", latency_ms)
        current_max = await _redis_call(async_redis_client.hget, key, "max_ms")
        if current_max is None or latency_ms > int(current_max):
            await _redis_call(async_redis_client.hset, key, "max_ms", latency_ms)
        await _redis_call(
            async_redis_client.hset,
            key,
            mapping={
                "last_ms": latency_ms,
                "last_outcome": outcome,
            },
        )
    except Exception:
        logger.warning("Latency metrics persistence failed; continuing.")


async def _record_pipeline_stage_metrics(
    *,
    build_context_ms: int,
    retrieval_ms: int,
    model_ms: int,
    retrieval_strategy: str,
    retrieved_count: int,
):
    """Persist stage-level latency metrics for successful full chat pipeline runs."""
    key = _latency_metrics_key()
    try:
        await _redis_call(async_redis_client.hincrby, key, "pipeline_count", 1)
        await _redis_call(
            async_redis_client.hincrby, key, "build_context_total_ms", build_context_ms
        )
        await _redis_call(async_redis_client.hincrby, key, "retrieval_total_ms", retrieval_ms)
        await _redis_call(async_redis_client.hincrby, key, "model_total_ms", model_ms)
        await _redis_call(
            async_redis_client.hset,
            key,
            mapping={
                "last_build_context_ms": build_context_ms,
                "last_retrieval_ms": retrieval_ms,
                "last_model_ms": model_ms,
                "last_retrieval_strategy": retrieval_strategy,
                "last_retrieved_count": retrieved_count,
            },
        )
    except Exception:
        logger.warning("Pipeline stage metrics persistence failed; continuing.")


def _build_retrieval_query(messages: list[dict]) -> str:
    """Build a retrieval query from the latest short-term conversation context."""
    text_parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role != "user" or not isinstance(content, str) or not content.strip():
            continue
        text_parts.append(content.strip())
    if not text_parts:
        return ""
    latest = text_parts[-1]
    if len(text_parts) == 1:
        return latest[:_RETRIEVAL_QUERY_MAX_CHARS].strip()

    compact_latest = " ".join(latest.split()).strip()
    lower_latest = compact_latest.lower()
    follow_up = bool(
        any(lower_latest.startswith(prefix) for prefix in _FOLLOW_UP_PREFIXES)
        or (
            len(compact_latest) <= 220
            and _FOLLOW_UP_REFERENCE_RE.search(compact_latest) is not None
        )
    )
    if not follow_up:
        return latest[:_RETRIEVAL_QUERY_MAX_CHARS].strip()
    return "\n\n".join(text_parts[-2:])[:_RETRIEVAL_QUERY_MAX_CHARS].strip()


def _retrieval_result_label(metadata, index: int) -> str:
    if not isinstance(metadata, dict):
        return f"Result {index}"
    label_parts: list[str] = []
    university = metadata.get("university")
    section_heading = metadata.get("section_heading")
    if isinstance(university, str) and university.strip():
        label_parts.append(university.strip())
    if isinstance(section_heading, str) and section_heading.strip():
        label_parts.append(section_heading.strip())
    return " | ".join(label_parts) if label_parts else f"Result {index}"


def _retrieval_content_and_metadata(result) -> tuple[str, dict]:
    if not isinstance(result, dict):
        return "", {}
    content = result.get("content")
    if not isinstance(content, str) or not content.strip():
        return "", {}
    metadata = result.get("metadata")
    return content, metadata if isinstance(metadata, dict) else {}


def _prompt_retrieval_result_limit() -> int:
    reranker_target = max(3, min(6, int(settings.bedrock.reranker_top_n)))
    return max(_RETRIEVAL_MAX_PROMPT_RESULTS, reranker_target)


def _format_retrieval_context(retrieval_result: dict) -> dict | None:
    """Convert retrieved long-term chunks into a single system-context message."""
    results = retrieval_result.get("results", []) if isinstance(retrieval_result, dict) else []
    if not isinstance(results, list) or not results:
        return None

    lines = [
        "Retrieved long-term knowledge. Use this only when relevant to the user's request.",
    ]
    max_items = _prompt_retrieval_result_limit()
    seen_chunks: set[str] = set()
    used_results = 0
    for result in results:
        content, metadata = _retrieval_content_and_metadata(result)
        if not content:
            continue
        dedupe_key = " ".join(content.lower().split())[:180]
        if dedupe_key in seen_chunks:
            continue
        seen_chunks.add(dedupe_key)
        used_results += 1

        label = _retrieval_result_label(metadata, used_results)
        compact_content = " ".join(content.split())[:_RETRIEVAL_CHUNK_MAX_CHARS]
        lines.append(f"{used_results}. {label}: {compact_content}")
        if used_results >= max_items:
            break

    if len(lines) == 1:
        return None
    joined = "\n".join(lines)
    return {"role": "system", "content": joined[:_RETRIEVAL_CONTEXT_MAX_CHARS]}


def _serpapi_key_present() -> bool:
    env_name = str(settings.serpapi.api_key_env_name).strip() or "SERPAPI_API_KEY"
    return bool(os.getenv(env_name, "").strip())


def _result_similarity(result: dict) -> float | None:
    """Derive a normalized similarity score [0,1] from retrieval metadata."""
    if not isinstance(result, dict):
        return None

    explicit_similarity = _safe_float(result.get("similarity"))
    if explicit_similarity is not None:
        return max(0.0, min(1.0, explicit_similarity))

    score = _safe_float(result.get("score"))
    if score is not None and 0.0 <= score <= 1.0:
        return score

    distance = _safe_float(result.get("distance"))
    if distance is None:
        return None
    return max(0.0, min(1.0, 1.0 - distance))


def _top_retrieval_similarity(results: list[dict]) -> float | None:
    """Return best similarity score among retrieval results when available."""
    if not isinstance(results, list) or not results:
        return None
    best: float | None = None
    for result in results:
        similarity = _result_similarity(result)
        if similarity is None:
            continue
        if best is None or similarity > best:
            best = similarity
    return best


def _merge_retrieval_results(
    primary: list[dict], secondary: list[dict], *, limit: int
) -> list[dict]:
    """Merge retrieval candidates while deduping by stable identity/content keys."""
    merged: list[dict] = []
    seen: set[str] = set()
    for result in list(primary or []) + list(secondary or []):
        if not isinstance(result, dict):
            continue
        chunk_id = str(result.get("chunk_id", "")).strip()
        source_path = str(result.get("source_path", "")).strip()
        content = " ".join(str(result.get("content", "")).split()).lower()[:220]
        key = chunk_id or source_path or content
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(result)
        if len(merged) >= max(1, int(limit)):
            break
    return merged


def _should_use_web_fallback(state: dict, top_similarity: float | None = None) -> bool:
    web_ready, _ = _web_retrieval_ready()
    if not web_ready:
        return False
    if not settings.serpapi.fallback_enabled:
        return False
    if int(state.get("retrieved_count", 0) or 0) <= 0:
        return True
    if top_similarity is None:
        return False
    return top_similarity < float(settings.serpapi.fallback_similarity_threshold)


def _web_retrieval_ready() -> tuple[bool, str]:
    if not settings.serpapi.enabled:
        return False, "serpapi_disabled"
    if not _serpapi_key_present():
        return False, "serpapi_api_key_missing"
    return True, "ready"


def _should_run_web_retrieval() -> bool:
    web_ready, _ = _web_retrieval_ready()
    if not web_ready:
        return False
    return bool(getattr(settings.serpapi, "always_web_retrieval_enabled", True))


def _retrieval_fanout_enabled() -> bool:
    return bool(getattr(settings.serpapi, "retrieval_fanout_enabled", True))


def _result_dicts(rows) -> list[dict]:
    if not isinstance(rows, list):
        return []
    return [item for item in rows if isinstance(item, dict)]


def _set_retrieval_state(state: dict, results: list[dict]) -> None:
    state["retrieved_results"] = results
    state["retrieved_count"] = len(results)
    state["retrieval_source_count"] = _retrieval_source_count(results)
    state["retrieval_evidence"] = _build_retrieval_evidence(results)


async def _retrieve_vector_candidates(
    retrieval_query: str,
    state: dict,
    prefetched_result: dict | None = None,
) -> tuple[list[dict], float | None]:
    emit_trace_event(
        "retrieval_vector_started",
        {
            "query": retrieval_query[:220],
        },
    )
    retrieval_result = prefetched_result
    if retrieval_result is None:
        retrieval_result = await aretrieve_document_chunks(
            retrieval_query,
            top_k=settings.postgres.default_top_k,
        )
    state["retrieval_strategy"] = str(retrieval_result.get("retrieval_strategy", "unknown"))
    vector_results = _result_dicts(retrieval_result.get("results", []))
    top_similarity = _top_retrieval_similarity(vector_results)
    state["retrieval_top_similarity"] = top_similarity
    _set_retrieval_state(state, vector_results)
    emit_trace_event(
        "retrieval_vector_completed",
        {
            "result_count": len(vector_results),
            "top_similarity": top_similarity,
            "strategy": state["retrieval_strategy"],
        },
    )
    return vector_results, top_similarity


async def _retrieve_web_candidates_if_needed(
    retrieval_query: str,
    *,
    vector_results: list[dict],
    vector_has_urls: bool,
    top_similarity: float | None,
    search_mode: str,
    state: dict,
    web_prefetch_task: asyncio.Task | None = None,
) -> tuple[list[dict], bool]:
    web_ready, web_ready_reason = _web_retrieval_ready()
    always_web_retrieval = web_ready and _should_run_web_retrieval()
    fallback_for_low_confidence = _should_use_web_fallback(state, top_similarity)
    fallback_for_missing_urls = (
        bool(vector_results)
        and not vector_has_urls
        and web_ready
        and settings.serpapi.fallback_enabled
    )
    if not (always_web_retrieval or fallback_for_low_confidence or fallback_for_missing_urls):
        if not web_ready:
            skip_reason = web_ready_reason
        elif not settings.serpapi.fallback_enabled and not always_web_retrieval:
            skip_reason = "serpapi_fallback_disabled"
        elif vector_results and top_similarity is not None:
            skip_reason = "vector_confident"
        elif vector_results and vector_has_urls:
            skip_reason = "vector_urls_available"
        else:
            skip_reason = "not_needed"
        emit_trace_event(
            "web_retrieval_skipped",
            {
                "reason": skip_reason,
                "query": retrieval_query[:220],
                "planner_available": web_ready,
            },
        )
        if not web_ready:
            emit_trace_event(
                "query_planner_skipped",
                {
                    "reason": skip_reason,
                },
            )
        return [], False

    reason = "always_web_retrieval"
    if not always_web_retrieval:
        reason = "low_confidence" if fallback_for_low_confidence else "missing_urls"
    emit_trace_event(
        "web_fallback_started",
        {
            "reason": reason,
            "query": retrieval_query[:220],
        },
    )
    if always_web_retrieval and vector_results:
        state["retrieval_strategy"] = "hybrid_vector_web"
    elif vector_results and fallback_for_low_confidence:
        state["retrieval_strategy"] = "vector_low_confidence"
    elif vector_results and fallback_for_missing_urls:
        state["retrieval_strategy"] = "vector_missing_urls"

    try:
        emit_trace_event(
            "query_planner_started",
            {
                "query": retrieval_query[:220],
            },
        )
        if web_prefetch_task is not None:
            web_result = await web_prefetch_task
        else:
            web_result = await _aretrieve_web_chunks_with_mode(
                retrieval_query,
                top_k=settings.postgres.default_top_k,
                search_mode=search_mode,
            )
        query_plan = web_result.get("query_plan", {})
        emit_trace_event(
            "query_planner_completed",
            {
                "planner": str(query_plan.get("planner", "heuristic")),
                "llm_used": bool(query_plan.get("llm_used", False)),
                "subquestions": query_plan.get("subquestions", []),
                "queries": web_result.get("query_variants", []),
            },
        )
        web_results = _result_dicts(web_result.get("results", []))
        web_urls = _traceable_urls(_evidence_urls(web_results))
        emit_trace_event(
            "web_fallback_completed",
            {
                "result_count": len(web_results),
                "query_variants": web_result.get("query_variants", []),
                "facts": web_result.get("facts", []),
                "source_urls": web_urls,
                "retrieval_loop": web_result.get("retrieval_loop", {}),
                "query_plan": web_result.get("query_plan", {}),
            },
        )
        if web_results:
            state["retrieval_strategy"] = (
                "web_hybrid" if always_web_retrieval and vector_results else "web_fallback"
            )
        return web_results, True
    except Exception as web_exc:
        state["retrieval_strategy"] = "web_fallback_error"
        emit_trace_event(
            "web_fallback_error",
            {
                "error": "web_fallback_failed",
                "error_type": type(web_exc).__name__,
            },
        )
        logger.warning(
            "Web fallback retrieval failed; continuing without web context. %s",
            web_exc,
        )
        return [], True


async def _aretrieve_web_chunks_with_mode(
    retrieval_query: str,
    *,
    top_k: int,
    search_mode: str,
) -> dict:
    try:
        return await aretrieve_web_chunks(
            retrieval_query,
            top_k=top_k,
            search_mode=search_mode,
        )
    except TypeError as exc:
        # Backward compatibility for tests or call-sites patching an older function signature.
        if "search_mode" not in str(exc):
            raise
        return await aretrieve_web_chunks(retrieval_query, top_k=top_k)


def _merge_vector_and_web_results(
    vector_results: list[dict], web_results: list[dict]
) -> list[dict]:
    if not web_results:
        return vector_results

    merge_limit = max(
        int(settings.postgres.default_top_k),
        int(settings.serpapi.max_context_results),
        int(settings.bedrock.reranker_max_documents),
    )
    return _merge_retrieval_results(
        vector_results,
        web_results,
        limit=merge_limit,
    )


async def _rerank_if_configured(
    retrieval_query: str,
    merged_results: list[dict],
    state: dict,
) -> list[dict]:
    if not merged_results:
        return merged_results

    original_results = list(merged_results)
    min_unique_domains = max(1, int(getattr(settings.serpapi, "retrieval_min_unique_domains", 1)))
    merge_limit = max(
        int(settings.postgres.default_top_k),
        int(settings.serpapi.max_context_results),
        int(settings.bedrock.reranker_max_documents),
    )

    def _result_identity_key(result: dict) -> str:
        if not isinstance(result, dict):
            return ""
        chunk_id = str(result.get("chunk_id", "")).strip()
        source_path = str(result.get("source_path", "")).strip()
        content = " ".join(str(result.get("content", "")).split()).lower()[:220]
        return chunk_id or source_path or content

    def _result_domain(result: dict) -> str:
        if not isinstance(result, dict):
            return ""
        metadata = result.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        candidate = _normalized_url(str(metadata.get("url", ""))) or _normalized_url(
            str(result.get("source_path", ""))
        )
        return _normalized_host_from_url(candidate)

    try:
        rerank_result = await arerank_retrieval_results(retrieval_query, merged_results)
        reranked_rows = _result_dicts(rerank_result.get("results", []))
        if reranked_rows:
            merged_results = reranked_rows
            if min_unique_domains > 1:
                domains = {_result_domain(item) for item in merged_results if _result_domain(item)}
                if len(domains) < min_unique_domains:
                    seen_keys = {
                        _result_identity_key(item)
                        for item in merged_results
                        if _result_identity_key(item)
                    }
                    for candidate in original_results:
                        key = _result_identity_key(candidate)
                        if not key or key in seen_keys:
                            continue
                        candidate_domain = _result_domain(candidate)
                        if not candidate_domain or candidate_domain in domains:
                            continue
                        merged_results.append(candidate)
                        seen_keys.add(key)
                        domains.add(candidate_domain)
                        if len(merged_results) >= merge_limit or len(domains) >= min_unique_domains:
                            break
                if len(merged_results) > merge_limit:
                    merged_results = merged_results[:merge_limit]
                emit_trace_event(
                    "retrieval_rerank_diversity",
                    {
                        "required_domains": min_unique_domains,
                        "result_count": len(merged_results),
                        "domain_count": len(
                            {
                                _result_domain(item)
                                for item in merged_results
                                if _result_domain(item)
                            }
                        ),
                    },
                )
        state["retrieval_reranker_applied"] = bool(rerank_result.get("applied", False))
        timings = rerank_result.get("timings_ms", {})
        if isinstance(timings, dict):
            state["retrieval_reranker_ms"] = _safe_int(timings.get("total"))
        if state["retrieval_reranker_applied"]:
            strategy = str(state.get("retrieval_strategy", "")).strip() or "retrieval"
            state["retrieval_strategy"] = f"{strategy}_reranked"
        emit_trace_event(
            "retrieval_reranked",
            {
                "applied": state["retrieval_reranker_applied"],
                "result_count": len(merged_results),
                "reranker_ms": state.get("retrieval_reranker_ms"),
            },
        )
    except Exception as rerank_exc:
        emit_trace_event(
            "retrieval_rerank_error",
            {
                "error": "retrieval_rerank_failed",
                "error_type": type(rerank_exc).__name__,
            },
        )
        logger.warning(
            "Reranker failed; using non-reranked retrieval order. %s",
            rerank_exc,
        )
    return merged_results


def _apply_grounded_retrieval_context(
    *,
    messages: list,
    merged_results: list[dict],
    used_web_results: bool,
    state: dict,
) -> tuple[list | None, str | None]:
    if not merged_results:
        return messages, None

    _set_retrieval_state(state, merged_results)
    evidence_urls = _evidence_urls(merged_results)
    evidence_hosts = _allowed_citation_hosts(evidence_urls)
    min_unique_domains = max(1, int(getattr(settings.serpapi, "retrieval_min_unique_domains", 1)))
    state["evidence_urls"] = evidence_urls
    state["evidence_domain_count"] = len(evidence_hosts)
    state["citation_min_hosts"] = max(1, min(len(evidence_hosts), min_unique_domains))
    state["citation_required"] = True
    if not evidence_urls:
        state["context_guard_reason"] = "weak_evidence_no_urls"
        return None, _NO_RELEVANT_INFORMATION_DETAIL

    emit_trace_event(
        "evidence_selected",
        {
            "result_count": len(merged_results),
            "source_count": len(evidence_urls),
            "source_domain_count": len(evidence_hosts),
            "source_domains": list(sorted(evidence_hosts))[:8],
            "citation_min_hosts": int(state.get("citation_min_hosts", 1) or 1),
            "source_urls": _traceable_urls(evidence_urls),
        },
    )
    emit_trace_event(
        "citation_grounding_ready",
        {
            "required": True,
            "citation_min_hosts": int(state.get("citation_min_hosts", 1) or 1),
            "allowed_source_count": len(evidence_urls),
        },
    )

    context_message = (
        _format_web_retrieval_context({"results": merged_results})
        if used_web_results
        else _format_retrieval_context({"results": merged_results})
    )
    if not context_message:
        state["context_guard_reason"] = "weak_evidence_empty_context"
        return None, _NO_RELEVANT_INFORMATION_DETAIL

    return [
        _citation_grounding_message(evidence_urls),
        context_message,
    ] + messages, None


async def _augment_messages_with_retrieval(
    *,
    messages: list,
    retrieval_query: str,
    search_mode: str,
    state: dict,
    vector_prefetch_result: dict | None = None,
) -> tuple[list | None, str | None]:
    retrieval_started_at = time.perf_counter()
    web_prefetch_task: asyncio.Task | None = None
    try:
        web_ready, _ = _web_retrieval_ready()
        if _retrieval_fanout_enabled() and web_ready and _should_run_web_retrieval():
            # Fan-out: prefetch web retrieval while vector retrieval is in-flight.
            web_prefetch_task = asyncio.create_task(
                _aretrieve_web_chunks_with_mode(
                    retrieval_query,
                    top_k=settings.postgres.default_top_k,
                    search_mode=search_mode,
                )
            )
        vector_results, top_similarity = await _retrieve_vector_candidates(
            retrieval_query,
            state,
            prefetched_result=vector_prefetch_result,
        )
        vector_has_urls = bool(_evidence_urls(vector_results))
        web_results, web_fallback_attempted = await _retrieve_web_candidates_if_needed(
            retrieval_query,
            vector_results=vector_results,
            vector_has_urls=vector_has_urls,
            top_similarity=top_similarity,
            search_mode=search_mode,
            state=state,
            web_prefetch_task=web_prefetch_task,
        )
        if web_fallback_attempted and not web_results:
            freshness_sensitive = _is_freshness_sensitive_query(
                str(state.get("safe_user_prompt", ""))
            )
            if not vector_results:
                if _is_citation_grounding_required() or freshness_sensitive:
                    state["context_guard_reason"] = "no_relevant_information"
                    return None, _NO_RELEVANT_INFORMATION_DETAIL
                state["retrieval_strategy"] = "web_fallback_empty_no_vector"
                logger.info("Web fallback returned no results and vector was empty; continuing.")
            if vector_results and not vector_has_urls:
                state["context_guard_reason"] = "weak_evidence_no_urls"
                return None, _NO_RELEVANT_INFORMATION_DETAIL
            state["retrieval_strategy"] = "vector_fallback_web_empty"
            logger.info("Web fallback returned no results; using available vector evidence.")

        merged_results = _merge_vector_and_web_results(vector_results, web_results)
        merged_results = await _rerank_if_configured(retrieval_query, merged_results, state)
        return _apply_grounded_retrieval_context(
            messages=messages,
            merged_results=merged_results,
            used_web_results=bool(web_results),
            state=state,
        )
    except Exception as exc:
        state["retrieval_strategy"] = "error"
        logger.warning(
            "Long-term retrieval failed; continuing without retrieved context. %s",
            exc,
        )
        return messages, None
    finally:
        if web_prefetch_task is not None:
            if not web_prefetch_task.done():
                web_prefetch_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await web_prefetch_task
        state["retrieval_ms"] = _elapsed_ms(retrieval_started_at)


def _validate_citation_grounding_state(state: dict) -> str | None:
    if not _is_citation_grounding_required():
        return None
    if not bool(state.get("citation_required", False)):
        state["context_guard_reason"] = "weak_evidence_missing"
        return _NO_RELEVANT_INFORMATION_DETAIL
    evidence_urls = state.get("evidence_urls", [])
    if not isinstance(evidence_urls, list) or not evidence_urls:
        state["context_guard_reason"] = "weak_evidence_no_urls"
        return _NO_RELEVANT_INFORMATION_DETAIL
    return None


def _web_context_line(result: dict, index: int) -> str | None:
    content = str(result.get("content", "")).strip()
    if not content:
        return None

    metadata = result.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    title = str(metadata.get("university", "")).strip() or f"Web Result {index}"
    url = str(metadata.get("url", "")).strip()
    compact = " ".join(content.split())[:_RETRIEVAL_CHUNK_MAX_CHARS]
    if url:
        return f"{index}. {title} ({url}): {compact}"
    return f"{index}. {title}: {compact}"


def _web_context_result_lines(results: list[dict], *, max_items: int) -> list[str]:
    lines: list[str] = []
    used = 0
    for result in results:
        if not isinstance(result, dict):
            continue
        line = _web_context_line(result, used + 1)
        if not line:
            continue
        lines.append(line)
        used += 1
        if used >= max_items:
            break
    return lines


def _format_web_retrieval_context(web_result: dict) -> dict | None:
    """Convert web fallback results into one system context message with URLs."""
    results = web_result.get("results", []) if isinstance(web_result, dict) else []
    if not isinstance(results, list) or not results:
        return None

    header = [
        "Live web fallback context (Google via SerpAPI). Use only if relevant and cite URLs.",
    ]
    result_lines = _web_context_result_lines(
        results,
        max_items=_prompt_retrieval_result_limit(),
    )
    if not result_lines:
        return None
    joined = "\n".join(header + result_lines)
    return {"role": "system", "content": joined[:_RETRIEVAL_CONTEXT_MAX_CHARS]}


def _build_retrieval_evidence(results: list[dict]) -> list[dict]:
    """Build compact retrieval evidence for grounded hallucination evaluation."""
    if not isinstance(results, list):
        return []

    evidence: list[dict] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        content = str(result.get("content", "")).strip()
        if not content:
            continue
        metadata = result.get("metadata")
        evidence.append(
            {
                "chunk_id": str(result.get("chunk_id", "")),
                "source_path": str(result.get("source_path", "")),
                "distance": result.get("distance"),
                "metadata": metadata if isinstance(metadata, dict) else {},
                "content": " ".join(content.split())[:_RETRIEVAL_EVIDENCE_CONTENT_MAX_CHARS],
            }
        )
        if len(evidence) >= _RETRIEVAL_EVIDENCE_MAX_ITEMS:
            break
    return evidence


def _normalized_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    return ""


def _normalized_host_from_url(url: str) -> str:
    host = str(urlparse(url).hostname or "").strip().lower()
    if host.startswith("www."):
        return host[4:]
    return host


def _allowed_citation_hosts(evidence_urls: list[str]) -> set[str]:
    hosts: set[str] = set()
    for url in evidence_urls:
        normalized = _normalized_url(str(url))
        if not normalized:
            continue
        host = _normalized_host_from_url(normalized)
        if host:
            hosts.add(host)
    return hosts


def _evidence_urls(results: list[dict]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            continue
        metadata = result.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        candidates = [
            metadata.get("url", ""),
            result.get("source_path", ""),
        ]
        for candidate in candidates:
            normalized = _normalized_url(str(candidate))
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            urls.append(normalized)
    return urls


def _retrieval_source_count(results: list[dict]) -> int:
    seen: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            continue
        metadata = result.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        url = _normalized_url(str(metadata.get("url", "")))
        source_path = str(result.get("source_path", "")).strip()
        key = (url or source_path).strip().lower()
        if not key:
            continue
        seen.add(key)
    return len(seen)


def _citation_grounding_message(evidence_urls: list[str]) -> dict:
    lines = [
        "Citation policy:",
        "- Answer only using provided evidence.",
        "- Cite URLs explicitly in your answer for every factual claim.",
        f"- If evidence is insufficient, respond exactly: {_NO_RELEVANT_INFORMATION_DETAIL}",
        "Allowed evidence URLs:",
    ]
    for index, url in enumerate(evidence_urls, start=1):
        lines.append(f"{index}. {url}")
        if index >= 12:
            break
    return {"role": "system", "content": "\n".join(lines)[:_RETRIEVAL_CONTEXT_MAX_CHARS]}


def _response_has_allowed_citation(text: str, evidence_urls: list[str]) -> bool:
    if not text or not evidence_urls:
        return False
    allowed_hosts = _allowed_citation_hosts(evidence_urls)
    if not allowed_hosts:
        return False
    return bool(_response_cited_allowed_hosts(text, evidence_urls))


def _response_cited_allowed_hosts(text: str, evidence_urls: list[str]) -> set[str]:
    if not text or not evidence_urls:
        return set()
    allowed_hosts = _allowed_citation_hosts(evidence_urls)
    if not allowed_hosts:
        return set()
    cited_urls = _CITATION_URL_RE.findall(str(text))
    cited_hosts: set[str] = set()
    for cited_url in cited_urls:
        host = _normalized_host_from_url(cited_url)
        if host and host in allowed_hosts:
            cited_hosts.add(host)
    return cited_hosts


def _enforce_citation_grounding(result: str, state: dict) -> str:
    citation_required = bool(state.get("citation_required", False))
    if not citation_required and not _is_citation_grounding_required():
        return result
    if not citation_required:
        state["output_guard_reason"] = "weak_evidence_missing"
        return _NO_RELEVANT_INFORMATION_DETAIL
    evidence_urls = state.get("evidence_urls", [])
    evidence_urls = evidence_urls if isinstance(evidence_urls, list) else []
    if not evidence_urls:
        state["output_guard_reason"] = "weak_evidence_no_urls"
        return _NO_RELEVANT_INFORMATION_DETAIL
    if _response_has_allowed_citation(result, evidence_urls):
        return result
    state["output_guard_reason"] = "missing_citations"
    return _NO_RELEVANT_INFORMATION_DETAIL


def _merge_llm_usage(total: dict, current: dict) -> dict:
    if not total:
        return dict(current or {})
    merged = dict(total)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        merged[key] = int(merged.get(key, 0) or 0) + int(current.get(key, 0) or 0)
    return merged


def _required_citation_host_count(state: dict, evidence_urls: list[str]) -> int:
    configured = _safe_int(state.get("citation_min_hosts"))
    if configured is not None and configured > 0:
        return configured
    available_hosts = len(_allowed_citation_hosts(evidence_urls))
    return max(1, available_hosts)


def _agentic_result_issues(result: str, state: dict) -> list[str]:
    issues: list[str] = []
    citation_required = bool(state.get("citation_required", False))
    citation_policy_enabled = _is_citation_grounding_required()
    evidence_urls = state.get("evidence_urls", [])
    evidence_urls = evidence_urls if isinstance(evidence_urls, list) else []

    if (
        citation_policy_enabled
        and citation_required
        and evidence_urls
        and result != _NO_RELEVANT_INFORMATION_DETAIL
        and not _response_has_allowed_citation(result, evidence_urls)
    ):
        issues.append("missing_allowed_citations")
    if (
        citation_policy_enabled
        and citation_required
        and evidence_urls
        and result != _NO_RELEVANT_INFORMATION_DETAIL
    ):
        required_hosts = _required_citation_host_count(state, evidence_urls)
        cited_hosts = _response_cited_allowed_hosts(result, evidence_urls)
        if len(cited_hosts) < required_hosts:
            issues.append("insufficient_source_diversity")
    if bool(state.get("deadline_query", False)) and result != _NO_RELEVANT_INFORMATION_DETAIL:
        if not _has_date_like_value(result):
            issues.append("missing_deadline_date")
    if citation_policy_enabled and result == _NO_RELEVANT_INFORMATION_DETAIL and evidence_urls:
        issues.append("returned_no_relevant_information_with_available_evidence")
    return issues


def _fallback_answer_plan(state: dict) -> dict:
    query = " ".join(str(state.get("safe_user_prompt", "")).split()).strip()[:240]
    success_criteria = [
        "answer the exact user query directly",
        "include only evidence-grounded factual claims",
        "cite allowed URLs for factual claims",
    ]
    if bool(state.get("deadline_query", False)):
        success_criteria.append("include exact date values when available")
    return {
        "intent": query,
        "subquestions": [],
        "search_queries": [],
        "success_criteria": success_criteria[:5],
        "planner": "heuristic",
    }


def _normalize_answer_plan_payload(payload: dict, state: dict) -> dict:
    fallback = _fallback_answer_plan(state)
    intent = " ".join(str(payload.get("intent", "")).split()).strip()[:240] or fallback["intent"]
    return {
        "intent": intent,
        "subquestions": _normalize_agentic_text_list(payload.get("subquestions"), limit=6),
        "search_queries": _normalize_agentic_text_list(payload.get("search_queries"), limit=6),
        "success_criteria": _normalize_agentic_text_list(payload.get("success_criteria"), limit=8)
        or fallback["success_criteria"],
        "planner": "llm",
    }


def _answer_plan_message(plan: dict) -> dict:
    lines = [
        "Internal execution plan (do not expose):",
        f"- Intent: {str(plan.get('intent', '')).strip()[:220]}",
    ]
    for item in _normalize_agentic_text_list(plan.get("subquestions"), limit=6):
        lines.append(f"- Subquestion: {item}")
    for item in _normalize_agentic_text_list(plan.get("success_criteria"), limit=8):
        lines.append(f"- Success criterion: {item}")
    return {"role": "system", "content": "\n".join(lines)[:_RETRIEVAL_CONTEXT_MAX_CHARS]}


def _build_answer_planner_messages(messages: list, state: dict) -> list[dict]:
    query = str(state.get("safe_user_prompt", "")).strip()[:400]
    evidence_urls = _traceable_urls(state.get("evidence_urls", []), limit=10)
    lines = [
        f"User query: {query}",
        "Allowed evidence URLs:",
    ]
    if evidence_urls:
        lines.extend(f"- {url}" for url in evidence_urls)
    else:
        lines.append("- (none)")
    lines.append(
        "Return strict JSON only with keys: intent, subquestions, search_queries, success_criteria."
    )
    return messages + [
        {"role": "system", "content": _agentic_planner_system_prompt()},
        {"role": "user", "content": "\n".join(lines)[:_RETRIEVAL_CONTEXT_MAX_CHARS]},
    ]


async def _generate_answer_plan(*, messages: list, state: dict) -> tuple[dict, dict, int]:
    fallback = _fallback_answer_plan(state)
    if not _agentic_planner_enabled():
        emit_trace_event("answer_planning_skipped", {"reason": "planner_disabled"})
        return fallback, {}, 0

    emit_trace_event(
        "answer_planning_started",
        {"query": str(state.get("safe_user_prompt", ""))[:220]},
    )
    try:
        response = await _call_model_with_fallback(
            _build_answer_planner_messages(messages, state), state
        )
    except Exception:
        emit_trace_event(
            "answer_planning_completed",
            {"planner": "heuristic", "used_fallback": True},
        )
        return fallback, {}, int(state.get("model_ms") or 0)

    usage = _extract_llm_usage(response)
    payload = _extract_json_object(str(response.choices[0].message.content or ""))
    plan = _normalize_answer_plan_payload(payload, state) if payload else fallback
    emit_trace_event(
        "answer_plan_created",
        {
            "planner": str(plan.get("planner", "heuristic")),
            "intent": str(plan.get("intent", ""))[:220],
            "subquestions": plan.get("subquestions", []),
            "success_criteria": plan.get("success_criteria", []),
        },
    )
    emit_trace_event(
        "answer_planning_completed",
        {"planner": str(plan.get("planner", "heuristic")), "used_fallback": not bool(payload)},
    )
    return plan, usage, int(state.get("model_ms") or 0)


def _build_answer_verifier_messages(
    *,
    candidate: str,
    state: dict,
    plan: dict,
    round_number: int,
) -> list[dict]:
    query = str(state.get("safe_user_prompt", "")).strip()[:400]
    evidence_urls = _traceable_urls(state.get("evidence_urls", []), limit=10)
    required_hosts = _required_citation_host_count(state, state.get("evidence_urls", []))
    lines = [
        f"User query: {query}",
        f"Draft answer (round {round_number}): {str(candidate)[:1200]}",
        f"Required minimum cited hosts: {required_hosts}",
        f"Verifier coverage threshold: {_agentic_verifier_min_coverage_score():.2f}",
        f"Plan intent: {str(plan.get('intent', '')).strip()[:220]}",
        "Plan success criteria:",
    ]
    for item in _normalize_agentic_text_list(plan.get("success_criteria"), limit=8):
        lines.append(f"- {item}")
    lines.append("Allowed evidence URLs:")
    if evidence_urls:
        lines.extend(f"- {url}" for url in evidence_urls)
    else:
        lines.append("- (none)")
    lines.append(
        "Return strict JSON only with keys: pass, coverage_score, issues, "
        "missing_points, revision_guidance."
    )
    return [
        {"role": "system", "content": _agentic_verifier_system_prompt()},
        {"role": "user", "content": "\n".join(lines)[:2600]},
    ]


def _normalize_verifier_payload(payload: dict) -> dict:
    raw_pass = payload.get("pass")
    if isinstance(raw_pass, bool):
        passed = raw_pass
    elif isinstance(raw_pass, str):
        passed = raw_pass.strip().lower() in {"1", "true", "yes", "on"}
    else:
        passed = bool(raw_pass)

    coverage = _safe_float(payload.get("coverage_score"))
    if coverage is None:
        coverage = 0.0
    return {
        "pass": passed,
        "coverage_score": max(0.0, min(1.0, coverage)),
        "issues": _normalize_agentic_text_list(payload.get("issues"), limit=8),
        "missing_points": _normalize_agentic_text_list(payload.get("missing_points"), limit=8),
        "revision_guidance": " ".join(str(payload.get("revision_guidance", "")).split()).strip()[
            :260
        ],
    }


async def _verify_answer_with_llm(
    *,
    candidate: str,
    state: dict,
    plan: dict,
    round_number: int,
) -> tuple[dict | None, dict, int]:
    if not _agentic_verifier_enabled():
        return None, {}, 0
    try:
        response = await _call_model_with_fallback(
            _build_answer_verifier_messages(
                candidate=candidate,
                state=state,
                plan=plan,
                round_number=round_number,
            ),
            state,
        )
    except Exception:
        return None, {}, int(state.get("model_ms") or 0)
    usage = _extract_llm_usage(response)
    payload = _extract_json_object(str(response.choices[0].message.content or ""))
    if not payload:
        return None, usage, int(state.get("model_ms") or 0)
    return _normalize_verifier_payload(payload), usage, int(state.get("model_ms") or 0)


def _combined_verification_issues(base_issues: list[str], verifier: dict | None) -> list[str]:
    combined: list[str] = []
    seen: set[str] = set()

    def _add(issue: str):
        key = issue.strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        combined.append(issue[:140])

    for issue in base_issues:
        _add(str(issue))
    if not isinstance(verifier, dict):
        return combined
    coverage = _safe_float(verifier.get("coverage_score")) or 0.0
    if coverage < _agentic_verifier_min_coverage_score():
        _add("coverage_below_threshold")
    if not bool(verifier.get("pass", False)):
        for issue in _normalize_agentic_text_list(verifier.get("issues"), limit=8):
            _add(f"verifier:{issue}")
        if not verifier.get("issues"):
            _add("verifier_failed")
    for item in _normalize_agentic_text_list(verifier.get("missing_points"), limit=4):
        _add(f"missing:{item}")
    return combined


def _agentic_reflection_message(
    issues: list[str], round_number: int, verifier: dict | None = None
) -> dict:
    compact = ", ".join(issues[:5]) if issues else "quality_check_failed"
    guidance = ""
    if isinstance(verifier, dict):
        guidance = str(verifier.get("revision_guidance", "")).strip()
    extra = f" Guidance: {guidance}" if guidance else ""
    return {
        "role": "system",
        "content": (
            f"Revision round {round_number}: improve the answer. "
            f"Fix: {compact}. Use only allowed evidence URLs and keep claims verifiable.{extra}"
        ),
    }


def _is_hard_verification_failure(issues: list[str], result: str, state: dict) -> bool:
    if result == _NO_RELEVANT_INFORMATION_DETAIL:
        return True
    evidence_urls = state.get("evidence_urls", [])
    evidence_urls = evidence_urls if isinstance(evidence_urls, list) else []
    if not evidence_urls:
        return True
    if not _response_has_allowed_citation(result, evidence_urls):
        return True
    hard_markers = (
        "missing_allowed_citations",
        "insufficient_source_diversity",
        "weak_evidence",
    )
    for issue in issues:
        lowered = str(issue).strip().lower()
        if any(marker in lowered for marker in hard_markers):
            return True
    return False


def _candidate_quality_score(candidate: str, issues: list[str], state: dict) -> tuple[int, int]:
    penalty = len(issues)
    if candidate == _NO_RELEVANT_INFORMATION_DETAIL:
        penalty += 200
    if _is_hard_verification_failure(issues, candidate, state):
        penalty += 50
    length_bonus = min(len(str(candidate).strip()), 4000)
    return penalty, -length_bonus


async def _generate_agentic_answer(
    *,
    user_id: str,
    messages: list,
    policy: dict,
    state: dict,
) -> tuple[str, dict]:
    max_attempts = max(1, int(policy.get("max_attempts", 1)))
    planner_enabled = bool(policy.get("planner_enabled", False))
    verifier_enabled = bool(policy.get("verifier_enabled", False))
    attempt = 0
    model_ms_total = 0
    llm_usage_total: dict = {}
    working_messages = list(messages)
    plan = _fallback_answer_plan(state)
    final_result = _NO_RELEVANT_INFORMATION_DETAIL
    final_issues: list[str] = []
    best_result = _NO_RELEVANT_INFORMATION_DETAIL
    best_issues: list[str] = []
    best_score = (10**9, 0)
    emit_trace_event(
        "answer_synthesis_started",
        {
            "max_rounds": max_attempts,
        },
    )
    if planner_enabled:
        plan, plan_usage, plan_model_ms = await _generate_answer_plan(
            messages=working_messages, state=state
        )
        model_ms_total += plan_model_ms
        llm_usage_total = _merge_llm_usage(llm_usage_total, plan_usage)
    else:
        emit_trace_event("answer_planning_skipped", {"reason": "mode_policy"})
    working_messages = working_messages + [_answer_plan_message(plan)]

    while attempt < max_attempts:
        attempt += 1
        emit_trace_event(
            "model_round_started",
            {
                "round": attempt,
                "max_rounds": max_attempts,
            },
        )
        response = await _call_model_with_fallback(working_messages, state)
        model_ms_total += int(state.get("model_ms") or 0)
        llm_usage_total = _merge_llm_usage(llm_usage_total, _extract_llm_usage(response))

        raw_result = response.choices[0].message.content
        candidate = _extract_guarded_result(user_id=user_id, raw_result=raw_result, state=state)
        base_issues = _agentic_result_issues(candidate, state)
        verifier = None
        verifier_usage = {}
        verifier_model_ms = 0
        if verifier_enabled:
            verifier, verifier_usage, verifier_model_ms = await _verify_answer_with_llm(
                candidate=candidate,
                state=state,
                plan=plan,
                round_number=attempt,
            )
        else:
            emit_trace_event(
                "answer_verification_completed",
                {
                    "round": attempt,
                    "verified": not base_issues,
                    "issues": base_issues,
                    "base_issues": base_issues,
                    "verifier": {},
                    "mode": str(policy.get("mode", _DEEP_MODE)),
                    "verifier_skipped": True,
                },
            )
        model_ms_total += verifier_model_ms
        llm_usage_total = _merge_llm_usage(llm_usage_total, verifier_usage)
        issues = _combined_verification_issues(base_issues, verifier)
        final_result = candidate
        final_issues = issues
        candidate_score = _candidate_quality_score(candidate, issues, state)
        if candidate_score < best_score:
            best_score = candidate_score
            best_result = candidate
            best_issues = list(issues)
        emit_trace_event(
            "model_round_completed",
            {
                "round": attempt,
                "issues": issues,
                "answer_preview": str(candidate)[:260],
            },
        )
        if verifier_enabled:
            emit_trace_event(
                "answer_verification_completed",
                {
                    "round": attempt,
                    "verified": not issues,
                    "issues": issues,
                    "base_issues": base_issues,
                    "verifier": verifier or {},
                },
            )
        if not issues:
            break
        if attempt >= max_attempts:
            break

        working_messages = working_messages + [
            {"role": "assistant", "content": str(candidate)},
            _agentic_reflection_message(issues, attempt + 1, verifier),
        ]

    state["model_ms"] = model_ms_total
    state["agent_rounds"] = attempt
    final_result = best_result if best_result else final_result
    final_issues = best_issues if best_issues else final_issues
    state["agent_last_issues"] = final_issues
    if final_issues and final_result != _NO_RELEVANT_INFORMATION_DETAIL:
        if _is_hard_verification_failure(final_issues, final_result, state):
            state["output_guard_reason"] = "agent_verification_failed"
            final_result = _NO_RELEVANT_INFORMATION_DETAIL
        else:
            state["output_guard_reason"] = "agent_verification_partial"
            emit_trace_event(
                "answer_partial_with_evidence",
                {
                    "issues": final_issues[:8],
                    "answer_preview": str(final_result)[:220],
                },
            )
    emit_trace_event(
        "answer_synthesis_completed",
        {
            "rounds_used": attempt,
            "verified": not final_issues,
            "issues": final_issues,
        },
    )
    return final_result, llm_usage_total


def _track_background_task(task: asyncio.Task, *, label: str) -> None:
    """Track and log fire-and-forget tasks so they are not silently lost."""
    _BACKGROUND_TASKS.add(task)

    def _on_done(completed: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(completed)
        try:
            completed.result()
        except Exception as exc:
            logger.warning("%s failed in background. %s", label, exc)

    task.add_done_callback(_on_done)


async def _persist_evaluation_trace(
    *,
    user_id: str,
    prompt: str,
    answer: str,
    retrieved_results: list[dict],
    retrieval_strategy: str,
    build_context_ms: int,
    retrieval_ms: int,
    model_ms: int,
    quality: dict,
    evidence_urls: list[str],
) -> None:
    """Persist evaluation traces outside the request critical path."""
    await asyncio.to_thread(
        store_chat_trace,
        user_id=user_id,
        prompt=prompt,
        answer=answer,
        retrieved_results=retrieved_results,
        retrieval_strategy=retrieval_strategy,
        timings_ms={
            "build_context": build_context_ms,
            "retrieval": retrieval_ms,
            "model": model_ms,
        },
        quality=quality,
        evidence_urls=evidence_urls,
        redis=redis_client,
    )


async def _chat_completion(messages: list, *, model_id: str):
    """Send one non-streaming chat completion request."""
    if _truthy_env(_LLM_MOCK_MODE_ENV):
        delay_seconds = _llm_mock_delay_seconds()
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        return _mock_completion_response(messages)
    return await client.chat.completions.create(
        model=model_id,
        messages=messages,
    )


async def _chat_completion_stream(messages: list, *, model_id: str) -> AsyncIterator[str]:
    """Stream token deltas for one model id."""
    if _truthy_env(_LLM_MOCK_MODE_ENV):
        text = _llm_mock_text(messages)
        chunk_size = _llm_mock_stream_chunk_chars()
        delay_seconds = _llm_mock_delay_seconds()
        for start in range(0, len(text), chunk_size):
            chunk = text[start : start + chunk_size]
            if not chunk:
                continue
            yield chunk
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
        return

    async for delta in client.chat.completions.stream(
        model=model_id,
        messages=messages,
    ):
        yield delta


async def _call_primary(messages: list):
    """Send the request to the primary Bedrock model."""
    return await _chat_completion(messages, model_id=settings.bedrock.primary_model_id)


async def _call_fallback(messages: list):
    """Send the request to the fallback Bedrock model."""
    return await _chat_completion(messages, model_id=settings.bedrock.fallback_model_id)


async def _stream_primary(messages: list) -> AsyncIterator[str]:
    """Stream token deltas from the primary deployment."""
    async for delta in _chat_completion_stream(
        messages, model_id=settings.bedrock.primary_model_id
    ):
        yield delta


async def _stream_fallback(messages: list) -> AsyncIterator[str]:
    """Stream token deltas from the fallback deployment."""
    async for delta in _chat_completion_stream(
        messages, model_id=settings.bedrock.fallback_model_id
    ):
        yield delta


def _new_metrics_state() -> dict:
    return {
        "build_context_ms": None,
        "retrieval_ms": None,
        "model_ms": None,
        "memory_update_ms": None,
        "cache_read_ms": None,
        "cache_write_ms": None,
        "evaluation_trace_ms": None,
        "retrieval_strategy": "none",
        "retrieved_count": 0,
        "retrieval_source_count": 0,
        "retrieval_top_similarity": None,
        "retrieval_reranker_applied": False,
        "retrieval_reranker_ms": None,
        "retrieved_results": [],
        "retrieval_evidence": [],
        "citation_required": False,
        "citation_min_hosts": 1,
        "evidence_urls": [],
        "evidence_domain_count": 0,
        "deadline_query": False,
        "llm_usage": {},
        "quality": {},
        "agentic_enabled": _agentic_enabled(),
        "agent_rounds": 0,
        "agent_last_issues": [],
        "requested_mode": _DEFAULT_EXECUTION_MODE,
        "execution_mode": _DEFAULT_EXECUTION_MODE,
        "auto_escalated": False,
        "input_guard_reason": "",
        "context_guard_reason": "",
        "output_guard_reason": "",
        "safe_user_prompt": "",
        "used_fallback_model": False,
    }


async def _record_request_outcome(
    *,
    request_id: str,
    started_at: float,
    user_id: str,
    session_id: str,
    user_prompt: str,
    safe_user_prompt: str,
    answer: str,
    outcome: str,
    state: dict,
    error_message: str = "",
) -> None:
    await _record_json_metrics(
        _build_json_metrics_record(
            request_id=request_id,
            started_at=started_at,
            user_id=user_id,
            session_id=session_id,
            user_prompt=user_prompt,
            safe_user_prompt=safe_user_prompt,
            answer=answer,
            outcome=outcome,
            metrics_state=state,
            error_message=error_message,
        )
    )
    await _record_latency_metrics(started_at, outcome)


async def _read_cached_response(cache_key: str) -> tuple[str | None, int]:
    started_at = time.perf_counter()
    cached = None
    try:
        cached = await _redis_call(async_redis_client.get, cache_key)
    except RedisError as exc:
        logger.warning("Redis cache read failed. %s", exc)
    return cached, _elapsed_ms(started_at)


async def _prepare_messages_for_model(
    *,
    user_id: str,
    conversation_user_id: str,
    safe_user_prompt: str,
    execution_mode: str,
    policy: dict,
    state: dict,
) -> tuple[list | None, str | None]:
    build_context_started_at = time.perf_counter()
    state["deadline_query"] = _is_deadline_query(safe_user_prompt)
    vector_prefetch_task: asyncio.Task | None = None
    prefetched_vector_result: dict | None = None
    if (
        _retrieval_fanout_enabled()
        and not _truthy_env(_RETRIEVAL_DISABLED_ENV)
        and str(safe_user_prompt).strip()
    ):
        # Fan-out: prefetch vector retrieval while short-term context is loading.
        vector_prefetch_task = asyncio.create_task(
            aretrieve_document_chunks(
                str(safe_user_prompt),
                top_k=settings.postgres.default_top_k,
            )
        )
    messages = await build_context(conversation_user_id, safe_user_prompt)
    state["build_context_ms"] = _elapsed_ms(build_context_started_at)

    retrieval_query = _build_retrieval_query(messages)
    if _truthy_env(_RETRIEVAL_DISABLED_ENV):
        state["retrieval_strategy"] = "disabled"
        state["retrieval_ms"] = 0
    elif retrieval_query:
        if vector_prefetch_task is not None:
            if retrieval_query.strip() == str(safe_user_prompt).strip():
                with suppress(Exception):
                    prefetched_vector_result = await vector_prefetch_task
            else:
                if not vector_prefetch_task.done():
                    vector_prefetch_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await vector_prefetch_task
        messages, detail = await _augment_messages_with_retrieval(
            messages=messages,
            retrieval_query=retrieval_query,
            search_mode=str(policy.get("web_search_mode", _DEEP_MODE)),
            state=state,
            vector_prefetch_result=prefetched_vector_result,
        )
        if detail:
            return None, detail
    else:
        if vector_prefetch_task is not None:
            if not vector_prefetch_task.done():
                vector_prefetch_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await vector_prefetch_task
        state["retrieval_ms"] = 0

    citation_detail = _validate_citation_grounding_state(state)
    if citation_detail:
        return None, citation_detail

    chat_system_prompt = prompts.get("chat", {}).get("system_prompt", "")
    system_messages: list[dict] = []
    if state["deadline_query"]:
        system_messages.append(
            {
                "role": "system",
                "content": (
                    "Date-answer policy: when the user asks for deadlines/dates, "
                    "return exact date values from evidence with URL citations. "
                    f"If exact dates are absent, answer exactly: {_NO_RELEVANT_INFORMATION_DETAIL}"
                ),
            }
        )
    if isinstance(chat_system_prompt, str) and chat_system_prompt.strip():
        system_messages.append({"role": "system", "content": chat_system_prompt.strip()})
    if system_messages:
        messages = system_messages + messages
    messages = _insert_system_message_before_dialog(
        messages,
        _mode_instruction_message(execution_mode),
    )
    if bool(state.get("agentic_enabled", False)):
        messages = messages + [_agentic_instruction_message()]

    context_guard = apply_context_guardrails(messages)
    if not context_guard["blocked"]:
        return context_guard["messages"], None

    state["context_guard_reason"] = str(context_guard.get("reason", "blocked_context"))
    refusal = refusal_response()
    logger.info(
        "GuardrailDecision | stage=context | user=%s | blocked=true | reason=%s",
        user_id,
        state["context_guard_reason"],
    )
    return None, refusal


async def _call_model_with_fallback(messages: list, state: dict):
    model_started_at = time.perf_counter()
    try:
        response = await _call_primary(messages)
    except Exception as primary_exc:
        state["used_fallback_model"] = True
        logger.warning("Primary model failed; attempting fallback. %s", primary_exc)
        try:
            response = await _call_fallback(messages)
        except Exception:
            logger.exception("Fallback model also failed.")
            state["model_ms"] = _elapsed_ms(model_started_at)
            raise
    state["model_ms"] = _elapsed_ms(model_started_at)
    return response


def _extract_guarded_result(*, user_id: str, raw_result, state: dict) -> str:
    guarded_output = guard_model_output(raw_result)
    result = guarded_output["text"]
    if guarded_output["blocked"]:
        state["output_guard_reason"] = str(guarded_output.get("reason", "blocked_output"))
        logger.info(
            "GuardrailDecision | stage=output | user=%s | blocked=true | reason=%s",
            user_id,
            state["output_guard_reason"],
        )
    grounded_result = _enforce_citation_grounding(str(result), state)
    if grounded_result == _NO_RELEVANT_INFORMATION_DETAIL:
        return grounded_result
    if bool(state.get("deadline_query", False)) and not _has_date_like_value(grounded_result):
        state["output_guard_reason"] = "deadline_missing_date"
        return _NO_RELEVANT_INFORMATION_DETAIL
    return grounded_result


def _cache_skip_reason(result: str, state: dict) -> str:
    text = str(result or "").strip()
    if not text:
        return "empty_result"
    if text == _NO_RELEVANT_INFORMATION_DETAIL:
        return "no_relevant_information"
    if text == refusal_response():
        return "guardrail_refusal"
    output_guard_reason = str(state.get("output_guard_reason", "") or "").strip()
    if output_guard_reason:
        return f"output_guard:{output_guard_reason}"
    context_guard_reason = str(state.get("context_guard_reason", "") or "").strip()
    if context_guard_reason:
        return f"context_guard:{context_guard_reason}"
    return ""


async def _update_memory_with_timing(
    *, conversation_user_id: str, safe_user_prompt: str, result: str, state: dict
) -> None:
    started_at = time.perf_counter()
    try:
        await update_memory(conversation_user_id, safe_user_prompt, result)
    except Exception as exc:
        logger.warning("Memory update failed. %s", exc)
    finally:
        state["memory_update_ms"] = _elapsed_ms(started_at)


async def _write_cache_with_timing(*, cache_key: str, result: str, state: dict) -> None:
    started_at = time.perf_counter()
    try:
        skip_reason = _cache_skip_reason(result, state)
        if skip_reason:
            emit_trace_event("cache_write_skipped", {"reason": skip_reason})
            return
        await _redis_call(
            async_redis_client.setex,
            cache_key,
            settings.memory.redis_ttl_seconds,
            result,
        )
    except RedisError as exc:
        logger.warning("Redis cache write failed. %s", exc)
    finally:
        state["cache_write_ms"] = _elapsed_ms(started_at)


def _compute_quality_metrics(*, query: str, answer: str, state: dict) -> dict:
    retrieved_results = state.get("retrieved_results", [])
    retrieved_results = retrieved_results if isinstance(retrieved_results, list) else []
    quality = generation_metrics(
        query=query,
        answer=answer,
        retrieved_results=retrieved_results,
    )
    evidence_urls = state.get("evidence_urls", [])
    evidence_urls = evidence_urls if isinstance(evidence_urls, list) else []
    quality["citation_accuracy"] = citation_accuracy_score(answer, evidence_urls)
    quality["source_count"] = float(state.get("retrieval_source_count", 0) or 0)
    return quality


def _schedule_evaluation_trace(*, user_id: str, user_prompt: str, result: str, state: dict) -> None:
    started_at = time.perf_counter()
    _track_background_task(
        asyncio.create_task(
            _persist_evaluation_trace(
                user_id=user_id,
                prompt=user_prompt,
                answer=result,
                retrieved_results=state["retrieved_results"],
                retrieval_strategy=state["retrieval_strategy"],
                build_context_ms=state["build_context_ms"] or 0,
                retrieval_ms=state["retrieval_ms"] or 0,
                model_ms=state["model_ms"] or 0,
                quality=state["quality"],
                evidence_urls=state.get("evidence_urls", []),
            )
        ),
        label="Evaluation trace persistence",
    )
    state["evaluation_trace_ms"] = _elapsed_ms(started_at)


async def _record_success_metrics(
    *,
    request_id: str,
    started_at: float,
    user_id: str,
    session_id: str,
    user_prompt: str,
    safe_user_prompt: str,
    result: str,
    state: dict,
) -> None:
    await _record_pipeline_stage_metrics(
        build_context_ms=state["build_context_ms"] or 0,
        retrieval_ms=state["retrieval_ms"] or 0,
        model_ms=state["model_ms"] or 0,
        retrieval_strategy=state["retrieval_strategy"],
        retrieved_count=state["retrieved_count"],
    )
    logger.info(
        (
            "ChatPipelineLatency | user=%s | build_context_ms=%s | retrieval_ms=%s "
            "| model_ms=%s | retrieval_strategy=%s | retrieved_count=%s"
        ),
        user_id,
        state["build_context_ms"],
        state["retrieval_ms"],
        state["model_ms"],
        state["retrieval_strategy"],
        state["retrieved_count"],
    )
    await _record_request_outcome(
        request_id=request_id,
        started_at=started_at,
        user_id=user_id,
        session_id=session_id,
        user_prompt=user_prompt,
        safe_user_prompt=safe_user_prompt,
        answer=result,
        outcome="success",
        state=state,
    )


def _new_stream_guard_state() -> dict[str, object]:
    return {"blocked": False, "reason": "", "final_text": ""}


def _guard_stream_text(assembled: str, stream_state: dict[str, object]) -> tuple[str, bool]:
    guarded = guard_model_output(assembled)
    guarded_text = str(guarded.get("text", ""))
    blocked = bool(guarded.get("blocked"))
    reason = str(guarded.get("reason", "blocked_output")) if blocked else ""
    stream_state["blocked"] = blocked
    stream_state["reason"] = reason
    stream_state["final_text"] = guarded_text
    return guarded_text, blocked


def _iter_stream_pieces(text: str, size: int):
    for start in range(0, len(text), size):
        piece = text[start : start + size]
        if piece:
            yield piece


def _stable_stream_text(guarded_text: str, emitted: str, holdback_chars: int) -> str | None:
    stable_len = len(guarded_text) - holdback_chars
    if stable_len <= 0:
        return None
    stable = guarded_text[:stable_len]
    if stable and stable != emitted:
        return stable
    return None


async def _yield_blocked_tail(
    *,
    guarded_text: str,
    emitted: str,
) -> AsyncIterator[str]:
    if guarded_text and guarded_text != emitted:
        yield guarded_text


async def _yield_stable_delta(
    *,
    guarded_text: str,
    emitted: str,
    holdback_chars: int,
) -> AsyncIterator[str]:
    stable = _stable_stream_text(guarded_text, emitted, holdback_chars)
    if stable is not None:
        yield stable


async def _iter_guarded_updates(
    *,
    text: str,
    size: int,
    stream_state: dict[str, object],
    emitted: str,
    holdback_chars: int,
    delay_seconds: float,
) -> AsyncIterator[tuple[str, bool]]:
    assembled = str(stream_state.get("_assembled", ""))
    for piece in _iter_stream_pieces(text, size):
        assembled += piece
        stream_state["_assembled"] = assembled
        guarded_text, blocked = _guard_stream_text(assembled, stream_state)
        if blocked:
            async for blocked_text in _yield_blocked_tail(
                guarded_text=guarded_text,
                emitted=emitted,
            ):
                yield blocked_text, True
            yield emitted, True
            return
        async for stable in _yield_stable_delta(
            guarded_text=guarded_text,
            emitted=emitted,
            holdback_chars=holdback_chars,
        ):
            emitted = stable
            yield emitted, False
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)


async def _yield_guarded_stream(
    delta_stream: AsyncIterator[str],
    *,
    stream_state: dict[str, object],
    chunk_size: int,
    chunk_delay_ms: int,
) -> AsyncIterator[str]:
    emitted = ""
    size = max(1, int(chunk_size))
    delay_seconds = max(0.0, float(chunk_delay_ms) / 1000.0)
    holdback_chars = max(0, _STREAM_GUARD_HOLDBACK_CHARS)
    stream_state["_assembled"] = ""

    async for delta in delta_stream:
        text = str(delta or "")
        if not text:
            continue
        async for value, blocked in _iter_guarded_updates(
            text=text,
            size=size,
            stream_state=stream_state,
            emitted=emitted,
            holdback_chars=holdback_chars,
            delay_seconds=delay_seconds,
        ):
            if value and value != emitted:
                emitted = value
                yield emitted
            if blocked:
                return

    guarded_text, _blocked = _guard_stream_text(
        str(stream_state.get("_assembled", "")), stream_state
    )
    if guarded_text and guarded_text != emitted:
        yield guarded_text


async def _emit_guarded_stream(
    delta_stream: AsyncIterator[str],
    *,
    runtime: dict[str, object],
    stream_state: dict[str, object],
    chunk_size: int,
    chunk_delay_ms: int,
) -> AsyncIterator[str]:
    async for partial in _yield_guarded_stream(
        delta_stream,
        stream_state=stream_state,
        chunk_size=chunk_size,
        chunk_delay_ms=chunk_delay_ms,
    ):
        runtime["streamed_text"] = partial
        yield partial


async def _stream_model_with_fallback(
    *,
    messages: list,
    state: dict,
    runtime: dict[str, object],
    chunk_size: int,
    chunk_delay_ms: int,
) -> AsyncIterator[str]:
    model_started_at = time.perf_counter()
    emit_trace_event("model_stream_started", {"mode": "primary"})
    try:
        async for partial in _emit_guarded_stream(
            _stream_primary(messages),
            runtime=runtime,
            stream_state=runtime["stream_guard_state"],
            chunk_size=chunk_size,
            chunk_delay_ms=chunk_delay_ms,
        ):
            yield partial
    except Exception as primary_exc:
        state["used_fallback_model"] = True
        emit_trace_event(
            "model_stream_fallback_started",
            {"error_type": type(primary_exc).__name__},
        )
        logger.warning("Primary model stream failed; attempting fallback. %s", primary_exc)
        if runtime["streamed_text"]:
            state["model_ms"] = _elapsed_ms(model_started_at)
            raise
        runtime["stream_guard_state"] = _new_stream_guard_state()
        try:
            async for partial in _emit_guarded_stream(
                _stream_fallback(messages),
                runtime=runtime,
                stream_state=runtime["stream_guard_state"],
                chunk_size=chunk_size,
                chunk_delay_ms=chunk_delay_ms,
            ):
                yield partial
        except Exception:
            logger.exception("Fallback model stream also failed.")
            state["model_ms"] = _elapsed_ms(model_started_at)
            raise
    state["model_ms"] = _elapsed_ms(model_started_at)
    emit_trace_event(
        "model_stream_completed",
        {
            "model_ms": state["model_ms"],
            "used_fallback": bool(state.get("used_fallback_model", False)),
        },
    )


def _new_request_context(
    user_id: str, user_prompt: str, session_id: str | None, requested_mode: str
) -> dict:
    effective_session_id = _resolve_session_id(user_id, session_id)
    return {
        "started_at": time.perf_counter(),
        "request_id": uuid4().hex,
        "user_id": user_id,
        "user_prompt": user_prompt,
        "effective_session_id": effective_session_id,
        "conversation_user_id": _conversation_user_id(user_id, effective_session_id),
        "safe_user_prompt": user_prompt,
        "requested_mode": _normalized_request_mode(requested_mode),
        "execution_mode": _DEFAULT_EXECUTION_MODE,
        "cache_key": "",
        "state": _new_metrics_state(),
    }


async def _record_context_outcome(
    context: dict,
    *,
    answer: str,
    outcome: str,
    error_message: str = "",
) -> None:
    await _record_request_outcome(
        request_id=str(context["request_id"]),
        started_at=float(context["started_at"]),
        user_id=str(context["user_id"]),
        session_id=str(context["effective_session_id"]),
        user_prompt=str(context["user_prompt"]),
        safe_user_prompt=str(context["safe_user_prompt"]),
        answer=answer,
        outcome=outcome,
        state=context["state"],
        error_message=error_message,
    )


async def _prepare_request(
    user_id: str,
    user_prompt: str,
    session_id: str | None,
    mode: str = _DEFAULT_EXECUTION_MODE,
) -> tuple[dict, list | None, str | None]:
    context = _new_request_context(user_id, user_prompt, session_id, mode)
    state = context["state"]
    state["requested_mode"] = str(context["requested_mode"])
    emit_trace_event(
        "request_received",
        {
            "user_id": user_id,
            "session_id": str(context["effective_session_id"]),
            "prompt_preview": str(user_prompt)[:260],
            "requested_mode": str(context["requested_mode"]),
        },
    )
    input_guard = guard_user_input(user_id, user_prompt)
    if input_guard["blocked"]:
        state["input_guard_reason"] = str(input_guard.get("reason", "blocked_input"))
        refusal = refusal_response()
        logger.info(
            "GuardrailDecision | stage=input | user=%s | blocked=true | reason=%s",
            user_id,
            state["input_guard_reason"],
        )
        await _record_context_outcome(context, answer=refusal, outcome="blocked_input")
        emit_trace_event(
            "request_blocked_input",
            {
                "reason": state["input_guard_reason"],
            },
        )
        return context, None, refusal

    context["safe_user_prompt"] = str(input_guard.get("sanitized_text", user_prompt))
    state["safe_user_prompt"] = str(context["safe_user_prompt"])
    context["execution_mode"] = _resolve_initial_execution_mode(
        str(context["requested_mode"]),
        str(context["safe_user_prompt"]),
    )
    state["execution_mode"] = str(context["execution_mode"])
    state["agentic_enabled"] = bool(str(context["execution_mode"]) == _DEEP_MODE)
    policy = _execution_policy(str(context["execution_mode"]))
    context["cache_key"] = _chat_cache_key(
        user_id,
        str(context["safe_user_prompt"]),
        str(context["effective_session_id"]),
        str(context["requested_mode"]),
    )
    cached, state["cache_read_ms"] = await _read_cached_response(str(context["cache_key"]))
    if cached:
        cached_text = str(cached)
        await _record_context_outcome(context, answer=cached_text, outcome="cache_hit")
        emit_trace_event(
            "cache_hit",
            {
                "answer_preview": cached_text[:260],
            },
        )
        return context, None, cached_text

    messages, refusal = await _prepare_messages_for_model(
        user_id=user_id,
        conversation_user_id=str(context["conversation_user_id"]),
        safe_user_prompt=str(context["safe_user_prompt"]),
        execution_mode=str(context["execution_mode"]),
        policy=policy,
        state=state,
    )
    if (
        refusal == _NO_RELEVANT_INFORMATION_DETAIL
        and str(context["requested_mode"]) == _AUTO_MODE
        and str(context["execution_mode"]) == _FAST_MODE
    ):
        emit_trace_event(
            "mode_auto_escalation_started",
            {
                "from_mode": _FAST_MODE,
                "reason": "fast_context_gate",
                "context_reason": str(state.get("context_guard_reason", "")),
            },
        )
        state["auto_escalated"] = True
        context["execution_mode"] = _DEEP_MODE
        state["execution_mode"] = _DEEP_MODE
        state["agentic_enabled"] = True
        state["context_guard_reason"] = ""
        deep_policy = _execution_policy(_DEEP_MODE)
        messages, refusal = await _prepare_messages_for_model(
            user_id=user_id,
            conversation_user_id=str(context["conversation_user_id"]),
            safe_user_prompt=str(context["safe_user_prompt"]),
            execution_mode=_DEEP_MODE,
            policy=deep_policy,
            state=state,
        )
        emit_trace_event(
            "mode_auto_escalation_completed",
            {
                "to_mode": _DEEP_MODE,
                "escalated_for_context": True,
                "success": refusal is None,
                "context_reason": str(state.get("context_guard_reason", "")),
            },
        )
    if refusal is not None:
        await _record_context_outcome(context, answer=refusal, outcome="blocked_context")
        emit_trace_event(
            "request_blocked_context",
            {
                "reason": state["context_guard_reason"],
            },
        )
        return context, None, refusal
    return context, messages, None


async def _finalize_success(context: dict, result: str) -> None:
    state = context["state"]
    state["quality"] = _compute_quality_metrics(
        query=str(context["safe_user_prompt"]),
        answer=result,
        state=state,
    )
    await _update_memory_with_timing(
        conversation_user_id=str(context["conversation_user_id"]),
        safe_user_prompt=str(context["safe_user_prompt"]),
        result=result,
        state=state,
    )
    await _write_cache_with_timing(
        cache_key=str(context["cache_key"]),
        result=result,
        state=state,
    )
    _schedule_evaluation_trace(
        user_id=str(context["user_id"]),
        user_prompt=str(context["user_prompt"]),
        result=result,
        state=state,
    )
    await _record_success_metrics(
        request_id=str(context["request_id"]),
        started_at=float(context["started_at"]),
        user_id=str(context["user_id"]),
        session_id=str(context["effective_session_id"]),
        user_prompt=str(context["user_prompt"]),
        safe_user_prompt=str(context["safe_user_prompt"]),
        result=result,
        state=state,
    )
    emit_trace_event(
        "answer_finalized",
        {
            "answer_preview": result[:260],
            "source_urls": _traceable_urls(state.get("evidence_urls", [])),
            "agent_rounds": int(state.get("agent_rounds", 0) or 0),
        },
    )


async def generate_response(
    user_id: str,
    user_prompt: str,
    session_id: str | None = None,
    mode: str = _DEFAULT_EXECUTION_MODE,
) -> str:
    """Run the full chat pipeline: guardrails, memory, model call, cache, and persistence."""
    context, messages, early_answer = await _prepare_request(
        user_id,
        user_prompt,
        session_id,
        mode=mode,
    )
    if early_answer is not None:
        return early_answer
    state = context["state"]
    policy = _execution_policy(str(context["execution_mode"]))
    policy["auto_requested"] = str(context.get("requested_mode", _DEEP_MODE)) == _AUTO_MODE

    try:
        result, llm_usage = await _generate_agentic_answer(
            user_id=user_id,
            messages=messages,
            policy=policy,
            state=state,
        )
        if (
            bool(policy.get("auto_requested"))
            and not bool(state.get("auto_escalated", False))
            and str(context.get("execution_mode", _DEEP_MODE)) == _FAST_MODE
            and _should_escalate_auto_to_deep(result=result, state=state)
        ):
            emit_trace_event(
                "mode_auto_escalation_started",
                {
                    "from_mode": str(context["execution_mode"]),
                    "reason": "fast_quality_gate",
                    "issues": state.get("agent_last_issues", []),
                },
            )
            state["auto_escalated"] = True
            context["execution_mode"] = _DEEP_MODE
            state["execution_mode"] = _DEEP_MODE
            state["agentic_enabled"] = True
            state["output_guard_reason"] = ""
            state["agent_last_issues"] = []
            deep_policy = _execution_policy(_DEEP_MODE)
            deep_result, deep_usage = await _generate_agentic_answer(
                user_id=user_id,
                messages=messages,
                policy=deep_policy,
                state=state,
            )
            result = deep_result
            llm_usage = _merge_llm_usage(llm_usage, deep_usage)
            emit_trace_event(
                "mode_auto_escalation_completed",
                {
                    "to_mode": _DEEP_MODE,
                    "answer_preview": str(result)[:260],
                },
            )
    except Exception as exc:
        await _record_context_outcome(
            context,
            answer="",
            outcome="model_error",
            error_message=str(exc),
        )
        raise

    state["llm_usage"] = llm_usage
    if state["llm_usage"]:
        logger.info(
            "LLMUsage | user=%s | prompt_tokens=%s | completion_tokens=%s | total_tokens=%s",
            user_id,
            state["llm_usage"]["prompt_tokens"],
            state["llm_usage"]["completion_tokens"],
            state["llm_usage"]["total_tokens"],
        )
    await _finalize_success(context, result)
    return result


async def generate_response_stream(
    user_id: str,
    user_prompt: str,
    session_id: str | None = None,
    mode: str = _DEFAULT_EXECUTION_MODE,
    *,
    chunk_size: int = 120,
    chunk_delay_ms: int = 12,
) -> AsyncIterator[str]:
    """Stream true model output from Bedrock and yield progressively assembled text."""
    context, messages, early_answer = await _prepare_request(
        user_id,
        user_prompt,
        session_id,
        mode=mode,
    )
    if early_answer is not None:
        yield early_answer
        return
    state = context["state"]

    runtime: dict[str, object] = {
        "streamed_text": "",
        "stream_guard_state": _new_stream_guard_state(),
    }
    try:
        async for partial in _stream_model_with_fallback(
            messages=messages,
            state=state,
            runtime=runtime,
            chunk_size=chunk_size,
            chunk_delay_ms=chunk_delay_ms,
        ):
            yield partial
    except Exception as exc:
        await _record_context_outcome(
            context,
            answer=str(runtime["streamed_text"]),
            outcome="model_error",
            error_message=str(exc),
        )
        raise

    stream_guard_state = runtime["stream_guard_state"]
    result = str(
        stream_guard_state.get("final_text", runtime["streamed_text"]) or runtime["streamed_text"]
    )
    if bool(stream_guard_state.get("blocked")):
        state["output_guard_reason"] = str(stream_guard_state.get("reason", "blocked_output"))
        logger.info(
            "GuardrailDecision | stage=output | user=%s | blocked=true | reason=%s",
            user_id,
            state["output_guard_reason"],
        )
    grounded_result = _enforce_citation_grounding(result, state)
    if grounded_result != result:
        result = grounded_result
        if result != str(runtime["streamed_text"]):
            yield result
    if int(state.get("agent_rounds", 0) or 0) <= 0:
        state["agent_rounds"] = 1
    if not isinstance(state.get("agent_last_issues"), list):
        state["agent_last_issues"] = []
    await _finalize_success(context, result)
