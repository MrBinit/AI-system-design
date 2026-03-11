from datetime import datetime, timedelta, timezone

from app.services import offline_evaluation_service


class _FakeDynamoClient:
    def __init__(self):
        self.query_calls = []

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        if kwargs.get("ProjectionExpression") == "request_id":
            return {"Items": [{"request_id": {"S": "req-1"}}]}
        return {"Items": [{"timestamp": {"S": "2026-03-11T00:00:00+00:00"}}]}


def test_latest_timestamp_from_table_uses_status_index_query(monkeypatch):
    fake = _FakeDynamoClient()
    monkeypatch.setattr(offline_evaluation_service, "_dynamodb_client", lambda: fake)

    ts = offline_evaluation_service._latest_timestamp_from_table(
        "requests-table",
        index_name="eval-status-timestamp-index",
        status_attr="eval_status",
        status_value="pending",
    )

    assert ts == datetime.fromisoformat("2026-03-11T00:00:00+00:00")
    assert len(fake.query_calls) == 1
    query = fake.query_calls[0]
    assert query["IndexName"] == "eval-status-timestamp-index"
    assert query["KeyConditionExpression"] == "#status = :status_value"


def test_get_offline_eval_status_uses_pending_as_new_data(monkeypatch):
    now = datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(offline_evaluation_service, "_utc_now", lambda: now)
    monkeypatch.setattr(offline_evaluation_service.settings.evaluation, "enabled", True)
    monkeypatch.setattr(offline_evaluation_service.settings.evaluation, "schedule_enabled", True)
    monkeypatch.setattr(
        offline_evaluation_service.settings.evaluation, "schedule_interval_hours", 1
    )
    monkeypatch.setattr(
        offline_evaluation_service.settings.app,
        "metrics_dynamodb_requests_table",
        "requests-table",
    )
    monkeypatch.setattr(
        offline_evaluation_service.settings.evaluation, "dynamodb_table", "eval-table"
    )

    def _fake_latest(table_name: str, **kwargs):
        if table_name == "eval-table":
            return now - timedelta(hours=2)
        if (
            kwargs.get("status_value")
            == offline_evaluation_service.settings.evaluation.request_pending_value
        ):
            return now
        return now - timedelta(minutes=30)

    monkeypatch.setattr(offline_evaluation_service, "_latest_timestamp_from_table", _fake_latest)
    monkeypatch.setattr(offline_evaluation_service, "_new_requests_pending", lambda: True)

    status = offline_evaluation_service.get_offline_eval_status()

    assert status["has_new_requests"] is True
    assert status["due_by_interval"] is True
    assert status["should_auto_run"] is True
