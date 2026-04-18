import asyncio
import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import boto3
from anyio import fail_after
from botocore.config import Config as BotoConfig

from app.core.config import get_settings
from app.infra.circuit import get_embedding_breaker, get_llm_breaker

settings = get_settings()
logger = logging.getLogger(__name__)
_bedrock_runtime_client = None
_BEDROCK_EXECUTOR = ThreadPoolExecutor(
    max_workers=settings.io.bedrock_executor_workers,
    thread_name_prefix="bedrock-io",
)
_RATE_BUCKETS_LOCK = asyncio.Lock()
_RATE_BUCKETS: dict[str, dict[str, float]] = {}


def _retry_max_attempts() -> int:
    return max(1, int(getattr(settings.bedrock, "throttle_retry_max_attempts", 5)))


def _retry_base_backoff_seconds() -> float:
    return max(0.0, float(getattr(settings.bedrock, "throttle_retry_base_backoff_seconds", 0.4)))


def _retry_max_backoff_seconds() -> float:
    return max(0.1, float(getattr(settings.bedrock, "throttle_retry_max_backoff_seconds", 8.0)))


def _retry_jitter_seconds() -> float:
    return max(0.0, float(getattr(settings.bedrock, "throttle_retry_jitter_seconds", 0.25)))


def _throttle_sleep_seconds(attempt: int) -> float:
    base = _retry_base_backoff_seconds()
    cap = _retry_max_backoff_seconds()
    jitter = _retry_jitter_seconds()
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    if jitter > 0:
        delay += random.uniform(0.0, jitter)
    return max(0.0, delay)


def _is_retryable_throttling_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = str(error.get("Code", "")).strip()
            if code in {
                "ThrottlingException",
                "TooManyRequestsException",
                "RequestLimitExceeded",
                "Throttling",
            }:
                return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "throttl",
            "too many requests",
            "rate exceeded",
            "slow down",
        )
    )


def _profile_rate_limit(profile: str) -> tuple[float, int]:
    normalized = str(profile or "answer").strip().lower()
    if normalized == "planner":
        return (
            max(0.0, float(getattr(settings.bedrock, "planner_rate_limit_rps", 0.0))),
            max(0, int(getattr(settings.bedrock, "planner_rate_limit_burst", 0))),
        )
    return (
        max(0.0, float(getattr(settings.bedrock, "answer_rate_limit_rps", 0.0))),
        max(0, int(getattr(settings.bedrock, "answer_rate_limit_burst", 0))),
    )


async def _await_rate_limit_slot(*, model_id: str, profile: str) -> None:
    rate_per_second, burst = _profile_rate_limit(profile)
    if rate_per_second <= 0.0 or burst <= 0:
        return
    key = f"{profile}:{model_id or 'default'}"
    while True:
        async with _RATE_BUCKETS_LOCK:
            now = time.monotonic()
            state = _RATE_BUCKETS.get(key)
            tokens = float(state["tokens"]) if isinstance(state, dict) else float(burst)
            updated_at = float(state["updated_at"]) if isinstance(state, dict) else now
            refill = max(0.0, now - updated_at) * rate_per_second
            tokens = min(float(burst), tokens + refill)
            if tokens >= 1.0:
                _RATE_BUCKETS[key] = {"tokens": tokens - 1.0, "updated_at": now}
                return
            _RATE_BUCKETS[key] = {"tokens": tokens, "updated_at": now}
            wait_seconds = (1.0 - tokens) / rate_per_second
        await asyncio.sleep(min(1.0, max(0.001, wait_seconds)))


def _invoke_with_throttle_retry(
    *,
    model_id: str,
    rate_limit_profile: str,
    operation: str,
    invoke,
):
    attempts = _retry_max_attempts()
    for attempt in range(1, attempts + 1):
        try:
            return invoke()
        except Exception as exc:
            if not _is_retryable_throttling_error(exc) or attempt >= attempts:
                raise
            sleep_seconds = _throttle_sleep_seconds(attempt)
            logger.warning(
                "BedrockThrottledRetry | operation=%s | model=%s | profile=%s | "
                "attempt=%s/%s | sleep_ms=%s | error=%s",
                operation,
                model_id,
                rate_limit_profile,
                attempt,
                attempts,
                int(sleep_seconds * 1000),
                exc,
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)


def get_bedrock_runtime_client():
    """Return a cached Bedrock runtime client for the configured AWS region."""
    global _bedrock_runtime_client
    if _bedrock_runtime_client is None:
        retry_mode = str(os.getenv("AWS_RETRY_MODE", "standard")).strip().lower() or "standard"
        try:
            max_attempts = int(os.getenv("AWS_MAX_ATTEMPTS", "2"))
        except (TypeError, ValueError):
            max_attempts = 2
        max_attempts = max(1, max_attempts)
        _bedrock_runtime_client = boto3.client(
            service_name="bedrock-runtime",
            region_name=settings.embedding.region_name,
            config=BotoConfig(retries={"mode": retry_mode, "max_attempts": max_attempts}),
        )
    return _bedrock_runtime_client


def _normalized_timeout_seconds(timeout_value: int | float | None) -> float | None:
    """Normalize timeout config into positive seconds or None when disabled."""
    if timeout_value is None:
        return None
    try:
        seconds = float(timeout_value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


@asynccontextmanager
async def _timeout_scope(timeout_value: int | float | None):
    """Apply asyncio timeout only when a positive timeout is configured."""
    seconds = _normalized_timeout_seconds(timeout_value)
    if seconds is None:
        yield
        return
    with fail_after(seconds):
        yield


async def _run_in_bedrock_executor(fn):
    """Run one blocking Bedrock SDK operation on the dedicated Bedrock thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_BEDROCK_EXECUTOR, fn)


async def aconverse(
    payload: dict[str, Any],
    *,
    rate_limit_profile: str = "answer",
) -> dict[str, Any]:
    """Invoke Bedrock Converse asynchronously using the dedicated Bedrock executor."""
    model_id = str(payload.get("modelId", "")).strip() or "default"

    def _invoke():
        client = get_bedrock_runtime_client()
        return _invoke_with_throttle_retry(
            model_id=model_id,
            rate_limit_profile=rate_limit_profile,
            operation="converse",
            invoke=lambda: get_llm_breaker(model_id).call(client.converse, **payload),
        )

    async with _timeout_scope(settings.bedrock.timeout):
        await _await_rate_limit_slot(model_id=model_id, profile=rate_limit_profile)
        return await _run_in_bedrock_executor(_invoke)


async def ainvoke_model(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Invoke Bedrock invoke_model asynchronously using the dedicated Bedrock executor."""

    def _invoke():
        client = get_bedrock_runtime_client()
        return get_embedding_breaker().call(client.invoke_model, **payload)

    async with _timeout_scope(settings.bedrock.timeout):
        return await _run_in_bedrock_executor(_invoke)


async def ainvoke_model_json(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Invoke Bedrock and decode the model body into a JSON object in one async call."""

    def _invoke_and_decode():
        client = get_bedrock_runtime_client()
        response = get_embedding_breaker().call(client.invoke_model, **payload)
        raw_body = response.get("body").read()
        if isinstance(raw_body, bytes):
            raw_body = raw_body.decode("utf-8")
        payload_obj = json.loads(raw_body)
        if not isinstance(payload_obj, dict):
            raise ValueError("Bedrock response body must decode to a JSON object.")
        return payload_obj

    async with _timeout_scope(settings.bedrock.timeout):
        return await _run_in_bedrock_executor(_invoke_and_decode)


def _parse_converse_stream_event(event: dict[str, Any]) -> tuple[str, Exception | None]:
    """Extract text deltas or service-side stream errors from one ConverseStream event."""
    if not isinstance(event, dict):
        return "", None

    content_delta = event.get("contentBlockDelta")
    if isinstance(content_delta, dict):
        delta_payload = content_delta.get("delta")
        if isinstance(delta_payload, dict):
            delta_text = delta_payload.get("text")
            if isinstance(delta_text, str) and delta_text:
                return delta_text, None

    error_keys = (
        "internalServerException",
        "modelStreamErrorException",
        "validationException",
        "throttlingException",
        "serviceUnavailableException",
    )
    for key in error_keys:
        if key not in event:
            continue
        raw = event.get(key)
        if isinstance(raw, dict):
            message = str(raw.get("message") or raw)
        else:
            message = str(raw)
        return "", RuntimeError(f"Bedrock stream error ({key}): {message}")
    return "", None


def _put_stream_item(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue, item: object) -> None:
    """Push one stream item into the async queue from a worker thread."""
    loop.call_soon_threadsafe(queue.put_nowait, item)


def _produce_converse_stream(
    payload: dict[str, Any],
    rate_limit_profile: str,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue,
    sentinel: object,
) -> None:
    """Pull Bedrock stream events in a worker thread and publish parsed deltas/errors."""
    try:
        client = get_bedrock_runtime_client()
        model_id = str(payload.get("modelId", "")).strip() or "default"
        response = _invoke_with_throttle_retry(
            model_id=model_id,
            rate_limit_profile=rate_limit_profile,
            operation="converse_stream",
            invoke=lambda: get_llm_breaker(model_id).call(client.converse_stream, **payload),
        )
        for event in response.get("stream", []):
            text, stream_error = _parse_converse_stream_event(event)
            if stream_error is not None:
                _put_stream_item(loop, queue, stream_error)
                return
            if text:
                _put_stream_item(loop, queue, text)
    except Exception as exc:
        _put_stream_item(loop, queue, exc)
    finally:
        _put_stream_item(loop, queue, sentinel)


def _consume_stream_item(item: object, sentinel: object) -> tuple[bool, str]:
    """Translate queue payloads into stream control signals and text output."""
    if item is sentinel:
        return True, ""
    if isinstance(item, Exception):
        raise item
    return False, str(item)


async def aconverse_stream_text(
    payload: dict[str, Any],
    *,
    rate_limit_profile: str = "answer",
) -> AsyncIterator[str]:
    """Yield incremental text deltas from Bedrock ConverseStream."""
    model_id = str(payload.get("modelId", "")).strip() or "default"
    await _await_rate_limit_slot(model_id=model_id, profile=rate_limit_profile)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    producer_task = loop.run_in_executor(
        _BEDROCK_EXECUTOR,
        _produce_converse_stream,
        payload,
        rate_limit_profile,
        loop,
        queue,
        sentinel,
    )
    try:
        while True:
            item = await queue.get()
            is_done, text = _consume_stream_item(item, sentinel)
            if is_done:
                break
            yield text
    finally:
        await producer_task
