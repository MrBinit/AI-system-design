from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Callable

_TRACE_CALLBACK: ContextVar[Callable[[dict], None] | None] = ContextVar(
    "chat_trace_callback",
    default=None,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_event(event_type: str, payload: dict) -> dict:
    safe_payload = payload if isinstance(payload, dict) else {}
    return {
        "type": str(event_type).strip() or "event",
        "timestamp": _now_iso(),
        "payload": safe_payload,
    }


@contextmanager
def trace_scope(callback: Callable[[dict], None] | None):
    token = _TRACE_CALLBACK.set(callback)
    try:
        yield
    finally:
        _TRACE_CALLBACK.reset(token)


def emit_trace_event(event_type: str, payload: dict | None = None, **kwargs) -> None:
    callback = _TRACE_CALLBACK.get()
    if callback is None:
        return

    merged_payload = dict(payload or {})
    merged_payload.update(kwargs)
    event = _sanitize_event(event_type, merged_payload)
    try:
        callback(event)
    except Exception:
        return
