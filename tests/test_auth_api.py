import json

from fastapi.testclient import TestClient

from app.core.security import decode_access_token
from app.main import app


def test_password_login_default_credentials(monkeypatch):
    monkeypatch.delenv("SECURITY_LOGIN_USERS_JSON", raising=False)
    client = TestClient(app)

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "bearer"
    assert payload["user_id"] == "admin"
    claims = decode_access_token(payload["access_token"])
    assert claims["sub"] == "admin"


def test_password_login_rejects_invalid_credentials():
    client = TestClient(app)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid username or password."


def test_password_login_uses_env_users(monkeypatch):
    monkeypatch.setenv(
        "SECURITY_LOGIN_USERS_JSON",
        json.dumps(
            [
                {
                    "username": "alice",
                    "password": "alice-pass",
                    "user_id": "alice@example.com",
                    "roles": ["user", "admin"],
                }
            ]
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "alice-pass"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == "alice@example.com"
    assert payload["roles"] == ["user", "admin"]
