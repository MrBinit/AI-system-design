from __future__ import annotations

from threading import Lock
from typing import Any

import pybreaker

from app.core.config import get_settings

settings = get_settings()

_BREAKERS: dict[str, pybreaker.CircuitBreaker] = {}
_BREAKERS_LOCK = Lock()


def _new_breaker(name: str) -> pybreaker.CircuitBreaker:
    """Create one configured circuit breaker instance."""
    return pybreaker.CircuitBreaker(
        fail_max=settings.circuit.fail_max,
        reset_timeout=settings.circuit.reset_timeout,
        name=name,
    )


def get_breaker(name: str) -> pybreaker.CircuitBreaker:
    """Return a stable named circuit breaker instance."""
    normalized = str(name or "").strip() or "default"
    with _BREAKERS_LOCK:
        existing = _BREAKERS.get(normalized)
        if existing is not None:
            return existing
        created = _new_breaker(normalized)
        _BREAKERS[normalized] = created
        return created


def get_llm_breaker(model_id: str) -> pybreaker.CircuitBreaker:
    """Return a per-model breaker for LLM generation calls."""
    normalized_model = str(model_id or "").strip() or "default"
    return get_breaker(f"llm:{normalized_model}")


def get_embedding_breaker() -> pybreaker.CircuitBreaker:
    """Return the breaker used for embedding model calls."""
    return get_breaker("embedding")


def reset_all_breakers() -> None:
    """Reset all registered breakers (primarily for tests)."""
    with _BREAKERS_LOCK:
        for instance in _BREAKERS.values():
            instance.close()


breaker = get_breaker("default")
CircuitBreakerError: Any = pybreaker.CircuitBreakerError
