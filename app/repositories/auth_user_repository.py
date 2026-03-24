import json

from app.core.config import get_settings
from app.infra.postgres_client import get_postgres_pool

settings = get_settings()


def _table(name: str) -> str:
    schema = str(settings.postgres.schema_name).strip()
    if not schema:
        return name
    return f"{schema}.{name}"


def _normalized_roles(roles) -> list[str]:
    if not isinstance(roles, list):
        return ["user"]
    normalized = [str(role).strip() for role in roles if str(role).strip()]
    return normalized or ["user"]


def ensure_auth_user_table() -> None:
    sql = f"""
        CREATE TABLE IF NOT EXISTS {_table("auth_users")} (
            username_key TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            user_id TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            roles JSONB NOT NULL DEFAULT '["user"]'::jsonb,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """
    pool = get_postgres_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def get_auth_user_by_username(username: str) -> dict | None:
    normalized_username = str(username).strip()
    if not normalized_username:
        return None
    username_key = normalized_username.lower()
    ensure_auth_user_table()
    pool = get_postgres_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT username, user_id, password_hash, roles, is_active
                FROM {_table("auth_users")}
                WHERE username_key = %s
                LIMIT 1
                """,
                (username_key,),
            )
            row = cur.fetchone()
    if not isinstance(row, dict):
        return None
    return row


def upsert_auth_user(
    *,
    username: str,
    user_id: str,
    password_hash: str,
    roles: list[str] | None = None,
    is_active: bool = True,
) -> None:
    normalized_username = str(username).strip()
    normalized_user_id = str(user_id).strip()
    normalized_password_hash = str(password_hash).strip()
    if not normalized_username:
        raise ValueError("username is required.")
    if not normalized_user_id:
        raise ValueError("user_id is required.")
    if not normalized_password_hash:
        raise ValueError("password_hash is required.")

    username_key = normalized_username.lower()
    roles_json = json.dumps(_normalized_roles(roles or ["user"]), ensure_ascii=False)

    ensure_auth_user_table()
    pool = get_postgres_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {_table("auth_users")} (
                    username_key, username, user_id, password_hash, roles, is_active
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (username_key) DO UPDATE SET
                    username = EXCLUDED.username,
                    user_id = EXCLUDED.user_id,
                    password_hash = EXCLUDED.password_hash,
                    roles = EXCLUDED.roles,
                    is_active = EXCLUDED.is_active,
                    updated_at = now()
                """,
                (
                    username_key,
                    normalized_username,
                    normalized_user_id,
                    normalized_password_hash,
                    roles_json,
                    bool(is_active),
                ),
            )
        conn.commit()
