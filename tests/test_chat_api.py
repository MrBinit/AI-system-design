from fastapi.testclient import TestClient

from app.api.v1 import chat as chat_api
from app.api.v1 import ops as ops_api
from app.core.security import create_access_token
from app.main import app


def test_chat_endpoint_success(monkeypatch):
    def fake_enqueue_chat_job(*, user_id: str, prompt: str, session_id: str | None = None) -> dict:
        assert user_id == "user-1"
        assert prompt == "hello"
        assert session_id is None
        return {
            "job_id": "job-sync-removed-1234",
            "status": "queued",
            "submitted_at": "2026-03-10T00:00:00+00:00",
        }

    monkeypatch.setattr(chat_api, "enqueue_chat_job", fake_enqueue_chat_job)

    token = create_access_token(user_id="user-1", roles=["user"])
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"user_id": "user-1", "prompt": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "job_id": "job-sync-removed-1234",
        "status": "queued",
        "submitted_at": "2026-03-10T00:00:00+00:00",
    }


def test_chat_endpoint_requires_user_id():
    token = create_access_token(user_id="user-1", roles=["user"])
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"prompt": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_chat_endpoint_requires_auth():
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"user_id": "user-1", "prompt": "hello"},
    )
    assert response.status_code == 401


def test_chat_endpoint_forbidden_for_different_user():
    token = create_access_token(user_id="user-a", roles=["user"])
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"user_id": "user-b", "prompt": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_chat_endpoint_runtime_error_hides_internal_details(monkeypatch):
    def fake_enqueue_chat_job(*, user_id: str, prompt: str, session_id: str | None = None) -> dict:
        _ = user_id, prompt, session_id
        raise RuntimeError("LLM_QUEUE_URL is not configured.")

    monkeypatch.setattr(chat_api, "enqueue_chat_job", fake_enqueue_chat_job)

    token = create_access_token(user_id="user-1", roles=["user"])
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"user_id": "user-1", "prompt": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Async chat service is temporarily unavailable."


def test_chat_endpoint_error_hides_internal_details(monkeypatch):
    def fake_enqueue_chat_job(*, user_id: str, prompt: str, session_id: str | None = None) -> dict:
        _ = user_id, prompt, session_id
        raise ValueError("connection string leaked")

    monkeypatch.setattr(chat_api, "enqueue_chat_job", fake_enqueue_chat_job)

    token = create_access_token(user_id="user-1", roles=["user"])
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"user_id": "user-1", "prompt": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "Failed to enqueue async chat job."


def test_chat_async_enqueue_success(monkeypatch):
    def fake_enqueue_chat_job(*, user_id: str, prompt: str, session_id: str | None = None) -> dict:
        assert user_id == "user-1"
        assert prompt == "hello async"
        assert session_id is None
        return {
            "job_id": "job-12345678",
            "status": "queued",
            "submitted_at": "2026-03-10T00:00:00+00:00",
        }

    monkeypatch.setattr(chat_api, "enqueue_chat_job", fake_enqueue_chat_job)

    token = create_access_token(user_id="user-1", roles=["user"])
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"user_id": "user-1", "prompt": "hello async"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "job_id": "job-12345678",
        "status": "queued",
        "submitted_at": "2026-03-10T00:00:00+00:00",
    }


def test_chat_async_enqueue_uses_session_id_when_provided(monkeypatch):
    def fake_enqueue_chat_job(*, user_id: str, prompt: str, session_id: str | None = None) -> dict:
        assert user_id == "user-1"
        assert prompt == "hello async"
        assert session_id == "session-abc"
        return {
            "job_id": "job-12345678",
            "status": "queued",
            "submitted_at": "2026-03-10T00:00:00+00:00",
        }

    monkeypatch.setattr(chat_api, "enqueue_chat_job", fake_enqueue_chat_job)

    token = create_access_token(user_id="user-1", roles=["user"])
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"user_id": "user-1", "session_id": "session-abc", "prompt": "hello async"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202


def test_chat_async_status_requires_owner(monkeypatch):
    monkeypatch.setattr(
        chat_api,
        "get_chat_job",
        lambda _job_id: {
            "job_id": "job-abc",
            "user_id": "user-b",
            "session_id": "user-b",
            "status": "processing",
            "created_at": "2026-03-10T00:00:00+00:00",
            "started_at": "2026-03-10T00:00:02+00:00",
            "completed_at": "",
            "answer": "",
            "error": "",
        },
    )

    token = create_access_token(user_id="user-a", roles=["user"])
    client = TestClient(app)
    response = client.get(
        "/api/v1/chat/job-abc",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_chat_stream_endpoint_success(monkeypatch):
    async def fake_generate_response_stream(
        user_id: str,
        prompt: str,
        session_id: str | None = None,
    ):
        assert user_id == "user-1"
        assert prompt == "hello stream"
        assert session_id is None
        yield "hel"
        yield "hello"

    monkeypatch.setattr(chat_api, "generate_response_stream", fake_generate_response_stream)

    token = create_access_token(user_id="user-1", roles=["user"])
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat/stream",
        json={"user_id": "user-1", "prompt": "hello stream"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert '"type": "chunk"' in body
    assert '"text": "hel"' in body
    assert '"text": "hello"' in body
    assert '{"type":"done"}' in body


def test_chat_stream_endpoint_forwards_session_id(monkeypatch):
    async def fake_generate_response_stream(
        user_id: str,
        prompt: str,
        session_id: str | None = None,
    ):
        assert user_id == "user-1"
        assert prompt == "hello stream"
        assert session_id == "session-xyz"
        yield "hello"

    monkeypatch.setattr(chat_api, "generate_response_stream", fake_generate_response_stream)

    token = create_access_token(user_id="user-1", roles=["user"])
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat/stream",
        json={"user_id": "user-1", "session_id": "session-xyz", "prompt": "hello stream"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200


def test_chat_stream_endpoint_hides_internal_details(monkeypatch):
    async def fake_generate_response_stream(
        user_id: str,
        prompt: str,
        session_id: str | None = None,
    ):
        _ = user_id, prompt, session_id
        if session_id == "__never__":
            # Keep this function as an async generator for signature compatibility in tests.
            yield ""
        raise RuntimeError("provider timeout at host internal.example")

    monkeypatch.setattr(chat_api, "generate_response_stream", fake_generate_response_stream)

    token = create_access_token(user_id="user-1", roles=["user"])
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat/stream",
        json={"user_id": "user-1", "prompt": "hello stream"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert '"type": "error"' in response.text
    assert '"detail": "Failed to stream chat response."' in response.text
    assert "internal.example" not in response.text


def test_chat_stream_endpoint_forbidden_for_different_user():
    token = create_access_token(user_id="user-a", roles=["user"])
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat/stream",
        json={"user_id": "user-b", "prompt": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_chat_status_hides_internal_job_error(monkeypatch):
    internal_ip = ".".join(["10", "0", "0", "1"])
    monkeypatch.setattr(
        chat_api,
        "get_chat_job",
        lambda _job_id: {
            "job_id": "job-abc-1234",
            "user_id": "user-1",
            "session_id": "session-1",
            "status": "failed",
            "created_at": "2026-03-10T00:00:00+00:00",
            "started_at": "2026-03-10T00:00:02+00:00",
            "completed_at": "2026-03-10T00:00:03+00:00",
            "answer": "",
            "error": f"db host is {internal_ip}:5432",
        },
    )

    token = create_access_token(user_id="user-1", roles=["user"])
    client = TestClient(app)
    response = client.get(
        "/api/v1/chat/job-abc-1234",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["error"] == "Async chat job failed."
    assert internal_ip not in payload["error"]


def test_route_matching_middleware_formats_404():
    client = TestClient(app)
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "No route matched this path."
    assert body["path"] == "/api/v1/does-not-exist"


def test_ops_status_requires_admin(monkeypatch):
    monkeypatch.setattr(
        ops_api,
        "get_ops_status",
        lambda: {
            "status": "ok",
            "memory": {"redis_available": True, "ttl_seconds": 3600, "encryption_enabled": True},
            "queue": {
                "stream_depth": 0,
                "pending_jobs": 0,
                "dlq_depth": 0,
                "consumer_group": "memory-summary-workers",
                "last_dlq_error": "",
            },
            "compaction": {"events": 0, "removed_messages": 0, "removed_tokens": 0},
            "latency": {
                "count": 0,
                "pipeline_count": 0,
                "average_ms": 0.0,
                "average_build_context_ms": 0.0,
                "average_retrieval_ms": 0.0,
                "average_model_ms": 0.0,
                "max_ms": 0,
                "last_ms": 0,
                "last_build_context_ms": 0,
                "last_retrieval_ms": 0,
                "last_model_ms": 0,
                "last_retrieval_strategy": "",
                "last_retrieved_count": 0,
                "last_outcome": "",
            },
        },
    )

    token = create_access_token(user_id="user-1", roles=["user"])
    client = TestClient(app)
    response = client.get(
        "/api/v1/ops/status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
