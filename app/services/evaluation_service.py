from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from app.core.config import get_settings
from app.core.memory_crypto import decrypt_memory_payload, encrypt_memory_payload
from app.infra.redis_client import app_scoped_key, redis_client
from app.services.quality_metrics_service import (
    aggregate_metric_rows,
    generation_metrics,
    retrieval_metrics,
)

settings = get_settings()
logger = logging.getLogger(__name__)

MAX_STORED_CONVERSATIONS = 500


def _conversation_key(conversation_id: str) -> str:
    return app_scoped_key("eval", "conversation", conversation_id)


def _user_conversation_index_key(user_id: str) -> str:
    return app_scoped_key("eval", "user", user_id, "conversations")


def _serialize_trace(trace: dict) -> str:
    return encrypt_memory_payload(trace)


def _deserialize_trace(raw: str) -> dict | None:
    parsed = decrypt_memory_payload(raw)
    return parsed if isinstance(parsed, dict) else None


def _safe_payload_results(results: list[dict] | None) -> list[dict]:
    safe_results: list[dict] = []
    for result in results or []:
        if not isinstance(result, dict):
            continue
        safe_results.append(
            {
                "chunk_id": result.get("chunk_id", ""),
                "document_id": result.get("document_id", ""),
                "metadata": result.get("metadata", {}),
                "content": result.get("content", ""),
                "distance": result.get("distance"),
            }
        )
    return safe_results


def store_chat_trace(
    *,
    user_id: str,
    prompt: str,
    answer: str,
    retrieved_results: list[dict] | None,
    retrieval_strategy: str,
    timings_ms: dict | None,
    redis=None,
) -> str | None:
    """
    Persist a chat trace so evaluation can run on real question/answer traffic.

    Failures are non-fatal to the chat pipeline.
    """
    client = redis or redis_client
    conversation_id = uuid4().hex
    now_iso = datetime.now(timezone.utc).isoformat()
    trace = {
        "conversation_id": conversation_id,
        "user_id": user_id,
        "prompt": prompt,
        "answer": answer,
        "created_at": now_iso,
        "retrieval_strategy": retrieval_strategy,
        "timings_ms": timings_ms or {},
        "retrieved_results": _safe_payload_results(retrieved_results),
        "labels": {
            "expected_answer": None,
            "relevant_chunk_ids": [],
        },
    }

    ttl_seconds = settings.memory.redis_ttl_seconds
    trace_key = _conversation_key(conversation_id)
    user_index_key = _user_conversation_index_key(user_id)

    try:
        client.setex(trace_key, ttl_seconds, _serialize_trace(trace))
        client.lpush(user_index_key, conversation_id)
        client.ltrim(user_index_key, 0, MAX_STORED_CONVERSATIONS - 1)
        client.expire(user_index_key, ttl_seconds)
        return conversation_id
    except Exception as exc:
        logger.warning("Failed to persist chat evaluation trace for user=%s. %s", user_id, exc)
        return None


def _load_trace(conversation_id: str, *, redis=None) -> dict | None:
    client = redis or redis_client
    trace_key = _conversation_key(conversation_id)
    try:
        raw = client.get(trace_key)
    except Exception:
        return None
    if not raw:
        return None
    return _deserialize_trace(raw)


def list_chat_traces(user_id: str, *, limit: int = 20, redis=None) -> list[dict]:
    """Load up to `limit` recent chat traces for a user."""
    client = redis or redis_client
    user_index_key = _user_conversation_index_key(user_id)

    try:
        conversation_ids = client.lrange(user_index_key, 0, max(0, limit - 1))
    except Exception as exc:
        logger.warning("Failed to list chat traces for user=%s. %s", user_id, exc)
        return []

    traces: list[dict] = []
    for conversation_id in conversation_ids:
        trace = _load_trace(conversation_id, redis=client)
        if not trace:
            continue
        if trace.get("user_id") != user_id:
            continue
        traces.append(trace)
    return traces


def label_chat_trace(
    *,
    user_id: str,
    conversation_id: str,
    expected_answer: str | None = None,
    relevant_chunk_ids: list[str] | None = None,
    redis=None,
) -> dict | None:
    """Attach human labels used for stronger retrieval and generation evaluation."""
    client = redis or redis_client
    trace = _load_trace(conversation_id, redis=client)
    if not trace or trace.get("user_id") != user_id:
        return None

    labels = trace.get("labels")
    if not isinstance(labels, dict):
        labels = {}

    if expected_answer is not None:
        labels["expected_answer"] = expected_answer
    if relevant_chunk_ids is not None:
        labels["relevant_chunk_ids"] = [
            chunk_id.strip()
            for chunk_id in relevant_chunk_ids
            if isinstance(chunk_id, str) and chunk_id.strip()
        ]

    trace["labels"] = labels
    trace_key = _conversation_key(conversation_id)
    try:
        client.setex(trace_key, settings.memory.redis_ttl_seconds, _serialize_trace(trace))
    except Exception as exc:
        logger.warning("Failed to persist labels for conversation=%s. %s", conversation_id, exc)
        return None
    return trace


def evaluate_trace(trace: dict) -> dict:
    """Compute retrieval and generation metrics for one stored chat trace."""
    labels = trace.get("labels") if isinstance(trace.get("labels"), dict) else {}
    relevant_chunk_ids = labels.get("relevant_chunk_ids", []) if isinstance(labels, dict) else []
    expected_answer = labels.get("expected_answer") if isinstance(labels, dict) else None

    retrieved_results = trace.get("retrieved_results", [])
    if not isinstance(retrieved_results, list):
        retrieved_results = []

    retrieval_row: dict[str, float | int] = {}
    if relevant_chunk_ids:
        retrieval_row = retrieval_metrics(retrieved_results, relevant_chunk_ids)

    generation_row = generation_metrics(
        query=str(trace.get("prompt", "")),
        answer=str(trace.get("answer", "")),
        expected_answer=expected_answer if isinstance(expected_answer, str) else None,
        retrieved_results=retrieved_results,
    )

    return {
        "retrieval": retrieval_row,
        "generation": generation_row,
    }


def get_user_evaluation_report(user_id: str, *, limit: int = 50, redis=None) -> dict:
    """Build an evaluation report over recent real conversations."""
    traces = list_chat_traces(user_id, limit=limit, redis=redis)
    conversation_rows = []
    retrieval_rows = []
    generation_rows = []
    labeled_count = 0

    for trace in traces:
        metrics = evaluate_trace(trace)
        if metrics["retrieval"]:
            retrieval_rows.append(metrics["retrieval"])
        if metrics["generation"]:
            generation_rows.append(metrics["generation"])

        labels = trace.get("labels", {})
        if isinstance(labels, dict) and (
            labels.get("expected_answer") or labels.get("relevant_chunk_ids")
        ):
            labeled_count += 1

        conversation_rows.append(
            {
                "conversation_id": trace.get("conversation_id", ""),
                "created_at": trace.get("created_at", ""),
                "prompt": trace.get("prompt", ""),
                "answer": trace.get("answer", ""),
                "retrieval_strategy": trace.get("retrieval_strategy", ""),
                "retrieved_count": len(trace.get("retrieved_results", [])),
                "labels": labels if isinstance(labels, dict) else {},
                "metrics": metrics,
            }
        )

    return {
        "user_id": user_id,
        "total_conversations": len(traces),
        "labeled_conversations": labeled_count,
        "retrieval_metrics": aggregate_metric_rows(retrieval_rows),
        "generation_metrics": aggregate_metric_rows(generation_rows),
        "conversations": conversation_rows,
    }
