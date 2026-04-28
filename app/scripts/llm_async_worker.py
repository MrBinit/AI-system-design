"""SQS worker for async chat jobs.

Consumes jobs from SQS, runs the existing `generate_response` pipeline,
and writes completed/failed status to DynamoDB result store.
"""

from __future__ import annotations
import asyncio
import json
import logging
from app.core.config import get_settings
from app.core.security import validate_security_configuration
from app.services.llm_async_queue_service import (
    append_job_trace_event,
    delete_llm_job_message,
    get_chat_job,
    mark_job_completed,
    mark_job_failed,
    mark_job_processing,
    receive_llm_job_messages,
)
from app.services.chat_trace_service import trace_scope
from app.services.llm_service import generate_response

settings = get_settings()
logger = logging.getLogger(__name__)


def _parse_message_body(raw_body: str) -> dict:
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_append_trace(job_id: str, event: dict) -> None:
    event_type = ""
    if isinstance(event, dict):
        event_type = str(event.get("type", "")).strip()
    try:
        append_job_trace_event(job_id, event)
    except Exception as exc:
        logger.warning(
            "AsyncLLMTraceAppendFailed | job_id=%s | event_type=%s | error_type=%s | error=%s",
            job_id,
            event_type or "event",
            type(exc).__name__,
            str(exc)[:240],
        )


async def _process_message(message: dict) -> None:
    receipt_handle = str(message.get("ReceiptHandle", ""))
    payload = _parse_message_body(str(message.get("Body", "")))
    job_id = str(payload.get("job_id", "")).strip()
    user_id = str(payload.get("user_id", "")).strip()
    session_id = str(payload.get("session_id", user_id)).strip() or user_id
    prompt = str(payload.get("prompt", "")).strip()
    mode = str(payload.get("mode", "deep")).strip().lower() or "deep"
    debug_enabled = bool(payload.get("debug", False))

    if not job_id or not user_id or not prompt:
        if job_id:
            mark_job_failed(job_id, "Invalid async job payload.")
        delete_llm_job_message(receipt_handle)
        return

    existing = get_chat_job(job_id)
    if existing and str(existing.get("status", "")).strip().lower() == "completed":
        delete_llm_job_message(receipt_handle)
        return

    mark_job_processing(job_id)
    _safe_append_trace(
        job_id,
        {
            "type": "job_processing_started",
            "payload": {
                "job_id": job_id,
                "user_id": user_id,
                "session_id": session_id,
                "mode": mode,
                "debug": debug_enabled,
            },
        },
    )

    def _trace_callback(event: dict) -> None:
        _safe_append_trace(job_id, event)

    try:
        with trace_scope(_trace_callback):
            answer_payload = await generate_response(
                user_id,
                prompt,
                session_id=session_id,
                mode=mode,
                debug=debug_enabled,
            )
        debug_info = {}
        if isinstance(answer_payload, dict):
            answer = str(answer_payload.get("response", ""))
            payload_debug = answer_payload.get("debug")
            debug_info = payload_debug if isinstance(payload_debug, dict) else {}
        else:
            answer = str(answer_payload)
        mark_job_completed(job_id, answer, debug_info=debug_info if debug_enabled else None)
        _safe_append_trace(
            job_id,
            {
                "type": "job_completed",
                "payload": {
                    "job_id": job_id,
                    "answer_chars": len(str(answer)),
                },
            },
        )
        delete_llm_job_message(receipt_handle)
    except Exception as exc:
        # Intentionally do not delete SQS message on failure so queue retries can happen.
        mark_job_failed(job_id, str(exc))
        _safe_append_trace(
            job_id,
            {
                "type": "job_failed",
                "payload": {
                    "job_id": job_id,
                    "error": "Async chat job failed.",
                },
            },
        )
        logger.exception("AsyncLLMJobFailed | job_id=%s", job_id)


async def run_forever() -> None:
    if not settings.queue.llm_async_enabled:
        logger.warning("AsyncLLMWorkerDisabled | LLM_ASYNC_ENABLED=false")
    while True:
        if not settings.queue.llm_async_enabled:
            await asyncio.sleep(max(1.0, settings.queue.llm_poll_sleep_seconds))
            continue
        try:
            messages = await asyncio.to_thread(receive_llm_job_messages)
        except Exception:
            logger.exception("AsyncLLMWorkerPollFailed")
            await asyncio.sleep(max(1.0, settings.queue.llm_poll_sleep_seconds))
            continue

        if not messages:
            await asyncio.sleep(max(0.0, settings.queue.llm_poll_sleep_seconds))
            continue

        for message in messages:
            try:
                await _process_message(message)
            except Exception:
                logger.exception("AsyncLLMWorkerMessageProcessingFailed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    validate_security_configuration()
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        logger.info("AsyncLLMWorkerStopped")


if __name__ == "__main__":
    main()
