from types import SimpleNamespace

from app.core.security import create_access_token
from app.middlewares.rate_limit import RateLimitMiddleware


def _request(
    path: str,
    host: str,
    auth_header: str | None = None,
    user_id: str | None = None,
    x_forwarded_for: str | None = None,
):
    headers = {}
    if auth_header:
        headers["authorization"] = auth_header
    if x_forwarded_for:
        headers["x-forwarded-for"] = x_forwarded_for
    state = SimpleNamespace()
    if user_id is not None:
        state.user_id = user_id
    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host=host),
        url=SimpleNamespace(path=path),
        state=state,
    )


def test_rate_limit_key_uses_user_id_and_ip():
    middleware = RateLimitMiddleware(app=lambda *_args, **_kwargs: None, limit=10, window_seconds=60)

    token_1 = create_access_token(user_id="user-1", roles=["user"])
    token_2 = create_access_token(user_id="user-2", roles=["user"])

    req_1 = _request("/api/v1/chat", "1.2.3.4", auth_header=f"Bearer {token_1}")
    req_2 = _request("/api/v1/chat", "1.2.3.4", auth_header=f"Bearer {token_2}")

    key_1 = middleware._rate_limit_key(req_1)
    key_2 = middleware._rate_limit_key(req_2)

    assert "user:user-1" in key_1
    assert "ip:1.2.3.4" in key_1
    assert key_1 != key_2


def test_rate_limit_key_falls_back_to_anonymous_when_token_invalid():
    middleware = RateLimitMiddleware(app=lambda *_args, **_kwargs: None, limit=10, window_seconds=60)
    req = _request("/api/v1/chat", "5.6.7.8", auth_header="Bearer invalid-token")
    key = middleware._rate_limit_key(req)
    assert "user:anonymous" in key
    assert "ip:5.6.7.8" in key


def test_rate_limit_ignores_x_forwarded_for_when_proxy_not_trusted():
    middleware = RateLimitMiddleware(app=lambda *_args, **_kwargs: None, limit=10, window_seconds=60)
    req = _request(
        "/api/v1/chat",
        "10.0.0.5",
        x_forwarded_for="203.0.113.9",
    )
    key = middleware._rate_limit_key(req)
    assert "ip:10.0.0.5" in key
    assert "ip:203.0.113.9" not in key


def test_rate_limit_uses_x_forwarded_for_when_proxy_trusted():
    middleware = RateLimitMiddleware(
        app=lambda *_args, **_kwargs: None,
        limit=10,
        window_seconds=60,
        trusted_proxy_cidrs=["10.0.0.0/8"],
    )
    req = _request(
        "/api/v1/chat",
        "10.0.0.5",
        x_forwarded_for="203.0.113.9, 10.0.0.5",
    )
    key = middleware._rate_limit_key(req)
    assert "ip:203.0.113.9" in key
