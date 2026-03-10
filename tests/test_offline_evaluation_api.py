from fastapi.testclient import TestClient

from app.api.v1 import evaluation as evaluation_api
from app.core.security import create_access_token
from app.main import app


def test_offline_eval_status_requires_admin():
    token = create_access_token(user_id="user-1", roles=["user"])
    client = TestClient(app)
    response = client.get(
        "/api/v1/eval/offline/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_offline_eval_status_success_admin(monkeypatch):
    monkeypatch.setattr(
        evaluation_api,
        "get_offline_eval_status",
        lambda: {
            "enabled": True,
            "schedule_enabled": True,
            "interval_hours": 24,
            "has_new_requests": True,
            "due_by_interval": True,
            "should_auto_run": True,
            "last_request_timestamp": "2026-03-10T00:00:00+00:00",
            "last_evaluated_timestamp": "2026-03-09T00:00:00+00:00",
            "reason": "ok",
        },
    )
    token = create_access_token(user_id="admin-1", roles=["admin"])
    client = TestClient(app)
    response = client.get(
        "/api/v1/eval/offline/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["should_auto_run"] is True


def test_offline_eval_run_force(monkeypatch):
    async def fake_run_offline_eval(limit: int | None = None, force: bool = False):
        return {
            "ran": True,
            "reason": "ok",
            "result": {"evaluated": 2, "skipped": 1},
            "status": {
                "enabled": True,
                "schedule_enabled": True,
                "interval_hours": 24,
                "has_new_requests": False,
                "due_by_interval": False,
                "should_auto_run": False,
                "last_request_timestamp": "",
                "last_evaluated_timestamp": "",
                "reason": "ok",
            },
        }

    monkeypatch.setattr(evaluation_api, "run_offline_eval", fake_run_offline_eval)
    token = create_access_token(user_id="admin-1", roles=["admin"])
    client = TestClient(app)
    response = client.post(
        "/api/v1/eval/offline/run?force=true&limit=10",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ran"] is True
    assert body["result"]["evaluated"] == 2


def test_offline_eval_report(monkeypatch):
    monkeypatch.setattr(
        evaluation_api,
        "build_offline_eval_report",
        lambda hours, top_bad: {
            "generated_at": "2026-03-10T00:00:00+00:00",
            "window_hours": hours,
            "evaluated_count": 5,
            "scores": {"overall_p50": 0.71, "overall_p95": 0.91},
            "failure_reasons": {"none": 4, "irrelevant": 1},
            "top_bad_examples": [],
        },
    )
    token = create_access_token(user_id="admin-1", roles=["admin"])
    client = TestClient(app)
    response = client.get(
        "/api/v1/eval/offline/report?hours=24&top_bad=5",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["evaluated_count"] == 5
    assert body["window_hours"] == 24
