import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from app.core.config import get_settings

settings = get_settings()


class DependencyBackpressureError(RuntimeError):
    """Raised when a dependency semaphore cannot be acquired fast enough."""

    def __init__(self, dependency: str, retry_after_seconds: float):
        self.dependency = dependency
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        super().__init__(f"Dependency limiter busy: {dependency}")


def _safe_limit(value, *, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return max(1, int(fallback))
    return max(1, parsed)


_LLM_ANSWER_LIMIT = _safe_limit(
    getattr(settings.io, "llm_answer_max_concurrency", None),
    fallback=settings.io.llm_max_concurrency,
)
_LLM_PLANNER_LIMIT = _safe_limit(
    getattr(settings.io, "llm_planner_max_concurrency", None),
    fallback=max(1, _LLM_ANSWER_LIMIT // 3),
)
_LLM_ANSWER_SEMAPHORE = asyncio.Semaphore(_LLM_ANSWER_LIMIT)

_LIMITS: dict[str, asyncio.Semaphore] = {
    "llm": _LLM_ANSWER_SEMAPHORE,
    "llm_answer": _LLM_ANSWER_SEMAPHORE,
    "llm_planner": asyncio.Semaphore(_LLM_PLANNER_LIMIT),
    "embedding": asyncio.Semaphore(settings.io.embedding_max_concurrency),
    "retrieval": asyncio.Semaphore(settings.io.retrieval_max_concurrency),
    "reranker": asyncio.Semaphore(settings.io.reranker_max_concurrency),
    "redis": asyncio.Semaphore(settings.io.redis_max_concurrency),
    "web_search": asyncio.Semaphore(settings.web_search.max_concurrency),
}


@asynccontextmanager
async def _acquire_with_timeout(
    semaphore: asyncio.Semaphore,
    *,
    dependency: str,
    acquire_timeout_seconds: float,
) -> AsyncIterator[None]:
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=acquire_timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise DependencyBackpressureError(dependency, acquire_timeout_seconds) from exc
    try:
        yield
    finally:
        semaphore.release()


@asynccontextmanager
async def dependency_limiter(
    name: str,
    *,
    acquire_timeout_seconds: float | None = None,
) -> AsyncIterator[None]:
    """Acquire the configured semaphore for one downstream dependency."""
    semaphore = _LIMITS.get(name)
    if semaphore is None:
        raise ValueError(f"Unknown dependency limiter: {name}")
    timeout_seconds = float(acquire_timeout_seconds or 0.0)
    if timeout_seconds > 0:
        async with _acquire_with_timeout(
            semaphore,
            dependency=name,
            acquire_timeout_seconds=timeout_seconds,
        ):
            yield
        return

    async with semaphore:
        yield
