import asyncio
import hashlib
import ipaddress
import logging
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.security import decode_access_token
from app.infra.redis_client import app_redis_client, app_scoped_key

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


class _RedisFixedWindowLimiter:
    """Distributed fixed-window limiter backed by Redis counters."""

    def __init__(self, *, limit: int, window_seconds: int, key_prefix: str):
        self.limit = limit
        self.window_seconds = window_seconds
        self.key_prefix = key_prefix.strip(": ")

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.time()
        window_start = int(now // self.window_seconds) * self.window_seconds
        window_key = (
            f"{self.key_prefix}:{window_start}:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
        )

        count = int(app_redis_client.incr(window_key))
        if count == 1:
            app_redis_client.expire(window_key, self.window_seconds + 2)

        if count > self.limit:
            retry_after = max(1, int((window_start + self.window_seconds) - now))
            return False, retry_after
        return True, 0


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        limit: int,
        window_seconds: int,
        *,
        use_redis: bool = True,
        redis_key_prefix: str = "ratelimit",
        trusted_proxy_cidrs: list[str] | None = None,
    ):
        super().__init__(app)
        self._limiter = _InMemorySlidingWindowLimiter(limit=limit, window_seconds=window_seconds)
        self._trusted_proxy_networks = self._parse_trusted_proxy_networks(trusted_proxy_cidrs or [])
        self._redis_limiter = (
            _RedisFixedWindowLimiter(
                limit=limit,
                window_seconds=window_seconds,
                key_prefix=app_scoped_key(redis_key_prefix),
            )
            if use_redis
            else None
        )

    @staticmethod
    def _parse_trusted_proxy_networks(raw_values: list[str]) -> list:
        networks = []
        for raw in raw_values:
            candidate = str(raw).strip()
            if not candidate:
                continue
            try:
                if "/" in candidate:
                    networks.append(ipaddress.ip_network(candidate, strict=False))
                    continue
                parsed_ip = ipaddress.ip_address(candidate)
                networks.append(
                    ipaddress.ip_network(
                        f"{parsed_ip}/{parsed_ip.max_prefixlen}",
                        strict=False,
                    )
                )
            except ValueError:
                logger.warning("Ignoring invalid trusted proxy CIDR: %s", candidate)
        return networks

    def _is_trusted_proxy_peer(self, peer_host: str) -> bool:
        if not self._trusted_proxy_networks:
            return False
        try:
            peer_ip = ipaddress.ip_address(peer_host)
        except ValueError:
            return False
        return any(
            peer_ip.version == network.version and peer_ip in network
            for network in self._trusted_proxy_networks
        )

    def _client_ip(self, request) -> str:
        peer_host = request.client.host if request.client else "unknown"
        if not self._is_trusted_proxy_peer(peer_host):
            return peer_host

        forwarded = request.headers.get("x-forwarded-for", "")
        if not forwarded:
            return peer_host

        first = forwarded.split(",")[0].strip()
        if not first:
            return peer_host

        try:
            ipaddress.ip_address(first)
            return first
        except ValueError:
            logger.warning("Invalid x-forwarded-for value ignored: %s", first)
            return peer_host

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
        user_id = (
            getattr(request.state, "user_id", None) or self._token_user_id(request) or "anonymous"
        )
        client_ip = self._client_ip(request)
        return f"user:{user_id}|ip:{client_ip}|path:{request.url.path}"

    async def dispatch(self, request, call_next):
        key = self._rate_limit_key(request)
        allowed = None
        retry_after = 0

        if self._redis_limiter is not None:
            try:
                allowed, retry_after = await asyncio.to_thread(self._redis_limiter.allow, key)
            except Exception as exc:
                logger.warning(
                    "Distributed rate limit unavailable; falling back to local limiter. %s", exc
                )

        if allowed is None:
            allowed, retry_after = self._limiter.allow(key)

        if not allowed:
            logger.warning("RateLimitExceeded | key=%s retry_after=%s", key, retry_after)
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please retry later."},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)
