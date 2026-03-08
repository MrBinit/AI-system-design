import os
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from app.core.config import get_settings

settings = get_settings()
_MIN_SECRET_LENGTH = 32
_DISALLOWED_DEFAULT_SECRETS = {
    "change-this-in-prod-very-secret-key",
    "changeme",
    "default",
}


def _configured_jwt_secret() -> str:
    """Resolve JWT secret from env overrides first, then settings."""
    for env_name in ("SECURITY_JWT_SECRET", "JWT_SECRET"):
        raw = os.getenv(env_name, "").strip()
        if raw:
            return raw
    return settings.security.jwt_secret.strip()


def _ensure_strong_secret(secret: str, label: str):
    """Validate a secret value has enough entropy and is not a known placeholder."""
    normalized = secret.strip()
    if len(normalized) < _MIN_SECRET_LENGTH:
        raise RuntimeError(f"{label} must be at least {_MIN_SECRET_LENGTH} characters.")
    if normalized.lower() in _DISALLOWED_DEFAULT_SECRETS:
        raise RuntimeError(f"{label} uses an insecure default placeholder value.")


def validate_security_configuration():
    """Fail fast when required production security secrets are missing or weak."""
    jwt_secret = _configured_jwt_secret()
    if settings.security.auth_enabled:
        _ensure_strong_secret(jwt_secret, "SECURITY_JWT_SECRET")

    memory_key = os.getenv("MEMORY_ENCRYPTION_KEY", "").strip()
    if not memory_key:
        raise RuntimeError("MEMORY_ENCRYPTION_KEY is required and must be set in the environment.")
    _ensure_strong_secret(memory_key, "MEMORY_ENCRYPTION_KEY")
    if memory_key == jwt_secret:
        raise RuntimeError("MEMORY_ENCRYPTION_KEY must be different from SECURITY_JWT_SECRET.")


def _secret_key() -> str:
    """Return the JWT signing key, preferring the environment override."""
    return _configured_jwt_secret()


def create_access_token(
    *,
    user_id: str,
    roles: list[str] | None = None,
    expires_minutes: int | None = None,
) -> str:
    """Create a signed JWT access token for the given user and roles."""
    now = datetime.now(timezone.utc)
    exp_minutes = expires_minutes or settings.security.jwt_exp_minutes
    payload = {
        "sub": user_id,
        "roles": roles or ["user"],
        "iss": settings.security.jwt_issuer,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=exp_minutes)).timestamp()),
    }
    return jwt.encode(payload, _secret_key(), algorithm=settings.security.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT access token."""
    return jwt.decode(
        token,
        _secret_key(),
        algorithms=[settings.security.jwt_algorithm],
        issuer=settings.security.jwt_issuer,
        options={"verify_aud": False},
    )


def is_jwt_error(exc: Exception) -> bool:
    """Report whether an exception is a JWT parsing or validation error."""
    return isinstance(exc, JWTError)
