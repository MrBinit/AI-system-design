import asyncio

from app.scripts import eval_dynamodb_worker


class _FakeDynamoClient:
    class exceptions:
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self):
        self.query_calls = []
        self.put_item_calls = []

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return {
            "Items": [
                {
                    "request_id": {"S": "req-1"},
                    "timestamp": {"S": "2026-03-11T00:00:00+00:00"},
                    "user_id": {"S": "user-1"},
                    "session_id": {"S": "session-1"},
                    "outcome": {"S": "success"},
                    "question": {"S": "What is AI?"},
                    "answer": {"S": "AI is ..."},
                    "retrieval_evidence_json": {"S": "[]"},
                    "record_json": {"S": "{}"},
                    "eval_status": {"S": "pending"},
                }
            ]
        }

    def put_item(self, **kwargs):
        self.put_item_calls.append(kwargs)


def test_load_requests_for_eval_uses_status_index_query(monkeypatch):
    fake = _FakeDynamoClient()
    monkeypatch.setattr(eval_dynamodb_worker, "_dynamodb_client", lambda: fake)
    monkeypatch.setattr(
        eval_dynamodb_worker.settings.app,
        "metrics_dynamodb_requests_table",
        "chat-metrics-requests",
    )
    monkeypatch.setattr(
        eval_dynamodb_worker.settings.evaluation,
        "request_status_attribute",
        "eval_status",
    )
    monkeypatch.setattr(
        eval_dynamodb_worker.settings.evaluation,
        "request_status_index_name",
        "eval-status-timestamp-index",
    )
    monkeypatch.setattr(
        eval_dynamodb_worker.settings.evaluation,
        "request_pending_value",
        "pending",
    )

    rows = eval_dynamodb_worker._load_requests_for_eval(max_items=5, lookback_hours=24)

    assert len(rows) == 1
    assert rows[0]["request_id"] == "req-1"
    assert len(fake.query_calls) == 1
    query = fake.query_calls[0]
    assert query["IndexName"] == "eval-status-timestamp-index"
    assert "#status = :status_value" in query["KeyConditionExpression"]
    assert "#ts >= :cutoff_iso" in query["KeyConditionExpression"]


def test_persist_eval_uses_conditional_put_and_status(monkeypatch):
    fake = _FakeDynamoClient()
    monkeypatch.setattr(eval_dynamodb_worker, "_dynamodb_client", lambda: fake)
    monkeypatch.setattr(eval_dynamodb_worker.settings.evaluation, "dynamodb_table", "eval-table")
    monkeypatch.setattr(eval_dynamodb_worker.settings.evaluation, "eval_status_attribute", "status")
    monkeypatch.setattr(
        eval_dynamodb_worker.settings.evaluation, "eval_completed_value", "completed"
    )

    inserted = eval_dynamodb_worker._persist_eval(
        {
            "request_id": "req-1",
            "timestamp": "2026-03-11T00:00:00+00:00",
            "user_id": "user-1",
            "session_id": "session-1",
            "question": "q",
            "answer": "a",
        },
        {
            "clarity_score": 0.8,
            "relevance_score": 0.9,
            "hallucination_score": 0.1,
            "evidence_similarity_score": 0.9,
            "answered_question": True,
            "failure_reason": "none",
            "judge_prompt_tokens": 10,
            "judge_completion_tokens": 5,
            "judge_total_tokens": 15,
            "notes": "ok",
        },
    )

    assert inserted is True
    assert len(fake.put_item_calls) == 1
    call = fake.put_item_calls[0]
    assert call["TableName"] == "eval-table"
    assert call["ConditionExpression"] == "attribute_not_exists(request_id)"
    assert call["Item"]["status"]["S"] == "completed"


def test_run_request_eval_claim_guard_prevents_duplicate(monkeypatch):
    monkeypatch.setattr(
        eval_dynamodb_worker.settings.app,
        "metrics_dynamodb_requests_table",
        "chat-metrics-requests",
    )

    monkeypatch.setattr(
        eval_dynamodb_worker,
        "_load_request_for_eval",
        lambda _request_id: (_ for _ in ()).throw(AssertionError("load should not run")),
    )
    monkeypatch.setattr(eval_dynamodb_worker, "_claim_request_for_eval", lambda _request_id: False)

    result = asyncio.run(eval_dynamodb_worker.run_request_eval("req-1"))

    assert result["evaluated"] is False
    assert result["skipped"] is True
    assert result["reason"] == "already evaluated or in progress"


def test_run_request_eval_reverts_to_pending_when_claimed_but_request_missing(monkeypatch):
    monkeypatch.setattr(eval_dynamodb_worker, "_claim_request_for_eval", lambda _request_id: True)
    monkeypatch.setattr(eval_dynamodb_worker, "_load_request_for_eval", lambda _request_id: None)
    marks = []
    monkeypatch.setattr(
        eval_dynamodb_worker,
        "_mark_request_eval_status",
        lambda request_id, status: marks.append((request_id, status)),
    )
    monkeypatch.setattr(
        eval_dynamodb_worker.settings.evaluation,
        "request_pending_value",
        "pending",
    )

    result = asyncio.run(eval_dynamodb_worker.run_request_eval("req-missing"))

    assert result["evaluated"] is False
    assert result["skipped"] is True
    assert result["reason"] == "request not found or not successful"
    assert marks == [("req-missing", "pending")]
