from fastapi.testclient import TestClient

from app.core.passwords import hash_password
from app.main import app
from app.api.v1 import auth as auth_api


def test_password_login_rejects_when_user_not_found(monkeypatch):
    monkeypatch.setattr(auth_api, "_fetch_auth_user", lambda _username: None)
    client = TestClient(app)

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "missing", "password": "admin"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid username or password."


def test_password_login_rejects_invalid_credentials(monkeypatch):
    user = {
        "username": "admin",
        "user_id": "admin",
        "password_hash": hash_password("correct-password"),
        "roles": ["admin"],
        "is_active": True,
    }
    monkeypatch.setattr(auth_api, "_fetch_auth_user", lambda _username: user)
    client = TestClient(app)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid username or password."


def test_password_login_uses_postgres_user_record(monkeypatch):
    user = {
        "username": "alice",
        "user_id": "alice@example.com",
        "password_hash": hash_password("alice-pass"),
        "roles": ["user", "admin"],
        "is_active": True,
    }
    monkeypatch.setattr(
        auth_api,
        "_fetch_auth_user",
        lambda username: user if username == "alice" else None,
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


def test_password_login_rejects_inactive_user(monkeypatch):
    user = {
        "username": "alice",
        "user_id": "alice@example.com",
        "password_hash": hash_password("alice-pass"),
        "roles": ["admin"],
        "is_active": False,
    }
    monkeypatch.setattr(auth_api, "_fetch_auth_user", lambda _username: user)
    client = TestClient(app)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "alice-pass"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid username or password."
