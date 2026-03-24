import logging

from fastapi import APIRouter, HTTPException, status

from app.core.config import get_settings
from app.core.passwords import verify_password
from app.core.security import create_access_token
from app.repositories.auth_user_repository import get_auth_user_by_username
from app.schemas.auth_schema import PasswordLoginRequest, PasswordLoginResponse

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()

_INVALID_CREDENTIALS_DETAIL = "Invalid username or password."


def _normalize_roles(raw_roles) -> list[str]:
    if not isinstance(raw_roles, list):
        return ["user"]
    normalized = [str(role).strip() for role in raw_roles if str(role).strip()]
    return normalized or ["user"]


def _normalize_user_id(user: dict) -> str:
    user_id = str(user.get("user_id", "")).strip()
    if user_id:
        return user_id
    return str(user.get("username", "")).strip()


def _normalize_user_roles(user: dict) -> list[str]:
    return _normalize_roles(user.get("roles"))


def _fetch_auth_user(username: str) -> dict | None:
    target = str(username).strip()
    if not target:
        return None
    if not settings.postgres.enabled:
        logger.warning("Postgres-backed login is configured but postgres.enabled=false.")
        return None
    try:
        return get_auth_user_by_username(target)
    except Exception as exc:
        logger.warning("Auth user lookup failed for username=%s. %s", target, exc)
        return None


@router.post("/auth/login", response_model=PasswordLoginResponse)
async def password_login(request: PasswordLoginRequest):
    """Authenticate a username/password pair and return a JWT bearer token."""
    user = _fetch_auth_user(request.username)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS_DETAIL,
        )

    if not bool(user.get("is_active", True)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS_DETAIL,
        )

    configured_password_hash = str(user.get("password_hash", "")).strip()
    if not configured_password_hash or not verify_password(
        request.password, configured_password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS_DETAIL,
        )

    roles = _normalize_user_roles(user)
    user_id = _normalize_user_id(user)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS_DETAIL,
        )
    token = create_access_token(user_id=user_id, roles=roles)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user_id,
        "roles": roles,
        "expires_in_seconds": int(settings.security.jwt_exp_minutes) * 60,
    }
