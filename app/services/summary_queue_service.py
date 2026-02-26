import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from redis.exceptions import RedisError, ResponseError

from app.core.config import get_settings
from app.infra.redis_client import redis_client

settings = get_settings()
logger = logging.getLogger(__name__)


def _stream_key() -> str:
    return settings.memory.summary_queue_stream_key


def _dlq_stream_key() -> str:
    return settings.memory.summary_queue_dlq_stream_key


def ensure_consumer_group():
    try:
        redis_client.xgroup_create(
            name=_stream_key(),
            groupname=settings.memory.summary_queue_group,
            id="0",
            mkstream=True,
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    except RedisError as exc:
        logger.warning("Failed to ensure summary queue consumer group. %s", exc)


def enqueue_summary_job(
    *,
    user_id: str,
    cutoff_seq: int,
    trigger: str,
    enqueue_version: int,
    approx_removed_tokens: int,
    attempt: int = 0,
) -> str | None:
    job_id = str(uuid4())
    payload = {
        "job_id": job_id,
        "user_id": user_id,
        "cutoff_seq": str(cutoff_seq),
        "trigger": trigger,
        "enqueue_version": str(enqueue_version),
        "approx_removed_tokens": str(approx_removed_tokens),
        "attempt": str(attempt),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        redis_client.xadd(
            _stream_key(),
            payload,
            maxlen=10000,
            approximate=True,
        )
        logger.info("SummaryJobEnqueued | %s", json.dumps(payload, sort_keys=True))
        return job_id
    except RedisError as exc:
        logger.warning("Failed to enqueue summary job for user=%s. %s", user_id, exc)
        return None


def read_summary_jobs(consumer_name: str) -> list[tuple[str, dict]]:
    ensure_consumer_group()
    try:
        response = redis_client.xreadgroup(
            groupname=settings.memory.summary_queue_group,
            consumername=consumer_name,
            streams={_stream_key(): ">"},
            count=settings.memory.summary_queue_read_count,
            block=settings.memory.summary_queue_block_ms,
        )
    except RedisError as exc:
        logger.warning("Failed reading summary queue. %s", exc)
        return []

    jobs = []
    for _stream, entries in response:
        for stream_id, fields in entries:
            jobs.append((stream_id, fields))
    return jobs


def ack_summary_job(stream_id: str):
    try:
        redis_client.xack(_stream_key(), settings.memory.summary_queue_group, stream_id)
    except RedisError as exc:
        logger.warning("Failed acknowledging summary job %s. %s", stream_id, exc)


def retry_or_dlq_summary_job(stream_id: str, fields: dict, error: str):
    try:
        attempt = int(fields.get("attempt", "0")) + 1
    except (TypeError, ValueError):
        attempt = 1

    if attempt >= settings.memory.summary_queue_max_attempts:
        dlq_payload = dict(fields)
        dlq_payload["failed_at"] = datetime.now(timezone.utc).isoformat()
        dlq_payload["error"] = error[:500]
        dlq_payload["final_attempt"] = str(attempt)
        try:
            redis_client.xadd(
                _dlq_stream_key(),
                dlq_payload,
                maxlen=5000,
                approximate=True,
            )
            logger.error("SummaryJobDLQ | %s", json.dumps(dlq_payload, sort_keys=True))
        except RedisError as exc:
            logger.warning("Failed writing summary job to DLQ. %s", exc)

        ack_summary_job(stream_id)
        return

    retry_payload = dict(fields)
    retry_payload["attempt"] = str(attempt)
    retry_payload["last_error"] = error[:300]
    retry_payload["retried_at"] = datetime.now(timezone.utc).isoformat()

    try:
        redis_client.xadd(
            _stream_key(),
            retry_payload,
            maxlen=10000,
            approximate=True,
        )
        ack_summary_job(stream_id)
    except RedisError as exc:
        logger.warning("Failed to retry summary job %s. %s", stream_id, exc)
