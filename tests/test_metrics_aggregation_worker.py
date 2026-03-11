import asyncio

from app.scripts import metrics_aggregation_worker


def test_process_message_handles_metrics_record_event(monkeypatch):
    processed = []
    deleted = []

    monkeypatch.setattr(
        metrics_aggregation_worker.settings.queue,
        "metrics_aggregation_queue_url",
        "https://sqs.example/metrics",
    )
    monkeypatch.setattr(
        metrics_aggregation_worker,
        "parse_message_json",
        lambda _msg: {
            "type": "metrics_record",
            "record": {"request_id": "req-1", "outcome": "success"},
        },
    )
    monkeypatch.setattr(
        metrics_aggregation_worker,
        "append_chat_metrics_json",
        lambda record: processed.append(record),
    )
    monkeypatch.setattr(
        metrics_aggregation_worker,
        "delete_queue_message",
        lambda queue_url, receipt_handle: deleted.append((queue_url, receipt_handle)),
    )

    asyncio.run(metrics_aggregation_worker._process_message({"ReceiptHandle": "rh-1"}))

    assert processed == [{"request_id": "req-1", "outcome": "success"}]
    assert deleted == [("https://sqs.example/metrics", "rh-1")]
