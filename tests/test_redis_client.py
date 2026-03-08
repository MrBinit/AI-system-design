import ssl
from types import SimpleNamespace

from app.infra import redis_client


def test_build_redis_client_without_tls(monkeypatch):
    captured_kwargs = {}

    def fake_redis(**kwargs):
        captured_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(redis_client.redis, "Redis", fake_redis)

    cfg = SimpleNamespace(
        host="localhost",
        port=6379,
        db=0,
        username="",
        password="",
        tls=False,
        ssl_cert_reqs="required",
        ssl_ca_certs="",
    )

    redis_client._build_redis_client(cfg)

    assert captured_kwargs["host"] == "localhost"
    assert captured_kwargs["port"] == 6379
    assert captured_kwargs["db"] == 0
    assert captured_kwargs["decode_responses"] is True
    assert "ssl" not in captured_kwargs
    assert "ssl_cert_reqs" not in captured_kwargs


def test_build_redis_client_with_tls(monkeypatch):
    captured_kwargs = {}

    def fake_redis(**kwargs):
        captured_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(redis_client.redis, "Redis", fake_redis)

    cfg = SimpleNamespace(
        host="cache.example.test",
        port=6379,
        db=0,
        username="app-user",
        password="secret",
        tls=True,
        ssl_cert_reqs="required",
        ssl_ca_certs="/etc/ssl/certs/ca-bundle.crt",
    )

    redis_client._build_redis_client(cfg)

    assert captured_kwargs["ssl"] is True
    assert captured_kwargs["ssl_cert_reqs"] == ssl.CERT_REQUIRED
    assert captured_kwargs["ssl_ca_certs"] == "/etc/ssl/certs/ca-bundle.crt"
    assert captured_kwargs["username"] == "app-user"
    assert captured_kwargs["password"] == "secret"
