from fastapi.testclient import TestClient

from app.api.v1 import chat as chat_api
from app.main import app


def test_chat_endpoint_success(monkeypatch):
    async def fake_generate_response(user_id: str, user_prompt: str) -> str:
        return f"{user_id}:{user_prompt}"

    monkeypatch.setattr(chat_api, "generate_response", fake_generate_response)

    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"user_id": "user-1", "prompt": "hello"},
    )

    assert response.status_code == 200
    assert response.json() == {"response": "user-1:hello"}


def test_chat_endpoint_requires_user_id():
    client = TestClient(app)
    response = client.post("/api/v1/chat", json={"prompt": "hello"})
    assert response.status_code == 422
