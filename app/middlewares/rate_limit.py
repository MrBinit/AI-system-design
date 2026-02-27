import logging
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.security import decode_access_token

logger = logging.getLogger(__name__)


class _InMemorySlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self._events = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.time()
        with self._lock:
            bucket = self._events[key]
            cutoff = now - self.window_seconds
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            if len(bucket) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - bucket[0])))
                return False, retry_after

            bucket.append(now)
            return True, 0


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limit: int, window_seconds: int):
        super().__init__(app)
        self._limiter = _InMemorySlidingWindowLimiter(limit=limit, window_seconds=window_seconds)

    @staticmethod
    def _client_ip(request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
        host = request.client.host if request.client else "unknown"
        return host

    @staticmethod
    def _token_user_id(request) -> str | None:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return None
        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            return None
        try:
            payload = decode_access_token(token)
        except Exception:
            return None
        sub = payload.get("sub")
        return sub if isinstance(sub, str) and sub else None

    def _rate_limit_key(self, request) -> str:
        user_id = getattr(request.state, "user_id", None) or self._token_user_id(request) or "anonymous"
        client_ip = self._client_ip(request)
        return f"user:{user_id}|ip:{client_ip}|path:{request.url.path}"

    async def dispatch(self, request, call_next):
        key = self._rate_limit_key(request)
        allowed, retry_after = self._limiter.allow(key)
        if not allowed:
            logger.warning("RateLimitExceeded | key=%s retry_after=%s", key, retry_after)
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please retry later."},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)
