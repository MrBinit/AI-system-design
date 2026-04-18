import asyncio
import os
import random
import time
from typing import Any

from app.core.config import get_settings
from app.infra.io_limiters import dependency_limiter

settings = get_settings()


def _require_web_search_enabled():
    if not settings.web_search.enabled:
        raise RuntimeError("Web search is disabled. Set web_search.enabled=true in config.")


def _api_key() -> tuple[str, str]:
    configured_env_name = str(settings.web_search.api_key_env_name).strip()
    candidate_names = [
        configured_env_name,
        "TAVILY_WEB_SEARCH",
        "WEB_SEARCH_API_KEY",
    ]
    for env_name in candidate_names:
        if not env_name:
            continue
        value = os.getenv(env_name, "").strip()
        if value:
            return env_name, value
    configured = configured_env_name or "TAVILY_WEB_SEARCH"
    raise RuntimeError(f"{configured} is required for Tavily web search requests.")


def _retry_attempts() -> int:
    configured = int(getattr(settings.web_search, "retry_max_attempts", 3) or 3)
    return max(1, min(6, configured))


def _retry_base_backoff_seconds() -> float:
    configured = float(getattr(settings.web_search, "retry_base_backoff_seconds", 0.8) or 0.8)
    return max(0.1, min(5.0, configured))


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code in {429, 500, 502, 503, 504}


def _sleep_for_retry(attempt: int) -> None:
    base = _retry_base_backoff_seconds()
    delay = (base * (2 ** max(0, attempt - 1))) + random.uniform(0.0, 0.25)
    time.sleep(min(delay, 8.0))


def _normalize_tavily_payload(raw: dict[str, Any], *, query: str) -> dict:
    results = raw.get("results", [])
    if not isinstance(results, list):
        results = []
    organic_results: list[dict[str, str]] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        organic_results.append(
            {
                "title": str(row.get("title", "")).strip(),
                "link": str(row.get("url", "")).strip(),
                "snippet": str(row.get("content", "")).strip(),
                "date": str(row.get("published_date", "")).strip(),
            }
        )
    answer = str(raw.get("answer", "")).strip()
    payload: dict[str, Any] = {
        "search_parameters": {"q": query},
        "organic_results": organic_results,
        "ai_overview": {"text": answer} if answer else {},
    }
    return payload


def _search_tavily_sync(
    query: str,
    *,
    num: int,
    search_depth: str = "advanced",
) -> dict:
    try:
        from tavily import TavilyClient
    except Exception as exc:  # pragma: no cover - import failure path
        raise RuntimeError(
            "tavily-python is required. Install with: pip install tavily-python"
        ) from exc

    _, key = _api_key()
    client = TavilyClient(key)
    response = client.search(
        query=query,
        search_depth=search_depth,
        max_results=max(1, int(num)),
    )
    if not isinstance(response, dict):
        raise RuntimeError("Tavily response must be a JSON object.")
    return response


def _request_json(
    query: str,
    *,
    timeout_seconds: float,
    num: int,
    search_depth: str = "advanced",
) -> dict:
    attempts = _retry_attempts()
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            payload = _search_tavily_sync(
                query=query,
                num=num,
                search_depth=search_depth,
            )
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                _sleep_for_retry(attempt)
                continue
            raise

    if last_error is not None:
        raise RuntimeError(f"Tavily request failed after retries: {last_error}")
    raise RuntimeError("Tavily request failed after retries.")


def _search_google_sync(
    query: str,
    *,
    gl: str | None = None,
    hl: str | None = None,
    num: int | None = None,
    search_depth: str | None = None,
) -> dict:
    trimmed_query = str(query).strip()
    if not trimmed_query:
        raise ValueError("query must be non-empty.")
    max_results = max(1, int(num or settings.web_search.default_num))
    configured_depth = str(getattr(settings.web_search, "search_depth", "advanced")).strip().lower()
    selected_depth = str(search_depth or configured_depth).strip().lower()
    selected_depth = selected_depth if selected_depth in {"basic", "advanced"} else "advanced"
    raw_payload = _request_json(
        trimmed_query,
        timeout_seconds=float(settings.web_search.timeout_seconds),
        num=max_results,
        search_depth=selected_depth,
    )
    return _normalize_tavily_payload(raw_payload, query=trimmed_query)


def search_google(
    query: str,
    *,
    gl: str | None = None,
    hl: str | None = None,
    num: int | None = None,
    search_depth: str | None = None,
) -> dict:
    """Run one Tavily web search request."""
    _require_web_search_enabled()
    return _search_google_sync(query, gl=gl, hl=hl, num=num, search_depth=search_depth)


async def asearch_google(
    query: str,
    *,
    gl: str | None = None,
    hl: str | None = None,
    num: int | None = None,
    search_depth: str | None = None,
) -> dict:
    """Run one Tavily web search request asynchronously."""
    _require_web_search_enabled()
    timeout_seconds = max(2.0, float(settings.web_search.timeout_seconds))
    async with dependency_limiter("web_search"):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    _search_google_sync,
                    query,
                    gl=gl,
                    hl=hl,
                    num=num,
                    search_depth=search_depth,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Tavily request timed out after {timeout_seconds:.1f}s."
            ) from exc


def _normalized_queries(queries: list[str]) -> list[str]:
    return [query.strip() for query in queries if isinstance(query, str) and query.strip()]


async def asearch_google_batch(
    queries: list[str],
    *,
    gl: str | None = None,
    hl: str | None = None,
    num: int | None = None,
    search_depth: str | None = None,
) -> list[dict]:
    """Run many Tavily web-search requests with an internal async work queue."""
    _require_web_search_enabled()
    normalized = _normalized_queries(queries)
    if not normalized:
        return []

    results: list[dict] = [{"query": query, "result": {}, "error": ""} for query in normalized]
    queue: asyncio.Queue = asyncio.Queue(maxsize=settings.web_search.queue_max_size)
    worker_count = min(settings.web_search.queue_workers, len(normalized))

    async def _worker():
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return

                index, query_value = item
                try:
                    payload = await asearch_google(
                        query_value,
                        gl=gl,
                        hl=hl,
                        num=num,
                        search_depth=search_depth,
                    )
                    results[index] = {"query": query_value, "result": payload, "error": ""}
                except Exception as exc:
                    results[index] = {"query": query_value, "result": {}, "error": str(exc)}
            finally:
                queue.task_done()

    workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]

    for index, query_value in enumerate(normalized):
        await queue.put((index, query_value))
    for _ in range(worker_count):
        await queue.put(None)

    await queue.join()
    await asyncio.gather(*workers)
    return results
