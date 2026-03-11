from app.services import sqs_event_queue_service


def test_enqueue_metrics_record_event_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(
        sqs_event_queue_service.settings.queue,
        "metrics_aggregation_queue_enabled",
        False,
    )
    monkeypatch.setattr(
        sqs_event_queue_service.settings.queue,
        "metrics_aggregation_queue_url",
        "https://sqs.example/metrics",
    )

    message_id = sqs_event_queue_service.enqueue_metrics_record_event({"request_id": "req-1"})
    assert message_id == ""


def test_enqueue_metrics_record_event_builds_payload(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        sqs_event_queue_service.settings.queue,
        "metrics_aggregation_queue_enabled",
        True,
    )
    monkeypatch.setattr(
        sqs_event_queue_service.settings.queue,
        "metrics_aggregation_queue_url",
        "https://sqs.example/metrics",
    )

    def fake_send(queue_url, payload, message_group_id="global"):
        captured["queue_url"] = queue_url
        captured["payload"] = payload
        captured["message_group_id"] = message_group_id
        return "msg-123"

    monkeypatch.setattr(sqs_event_queue_service, "_send_json", fake_send)

    message_id = sqs_event_queue_service.enqueue_metrics_record_event(
        {
            "request_id": "req-1",
            "user_id": "user-1",
            "session_id": "session-1",
            "outcome": "success",
        }
    )

    assert message_id == "msg-123"
    assert captured["queue_url"] == "https://sqs.example/metrics"
    assert captured["payload"]["type"] == "metrics_record"
    assert captured["payload"]["record"]["request_id"] == "req-1"
    assert captured["message_group_id"] == "session-1"
