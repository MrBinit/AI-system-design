import json

from botocore.exceptions import ClientError
from redis.exceptions import RedisError

from app.services import summary_queue_service


class FakeSQS:
    def __init__(self):
        self.queues = {}
        self.in_flight = {}
        self.counter = 0

    def _queue(self, queue_url: str):
        return self.queues.setdefault(queue_url, [])

    def send_message(self, QueueUrl, MessageBody, MessageGroupId=None):  # noqa: N803
        _ = MessageGroupId
        self.counter += 1
        message_id = f"msg-{self.counter}"
        receipt_handle = f"rh-{self.counter}"
        self._queue(QueueUrl).append(
            {
                "MessageId": message_id,
                "ReceiptHandle": receipt_handle,
                "Body": MessageBody,
            }
        )
        return {"MessageId": message_id}

    def receive_message(
        self,
        QueueUrl,  # noqa: N803
        MaxNumberOfMessages=1,  # noqa: N803
        WaitTimeSeconds=0,  # noqa: N803
        VisibilityTimeout=30,  # noqa: N803
        MessageAttributeNames=None,  # noqa: N803
        AttributeNames=None,  # noqa: N803
    ):
        _ = (
            WaitTimeSeconds,
            VisibilityTimeout,
            MessageAttributeNames,
            AttributeNames,
        )
        queue = self._queue(QueueUrl)
        batch = []
        for _idx in range(min(MaxNumberOfMessages, len(queue))):
            message = queue.pop(0)
            self.in_flight[message["ReceiptHandle"]] = {
                "queue_url": QueueUrl,
                "message": dict(message),
            }
            batch.append(dict(message))
        return {"Messages": batch}

    def delete_message(self, QueueUrl, ReceiptHandle):  # noqa: N803
        in_flight = self.in_flight.get(ReceiptHandle)
        if in_flight and in_flight["queue_url"] == QueueUrl:
            del self.in_flight[ReceiptHandle]
        return {}

    def get_queue_attributes(self, QueueUrl, AttributeNames):  # noqa: N803
        _ = AttributeNames
        visible = len(self._queue(QueueUrl))
        not_visible = sum(
            1 for entry in self.in_flight.values() if entry.get("queue_url") == QueueUrl
        )
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": str(visible),
                "ApproximateNumberOfMessagesNotVisible": str(not_visible),
            }
        }


def _configure_summary_queue(monkeypatch, fake_sqs: FakeSQS):
    monkeypatch.setattr(summary_queue_service, "_sqs_client", lambda: fake_sqs)
    monkeypatch.setattr(summary_queue_service.settings.queue, "summary_queue_enabled", True)
    monkeypatch.setattr(
        summary_queue_service.settings.queue,
        "summary_queue_url",
        "https://sqs.us-east-1.amazonaws.com/123/summary-jobs",
    )
    monkeypatch.setattr(
        summary_queue_service.settings.queue,
        "summary_dlq_url",
        "https://sqs.us-east-1.amazonaws.com/123/summary-jobs-dlq",
    )
    monkeypatch.setattr(summary_queue_service.settings.queue, "summary_receive_wait_seconds", 0)
    monkeypatch.setattr(summary_queue_service.settings.queue, "summary_max_messages_per_poll", 10)
    monkeypatch.setattr(
        summary_queue_service.settings.queue,
        "summary_visibility_timeout_seconds",
        30,
    )


def test_monitor_summary_dlq_alerts_once_per_cooldown(monkeypatch):
    fake_sqs = FakeSQS()
    _configure_summary_queue(monkeypatch, fake_sqs)
    monkeypatch.setattr(
        summary_queue_service.settings.memory, "summary_queue_dlq_alert_threshold", 1
    )
    monkeypatch.setattr(
        summary_queue_service.settings.memory,
        "summary_queue_dlq_alert_cooldown_seconds",
        300,
    )

    summary_queue_service._LAST_DLQ_ALERT_AT_MONOTONIC = 0.0
    summary_queue_service._LAST_DLQ_INFO = None

    payload = {
        "job_id": "job-1",
        "user_id": "user-1",
        "failed_at": "2026-02-28T00:00:00+00:00",
        "error": "boom",
        "final_attempt": "5",
    }
    fake_sqs.send_message(
        QueueUrl=summary_queue_service.settings.queue.summary_dlq_url,
        MessageBody=json.dumps(payload),
        MessageGroupId="summary",
    )
    summary_queue_service._remember_latest_dlq(payload)

    alerts = []
    monkeypatch.setattr(
        summary_queue_service.logger,
        "error",
        lambda message, payload: alerts.append((message, payload)),
    )

    first = summary_queue_service.monitor_summary_dlq()
    second = summary_queue_service.monitor_summary_dlq()

    assert first["depth"] == 1
    assert first["alerted"] is True
    assert second["alerted"] is False
    assert len(alerts) == 1
    assert "SummaryJobDLQAlert" in alerts[0][0]


def test_retry_or_dlq_summary_job_triggers_dlq_monitor(monkeypatch):
    fake_sqs = FakeSQS()
    _configure_summary_queue(monkeypatch, fake_sqs)
    monkeypatch.setattr(summary_queue_service.settings.memory, "summary_queue_max_attempts", 5)

    acked = []
    monitored = []
    monkeypatch.setattr(
        summary_queue_service, "ack_summary_job", lambda stream_id: acked.append(stream_id)
    )
    monkeypatch.setattr(
        summary_queue_service, "monitor_summary_dlq", lambda: monitored.append(True)
    )

    summary_queue_service.retry_or_dlq_summary_job(
        "rh-7",
        {
            "job_id": "job-7",
            "user_id": "user-7",
            "attempt": "4",
        },
        "failed badly",
    )

    dlq_messages = fake_sqs.queues[summary_queue_service.settings.queue.summary_dlq_url]
    assert len(dlq_messages) == 1
    payload = json.loads(dlq_messages[0]["Body"])
    assert payload["job_id"] == "job-7"
    assert payload["final_attempt"] == "5"
    assert acked == ["rh-7"]
    assert monitored == [True]


def test_claim_stale_summary_jobs_returns_empty(monkeypatch):
    fake_sqs = FakeSQS()
    _configure_summary_queue(monkeypatch, fake_sqs)
    jobs = summary_queue_service.claim_stale_summary_jobs("worker-a")
    assert jobs == []


def test_enqueue_summary_job_uses_sqs(monkeypatch):
    fake_sqs = FakeSQS()
    _configure_summary_queue(monkeypatch, fake_sqs)

    job_id = summary_queue_service.enqueue_summary_job(
        user_id="user-1",
        cutoff_seq=10,
        trigger="token_limit",
        enqueue_version=2,
        approx_removed_tokens=900,
    )

    assert isinstance(job_id, str) and job_id
    messages = fake_sqs.queues[summary_queue_service.settings.queue.summary_queue_url]
    assert len(messages) == 1
    payload = json.loads(messages[0]["Body"])
    assert payload["job_id"] == job_id
    assert payload["user_id"] == "user-1"
    assert payload["cutoff_seq"] == "10"


def test_read_and_ack_summary_jobs_updates_queue_state(monkeypatch):
    fake_sqs = FakeSQS()
    _configure_summary_queue(monkeypatch, fake_sqs)

    summary_queue_service.enqueue_summary_job(
        user_id="user-2",
        cutoff_seq=20,
        trigger="summary_trigger",
        enqueue_version=3,
        approx_removed_tokens=1200,
    )
    before = summary_queue_service.get_summary_queue_state()
    jobs = summary_queue_service.read_summary_jobs("worker-a")
    mid = summary_queue_service.get_summary_queue_state()

    assert before["stream_depth"] == 1
    assert before["pending_jobs"] == 0
    assert len(jobs) == 1
    receipt_handle, fields = jobs[0]
    assert receipt_handle
    assert fields["user_id"] == "user-2"
    assert mid["stream_depth"] == 0
    assert mid["pending_jobs"] == 1

    summary_queue_service.ack_summary_job(receipt_handle)
    after = summary_queue_service.get_summary_queue_state()
    assert after["pending_jobs"] == 0


def test_secrets_region_resolution(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_SECRETS_MANAGER_REGION", raising=False)
    assert summary_queue_service._secrets_region() is None

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    assert summary_queue_service._secrets_region() == "us-east-1"


def test_copy_payload_and_group_id_helpers():
    class DemoValue:
        def __str__(self):
            return "demo-value"

    payload = summary_queue_service._copy_job_payload(
        {
            "visible": "yes",
            "_hidden": "skip",
            99: "invalid-key",
            "extra": DemoValue(),
        }
    )
    assert payload == {"visible": "yes", "extra": "demo-value"}
    assert summary_queue_service._safe_message_group_id("") == "summary"
    assert len(summary_queue_service._safe_message_group_id("x" * 200)) == 128


def test_send_json_retries_without_message_group(monkeypatch):
    class RetrySQS:
        def __init__(self):
            self.calls = []
            self._first = True

        def send_message(self, **kwargs):
            self.calls.append(dict(kwargs))
            if self._first:
                self._first = False
                raise ClientError(
                    {"Error": {"Code": "InvalidParameterValue", "Message": "bad group"}},
                    "SendMessage",
                )
            return {"MessageId": "ok-1"}

    fake = RetrySQS()
    monkeypatch.setattr(summary_queue_service, "_sqs_client", lambda: fake)

    sent = summary_queue_service._send_json(
        "https://sqs.us-east-1.amazonaws.com/123/summary-jobs",
        {"job_id": "job-1"},
        message_group_id="user-1",
    )

    assert sent is True
    assert len(fake.calls) == 2
    assert "MessageGroupId" in fake.calls[0]
    assert "MessageGroupId" not in fake.calls[1]


def test_send_json_returns_false_on_client_and_runtime_errors(monkeypatch):
    class DeniedSQS:
        def send_message(self, **kwargs):
            _ = kwargs
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "no permissions"}},
                "SendMessage",
            )

    class BrokenSQS:
        def send_message(self, **kwargs):
            _ = kwargs
            raise RuntimeError("network down")

    assert summary_queue_service._send_json("", {"x": 1}, "g") is False

    monkeypatch.setattr(summary_queue_service, "_sqs_client", lambda: DeniedSQS())
    assert (
        summary_queue_service._send_json("https://sqs.us-east-1.amazonaws.com/123/q", {"x": 1}, "g")
        is False
    )

    monkeypatch.setattr(summary_queue_service, "_sqs_client", lambda: BrokenSQS())
    assert (
        summary_queue_service._send_json("https://sqs.us-east-1.amazonaws.com/123/q", {"x": 1}, "g")
        is False
    )


def test_queue_depth_and_pending_edge_cases(monkeypatch):
    class BadAttributesSQS:
        def get_queue_attributes(self, **kwargs):
            _ = kwargs
            return {"Attributes": []}

    class NegativeCountsSQS:
        def get_queue_attributes(self, **kwargs):
            _ = kwargs
            return {
                "Attributes": {
                    "ApproximateNumberOfMessages": "-5",
                    "ApproximateNumberOfMessagesNotVisible": "-2",
                }
            }

    class ErrorSQS:
        def get_queue_attributes(self, **kwargs):
            _ = kwargs
            raise RuntimeError("boom")

    assert summary_queue_service._queue_depth_and_pending("") == (0, 0)

    monkeypatch.setattr(
        summary_queue_service,
        "_sqs_client",
        lambda: BadAttributesSQS(),
    )
    assert summary_queue_service._queue_depth_and_pending(
        "https://sqs.us-east-1.amazonaws.com/123/q"
    ) == (
        0,
        0,
    )

    monkeypatch.setattr(
        summary_queue_service,
        "_sqs_client",
        lambda: NegativeCountsSQS(),
    )
    assert summary_queue_service._queue_depth_and_pending(
        "https://sqs.us-east-1.amazonaws.com/123/q"
    ) == (
        0,
        0,
    )

    monkeypatch.setattr(
        summary_queue_service,
        "_sqs_client",
        lambda: ErrorSQS(),
    )
    assert summary_queue_service._queue_depth_and_pending(
        "https://sqs.us-east-1.amazonaws.com/123/q"
    ) == (
        0,
        0,
    )


def test_idempotency_key_selection_paths():
    explicit = summary_queue_service.get_summary_job_idempotency_key(
        {"idempotency_key": " explicit-key "}
    )
    assert explicit == "explicit-key"

    rebuilt = summary_queue_service.get_summary_job_idempotency_key(
        {
            "user_id": "u-1",
            "cutoff_seq": "10",
            "trigger": "token_limit",
            "enqueue_version": "2",
        }
    )
    assert len(rebuilt) == 64

    by_job_id = summary_queue_service.get_summary_job_idempotency_key(
        {"user_id": "u-1", "cutoff_seq": "bad", "job_id": "job-77"}
    )
    assert by_job_id == "job:job-77"
    assert summary_queue_service.get_summary_job_idempotency_key({}) == ""


def test_redis_marker_helpers_handle_redis_errors(monkeypatch):
    def boom(*args, **kwargs):
        _ = (args, kwargs)
        raise RedisError("redis unavailable")

    monkeypatch.setattr(summary_queue_service.worker_redis_client, "get", boom)
    monkeypatch.setattr(summary_queue_service.worker_redis_client, "set", boom)
    monkeypatch.setattr(summary_queue_service.worker_redis_client, "delete", boom)

    assert summary_queue_service.is_summary_job_processed("abc") is False
    assert summary_queue_service.claim_summary_job_processing("abc", "stream-1") is True
    summary_queue_service.release_summary_job_processing("abc")
    summary_queue_service.mark_summary_job_processed("abc", "stream-1")
