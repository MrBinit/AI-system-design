import os
from typing import Literal

from pydantic import BaseModel, Field, model_validator

_LOCAL_REDIS_HOSTS = {"localhost", "127.0.0.1", "::1", "redis", "host.docker.internal"}


def _redis_local_tunnel_enabled_from_env() -> bool:
    value = str(os.getenv("REDIS_LOCAL_TUNNEL_ENABLED", "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


class RedisRoleConfig(BaseModel):
    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0, le=15)
    username: str = ""
    password: str = ""
    tls: bool = False
    ssl_cert_reqs: Literal["required", "optional", "none"] = "required"
    ssl_ca_certs: str = ""
    socket_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=60)
    socket_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    namespace: str = Field(min_length=1, default="app")


class RedisConfig(BaseModel):
    allow_local_redis: bool = False
    local_tunnel_enabled: bool = False
    tunnel_local_port: int = Field(default=6380, ge=1, le=65535)
    tunnel_instance_id: str = ""
    app: RedisRoleConfig = Field(default_factory=RedisRoleConfig)
    worker: RedisRoleConfig = Field(default_factory=lambda: RedisRoleConfig(namespace="worker"))

    @model_validator(mode="after")
    def validate_hosts(self):
        """Require ElastiCache hosts unless local-tunnel mode is enabled."""
        local_tunnel_enabled = self.local_tunnel_enabled or _redis_local_tunnel_enabled_from_env()
        allow_local_hosts = self.allow_local_redis or local_tunnel_enabled
        for role in (self.app, self.worker):
            host = role.host.strip().lower()
            if not host:
                raise ValueError("redis host is required.")
            if host in _LOCAL_REDIS_HOSTS:
                if not allow_local_hosts:
                    raise ValueError(
                        "redis host must point to AWS ElastiCache; local Redis hosts are not allowed."
                    )
                if local_tunnel_enabled:
                    if not role.tls:
                        raise ValueError("redis TLS must be enabled for ElastiCache tunnel mode.")
                    if role.ssl_cert_reqs != "required":
                        raise ValueError(
                            "redis ssl_cert_reqs must be 'required' for ElastiCache tunnel mode."
                        )
                continue
            if "cache.amazonaws.com" not in host:
                raise ValueError(
                    "redis host must be an AWS ElastiCache endpoint (*.cache.amazonaws.com)."
                )
            if not role.tls:
                raise ValueError("redis TLS must be enabled for ElastiCache.")
            if role.ssl_cert_reqs != "required":
                raise ValueError("redis ssl_cert_reqs must be 'required' for ElastiCache.")
        return self
