import pytest
from pydantic import ValidationError

from app.schemas.redis_config_schema import RedisConfig, RedisRoleConfig


def test_redis_role_config_rejects_local_hosts():
    with pytest.raises(ValidationError, match="local Redis hosts are not allowed"):
        RedisConfig(
            app=RedisRoleConfig(host="redis", tls=True, ssl_cert_reqs="required"),
            worker=RedisRoleConfig(
                host="my-cache.serverless.use1.cache.amazonaws.com",
                tls=True,
                ssl_cert_reqs="required",
                namespace="worker",
            ),
        )


def test_redis_role_config_allows_local_tunnel_when_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("REDIS_LOCAL_TUNNEL_ENABLED", raising=False)
    cfg = RedisConfig(
        local_tunnel_enabled=True,
        app=RedisRoleConfig(host="host.docker.internal", tls=True, ssl_cert_reqs="required"),
        worker=RedisRoleConfig(
            host="host.docker.internal",
            tls=True,
            ssl_cert_reqs="required",
            namespace="worker",
        ),
    )
    assert cfg.app.host == "host.docker.internal"


def test_redis_role_config_allows_local_redis_without_tls():
    cfg = RedisConfig(
        allow_local_redis=True,
        app=RedisRoleConfig(host="redis", tls=False, ssl_cert_reqs="none"),
        worker=RedisRoleConfig(host="redis", tls=False, ssl_cert_reqs="none", namespace="worker"),
    )
    assert cfg.allow_local_redis is True


def test_redis_role_config_requires_elasticache_endpoint():
    with pytest.raises(ValidationError, match="must be an AWS ElastiCache endpoint"):
        RedisConfig(
            app=RedisRoleConfig(
                host="cache.example.internal",
                tls=True,
                ssl_cert_reqs="required",
            ),
            worker=RedisRoleConfig(
                host="my-cache.serverless.use1.cache.amazonaws.com",
                tls=True,
                ssl_cert_reqs="required",
                namespace="worker",
            ),
        )


def test_redis_role_config_requires_tls_and_required_cert_verification():
    with pytest.raises(ValidationError, match="TLS must be enabled"):
        RedisConfig(
            app=RedisRoleConfig(
                host="my-cache.serverless.use1.cache.amazonaws.com",
                tls=False,
            ),
            worker=RedisRoleConfig(
                host="my-cache-2.serverless.use1.cache.amazonaws.com",
                tls=True,
                ssl_cert_reqs="required",
                namespace="worker",
            ),
        )

    with pytest.raises(ValidationError, match="ssl_cert_reqs must be 'required'"):
        RedisConfig(
            app=RedisRoleConfig(
                host="my-cache.serverless.use1.cache.amazonaws.com",
                tls=True,
                ssl_cert_reqs="optional",
            ),
            worker=RedisRoleConfig(
                host="my-cache-2.serverless.use1.cache.amazonaws.com",
                tls=True,
                ssl_cert_reqs="required",
                namespace="worker",
            ),
        )


def test_redis_tunnel_mode_requires_tls_for_local_host(monkeypatch):
    monkeypatch.delenv("REDIS_LOCAL_TUNNEL_ENABLED", raising=False)
    with pytest.raises(ValidationError, match="tunnel mode"):
        RedisConfig(
            local_tunnel_enabled=True,
            app=RedisRoleConfig(host="host.docker.internal", tls=False, ssl_cert_reqs="none"),
            worker=RedisRoleConfig(
                host="host.docker.internal",
                tls=False,
                ssl_cert_reqs="none",
                namespace="worker",
            ),
        )


def test_redis_role_config_accepts_valid_elasticache_config():
    cfg = RedisConfig(
        app=RedisRoleConfig(
            host="my-cache.serverless.use1.cache.amazonaws.com",
            tls=True,
            ssl_cert_reqs="required",
        ),
        worker=RedisRoleConfig(
            host="my-cache-2.serverless.use1.cache.amazonaws.com",
            tls=True,
            ssl_cert_reqs="required",
            namespace="worker",
        ),
    )
    assert cfg.app.host == "my-cache.serverless.use1.cache.amazonaws.com"
