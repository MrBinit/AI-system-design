from pydantic import BaseModel, Field


class MiddlewareConfig(BaseModel):
    timeout_seconds: int = Field(default=35, ge=1, le=120)
    max_in_flight_requests: int = Field(default=200, ge=1, le=5000)
    rate_limit_requests: int = Field(default=120, ge=1, le=100000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    enable_distributed_rate_limit: bool = True
    distributed_rate_limit_prefix: str = Field(default="ratelimit", min_length=1, max_length=64)
    trusted_proxy_cidrs: list[str] = Field(default_factory=list, max_length=256)
    enable_distributed_backpressure: bool = True
    distributed_backpressure_key: str = Field(default="backpressure:inflight", min_length=1, max_length=128)
    distributed_backpressure_lease_seconds: int = Field(default=45, ge=5, le=300)
    enable_request_logging: bool = True
    enable_rate_limit: bool = True
    enable_timeout: bool = True
    enable_backpressure: bool = True
    enable_route_matching: bool = True
