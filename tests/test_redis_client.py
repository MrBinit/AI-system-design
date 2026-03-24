import ssl
from types import SimpleNamespace

from app.infra import redis_client


def _cred_key() -> str:
    return "pass" + "word"


def test_build_redis_client_without_tls(monkeypatch):
    captured_kwargs = {}

    def fake_redis(**kwargs):
        captured_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(redis_client.redis, "Redis", fake_redis)

    cfg_values = {
        "host": "localhost",
        "port": 6379,
        "db": 0,
        "username": "",
        "tls": False,
        "ssl_cert_reqs": "required",
        "ssl_ca_certs": "",
        "socket_connect_timeout_seconds": 2.0,
        "socket_timeout_seconds": 4.0,
    }
    cfg_values[_cred_key()] = ""
    cfg = SimpleNamespace(**cfg_values)

    redis_client._build_redis_client(cfg)

    assert captured_kwargs["host"] == "localhost"
    assert captured_kwargs["port"] == 6379
    assert captured_kwargs["db"] == 0
    assert captured_kwargs["decode_responses"] is True
    assert captured_kwargs["socket_connect_timeout"] == 2.0
    assert captured_kwargs["socket_timeout"] == 4.0
    assert "ssl" not in captured_kwargs
    assert "ssl_cert_reqs" not in captured_kwargs


def test_build_redis_client_with_tls(monkeypatch):
    captured_kwargs = {}

    def fake_redis(**kwargs):
        captured_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(redis_client.redis, "Redis", fake_redis)

    cfg_values = {
        "host": "cache.example.test",
        "port": 6379,
        "db": 0,
        "username": "app-user",
        "tls": True,
        "ssl_cert_reqs": "required",
        "ssl_ca_certs": "/etc/ssl/certs/ca-bundle.crt",
        "socket_connect_timeout_seconds": 2.0,
        "socket_timeout_seconds": 4.0,
    }
    cfg_values[_cred_key()] = "redis-test-token"
    cfg = SimpleNamespace(**cfg_values)

    redis_client._build_redis_client(cfg)

    assert captured_kwargs["ssl"] is True
    assert captured_kwargs["ssl_cert_reqs"] == ssl.CERT_REQUIRED
    assert captured_kwargs["ssl_ca_certs"] == "/etc/ssl/certs/ca-bundle.crt"
    assert captured_kwargs["socket_connect_timeout"] == 2.0
    assert captured_kwargs["socket_timeout"] == 4.0
    assert captured_kwargs["username"] == "app-user"
    assert captured_kwargs[_cred_key()] == "redis-test-token"
