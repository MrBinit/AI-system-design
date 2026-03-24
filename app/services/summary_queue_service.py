import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from functools import lru_cache
from threading import Lock
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.infra.redis_client import worker_redis_client, worker_scoped_key

settings = get_settings()
logger = logging.getLogger(__name__)

_PROCESSING_TTL_SECONDS = 300
_COMPLETED_TTL_SECONDS = 86400
_MAX_SQS_POLL_MESSAGES = 10
_LAST_DLQ_INFO: dict | None = None
_LAST_DLQ_ALERT_AT_MONOTONIC = 0.0
_STATE_LOCK = Lock()
_SUMMARY_QUEUE_SEND_FAILURE_LOG = "Failed sending summary queue message. %s"


def _secrets_region() -> str | None:
    region = (
        os.getenv("AWS_REGION", "").strip()
        or os.getenv("AWS_DEFAULT_REGION", "").strip()
        or os.getenv("AWS_SECRETS_MANAGER_REGION", "").strip()
    )
    return region or None


@lru_cache()
def _sqs_client():
    kwargs = {"region_name": _secrets_region()} if _secrets_region() else {}
    return boto3.client("sqs", **kwargs)


def _summary_queue_url() -> str:
    return str(settings.queue.summary_queue_url or "").strip()


def _summary_dlq_url() -> str:
    return str(settings.queue.summary_dlq_url or "").strip()


def _is_summary_queue_enabled() -> bool:
    return bool(settings.queue.summary_queue_enabled and _summary_queue_url())


def _stream_key() -> str:
    """Return the worker-scoped key namespace used for summary idempotency markers."""
    return worker_scoped_key(settings.memory.summary_queue_stream_key)


def _dlq_stream_key() -> str:
    """Return the worker-scoped key namespace used for summary DLQ metadata markers."""
    return worker_scoped_key(settings.memory.summary_queue_dlq_stream_key)


def _processing_key(idempotency_key: str) -> str:
    """Build the Redis key used to mark a summary job as currently processing."""
    return f"{_stream_key()}:processing:{idempotency_key}"


def _completed_key(idempotency_key: str) -> str:
    """Build the Redis key used to mark a summary job as already completed."""
    return f"{_stream_key()}:completed:{idempotency_key}"


def _to_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_message_group_id(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return "summary"
    return text[:128]


def _json_safe_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, dict)):
        return value
    return str(value)


def _copy_job_payload(fields: dict) -> dict:
    payload = {}
    for key, value in fields.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        payload[key] = _json_safe_value(value)
    return payload


def _send_json(queue_url: str, payload: dict, message_group_id: str) -> bool:
    if not queue_url.strip():
        return False
    body = json.dumps(payload, ensure_ascii=False)
    send_kwargs = {
        "QueueUrl": queue_url.strip(),
        "MessageBody": body,
        "MessageGroupId": _safe_message_group_id(message_group_id),
    }
    try:
        _sqs_client().send_message(**send_kwargs)
        return True
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code not in {"InvalidParameterValue", "UnsupportedOperation"}:
            logger.warning(_SUMMARY_QUEUE_SEND_FAILURE_LOG, exc)
            return False
        send_kwargs.pop("MessageGroupId", None)
        try:
            _sqs_client().send_message(**send_kwargs)
            return True
        except Exception as inner_exc:
            logger.warning(_SUMMARY_QUEUE_SEND_FAILURE_LOG, inner_exc)
            return False
    except Exception as exc:
        logger.warning(_SUMMARY_QUEUE_SEND_FAILURE_LOG, exc)
        return False


def _queue_depth_and_pending(queue_url: str) -> tuple[int, int]:
    if not queue_url.strip():
        return 0, 0
    try:
        response = _sqs_client().get_queue_attributes(
            QueueUrl=queue_url.strip(),
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )
    except Exception as exc:
        logger.warning("Failed reading SQS queue attributes. %s", exc)
        return 0, 0

    attributes = response.get("Attributes", {})
    if not isinstance(attributes, dict):
        return 0, 0
    depth = max(0, _to_int(attributes.get("ApproximateNumberOfMessages"), 0))
    pending = max(0, _to_int(attributes.get("ApproximateNumberOfMessagesNotVisible"), 0))
    return depth, pending


def _remember_latest_dlq(payload: dict) -> None:
    global _LAST_DLQ_INFO
    latest_info = {
        "stream_id": str(payload.get("job_id", "")),
        "job_id": str(payload.get("job_id", "")),
        "user_id": str(payload.get("user_id", "")),
        "failed_at": str(payload.get("failed_at", "")),
        "error": str(payload.get("error", "")),
        "final_attempt": str(payload.get("final_attempt", "")),
    }
    with _STATE_LOCK:
        _LAST_DLQ_INFO = latest_info


def _receive_summary_messages() -> list[dict]:
    queue_url = _summary_queue_url()
    if not queue_url:
        return []
    try:
        response = _sqs_client().receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=min(
                settings.queue.summary_max_messages_per_poll,
                _MAX_SQS_POLL_MESSAGES,
            ),
            WaitTimeSeconds=settings.queue.summary_receive_wait_seconds,
            VisibilityTimeout=settings.queue.summary_visibility_timeout_seconds,
            MessageAttributeNames=["All"],
            AttributeNames=["All"],
        )
    except Exception as exc:
        logger.warning("Failed reading summary queue. %s", exc)
        return []
    messages = response.get("Messages", [])
    return messages if isinstance(messages, list) else []


def build_summary_job_idempotency_key(
    *,
    user_id: str,
    cutoff_seq: int,
    trigger: str,
    enqueue_version: int,
) -> str:
    """Create a deterministic idempotency key for a logical summary job."""
    raw = f"{user_id}:{cutoff_seq}:{trigger}:{enqueue_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_summary_job_idempotency_key(fields: dict) -> str:
    """Read or reconstruct the idempotency key from one queue entry payload."""
    explicit = fields.get("idempotency_key")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    user_id = fields.get("user_id", "")
    trigger = fields.get("trigger", "summary_trigger")
    try:
        cutoff_seq = int(fields.get("cutoff_seq", "0"))
        enqueue_version = int(fields.get("enqueue_version", "0"))
    except (TypeError, ValueError):
        cutoff_seq = 0
        enqueue_version = 0

    if isinstance(user_id, str) and user_id and cutoff_seq > 0:
        return build_summary_job_idempotency_key(
            user_id=user_id,
            cutoff_seq=cutoff_seq,
            trigger=trigger if isinstance(trigger, str) else "summary_trigger",
            enqueue_version=enqueue_version,
        )

    job_id = fields.get("job_id", "")
    if isinstance(job_id, str) and job_id.strip():
        return f"job:{job_id.strip()}"
    return ""


def ensure_consumer_group():
    """No-op for SQS-backed summary queue; kept for backward-compatible call sites."""
    return None


def get_summary_dlq_state() -> dict:
    """Return the current summary DLQ depth and latest known failed job metadata."""
    dlq_url = _summary_dlq_url()
    depth, _ = _queue_depth_and_pending(dlq_url) if dlq_url else (0, 0)
    with _STATE_LOCK:
        latest = dict(_LAST_DLQ_INFO) if isinstance(_LAST_DLQ_INFO, dict) else None
    return {"depth": depth, "latest": latest}


def get_summary_queue_state() -> dict:
    """Return the current summary queue depth and in-flight count from SQS attributes."""
    if not _is_summary_queue_enabled():
        return {"stream_depth": 0, "pending_jobs": 0}
    depth, pending = _queue_depth_and_pending(_summary_queue_url())
    return {
        "stream_depth": depth,
        "pending_jobs": pending,
    }


def monitor_summary_dlq(*, force: bool = False) -> dict:
    """Inspect DLQ depth and emit a throttled alert when backlog crosses the threshold."""
    global _LAST_DLQ_ALERT_AT_MONOTONIC
    state = get_summary_dlq_state()
    depth = state["depth"]
    threshold = settings.memory.summary_queue_dlq_alert_threshold

    if depth < threshold:
        return {"depth": depth, "latest": state["latest"], "alerted": False}

    should_alert = force
    if not force:
        now = time.monotonic()
        with _STATE_LOCK:
            elapsed = now - _LAST_DLQ_ALERT_AT_MONOTONIC
            if elapsed >= settings.memory.summary_queue_dlq_alert_cooldown_seconds:
                _LAST_DLQ_ALERT_AT_MONOTONIC = now
                should_alert = True

    if should_alert:
        logger.error(
            "SummaryJobDLQAlert | %s",
            json.dumps(
                {
                    "depth": depth,
                    "threshold": threshold,
                    "latest": state["latest"],
                },
                sort_keys=True,
            ),
        )

    return {"depth": depth, "latest": state["latest"], "alerted": should_alert}


def enqueue_summary_job(
    *,
    user_id: str,
    cutoff_seq: int,
    trigger: str,
    enqueue_version: int,
    approx_removed_tokens: int,
    attempt: int = 0,
) -> str | None:
    """Push a new summary compaction job onto the configured SQS summary queue."""
    if not _is_summary_queue_enabled():
        logger.warning("Summary queue is disabled or not configured.")
        return None

    job_id = str(uuid4())
    idempotency_key = build_summary_job_idempotency_key(
        user_id=user_id,
        cutoff_seq=cutoff_seq,
        trigger=trigger,
        enqueue_version=enqueue_version,
    )
    payload = {
        "job_id": job_id,
        "idempotency_key": idempotency_key,
        "user_id": user_id,
        "cutoff_seq": str(cutoff_seq),
        "trigger": trigger,
        "enqueue_version": str(enqueue_version),
        "approx_removed_tokens": str(approx_removed_tokens),
        "attempt": str(attempt),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    sent = _send_json(_summary_queue_url(), payload, message_group_id=user_id)
    if not sent:
        logger.warning("Failed to enqueue summary job for user=%s.", user_id)
        return None

    logger.info("SummaryJobEnqueued | %s", json.dumps(payload, sort_keys=True))
    return job_id


def is_summary_job_processed(idempotency_key: str) -> bool:
    """Check whether a logical summary job has already been completed."""
    if not idempotency_key:
        return False
    try:
        return bool(worker_redis_client.get(_completed_key(idempotency_key)))
    except RedisError as exc:
        logger.warning("Failed checking summary job completion marker. %s", exc)
        return False


def claim_summary_job_processing(idempotency_key: str, stream_id: str) -> bool:
    """Claim the in-progress marker for a summary job to avoid duplicate processing."""
    if not idempotency_key:
        return True
    try:
        claimed = worker_redis_client.set(
            _processing_key(idempotency_key),
            stream_id,
            ex=_PROCESSING_TTL_SECONDS,
            nx=True,
        )
        return bool(claimed)
    except RedisError as exc:
        logger.warning("Failed claiming summary job processing marker. %s", exc)
        return True


def release_summary_job_processing(idempotency_key: str):
    """Remove the in-progress marker so a failed job can be retried safely."""
    if not idempotency_key:
        return
    try:
        worker_redis_client.delete(_processing_key(idempotency_key))
    except RedisError as exc:
        logger.warning("Failed releasing summary job processing marker. %s", exc)


def mark_summary_job_processed(idempotency_key: str, stream_id: str):
    """Mark a summary job as completed and clear any in-progress marker."""
    if not idempotency_key:
        return
    try:
        worker_redis_client.set(
            _completed_key(idempotency_key),
            stream_id,
            ex=_COMPLETED_TTL_SECONDS,
        )
        worker_redis_client.delete(_processing_key(idempotency_key))
    except RedisError as exc:
        logger.warning("Failed marking summary job as processed. %s", exc)


def read_summary_jobs(consumer_name: str) -> list[tuple[str, dict]]:
    """Read the next batch of summary jobs from SQS for one worker poll cycle."""
    _ = consumer_name
    if not _is_summary_queue_enabled():
        return []

    jobs = []
    for message in _receive_summary_messages():
        receipt_handle = str(message.get("ReceiptHandle", "")).strip()
        if not receipt_handle:
            continue

        raw_body = str(message.get("Body", ""))
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            payload = {}
        fields = payload if isinstance(payload, dict) else {}
        if "attempt" not in fields:
            fields["attempt"] = "0"
        fields["_message_id"] = str(message.get("MessageId", ""))
        jobs.append((receipt_handle, fields))
    return jobs


def claim_stale_summary_jobs(consumer_name: str) -> list[tuple[str, dict]]:
    """No-op for SQS: visibility timeout manages stale/in-flight message retries."""
    _ = consumer_name
    return []


def ack_summary_job(stream_id: str):
    """Acknowledge a processed summary job by deleting its SQS message."""
    queue_url = _summary_queue_url()
    if not queue_url or not stream_id:
        return
    try:
        _sqs_client().delete_message(
            QueueUrl=queue_url,
            ReceiptHandle=stream_id,
        )
    except Exception as exc:
        logger.warning("Failed acknowledging summary job %s. %s", stream_id, exc)


def retry_or_dlq_summary_job(stream_id: str, fields: dict, error: str):
    """Retry a failed summary job or move it to the DLQ after the final attempt."""
    try:
        attempt = int(fields.get("attempt", "0")) + 1
    except (TypeError, ValueError):
        attempt = 1

    if attempt >= settings.memory.summary_queue_max_attempts:
        dlq_payload = _copy_job_payload(fields)
        dlq_payload["failed_at"] = datetime.now(timezone.utc).isoformat()
        dlq_payload["error"] = error[:500]
        dlq_payload["final_attempt"] = str(attempt)
        dlq_url = _summary_dlq_url()
        if dlq_url:
            sent = _send_json(
                dlq_url,
                dlq_payload,
                message_group_id=str(dlq_payload.get("user_id", "summary")),
            )
            if sent:
                _remember_latest_dlq(dlq_payload)
                logger.error("SummaryJobDLQ | %s", json.dumps(dlq_payload, sort_keys=True))
                monitor_summary_dlq()
            else:
                logger.warning("Failed writing summary job to DLQ.")
        else:
            logger.warning("Summary DLQ URL is not configured; dropping final failed summary job.")

        ack_summary_job(stream_id)
        return

    retry_payload = _copy_job_payload(fields)
    retry_payload["attempt"] = str(attempt)
    retry_payload["last_error"] = error[:300]
    retry_payload["retried_at"] = datetime.now(timezone.utc).isoformat()

    sent = _send_json(
        _summary_queue_url(),
        retry_payload,
        message_group_id=str(retry_payload.get("user_id", "summary")),
    )
    if not sent:
        logger.warning("Failed to retry summary job %s.", stream_id)
        return
    ack_summary_job(stream_id)
