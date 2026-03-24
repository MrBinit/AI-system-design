import argparse
import getpass

from app.core.passwords import hash_password
from app.repositories.auth_user_repository import upsert_auth_user


def _parsed_roles(raw: str) -> list[str]:
    roles = [part.strip() for part in str(raw).split(",") if part.strip()]
    return roles or ["user"]


def _resolve_password(args) -> str:
    if args.password:
        return str(args.password)
    prompt_user = str(args.username).strip() or "user"
    return getpass.getpass(f"Password for {prompt_user}: ").strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update a Postgres-backed auth user.")
    parser.add_argument("--username", required=True, help="Login username.")
    parser.add_argument(
        "--user-id",
        default="",
        help="JWT subject/user id. Defaults to username.",
    )
    parser.add_argument(
        "--password", default="", help="Plaintext password (or leave empty for prompt)."
    )
    parser.add_argument(
        "--roles", default="admin", help='Comma-separated roles. Example: "admin,user"'
    )
    parser.add_argument(
        "--inactive",
        action="store_true",
        help="Mark user inactive (blocked login).",
    )
    args = parser.parse_args()

    username = str(args.username).strip()
    if not username:
        raise RuntimeError("username is required.")
    user_id = str(args.user_id).strip() or username
    password = _resolve_password(args)
    if not password:
        raise RuntimeError("password is required.")

    upsert_auth_user(
        username=username,
        user_id=user_id,
        password_hash=hash_password(password),
        roles=_parsed_roles(args.roles),
        is_active=not bool(args.inactive),
    )
    print(f"OK | upserted auth user | username={username} | user_id={user_id}")


if __name__ == "__main__":
    main()
