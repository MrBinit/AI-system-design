from app.services import llm_async_queue_service


def test_mark_job_failed_sanitizes_internal_exception_text(monkeypatch):
    captured = []
    monkeypatch.setattr(
        llm_async_queue_service,
        "_update_job",
        lambda job_id, updates: captured.append((job_id, updates)),
    )
    monkeypatch.setattr(llm_async_queue_service, "_now_iso", lambda: "2026-03-11T00:00:00+00:00")

    llm_async_queue_service.mark_job_failed("job-1", "password=secret db host=10.0.0.1")

    assert captured == [
        (
            "job-1",
            {
                "status": "failed",
                "error": "Async chat job failed.",
                "updated_at": "2026-03-11T00:00:00+00:00",
            },
        )
    ]


def test_mark_job_failed_preserves_safe_queue_enqueue_error(monkeypatch):
    captured = []
    monkeypatch.setattr(
        llm_async_queue_service,
        "_update_job",
        lambda job_id, updates: captured.append((job_id, updates)),
    )
    monkeypatch.setattr(llm_async_queue_service, "_now_iso", lambda: "2026-03-11T00:00:00+00:00")

    llm_async_queue_service.mark_job_failed("job-2", "Queue enqueue failed: endpoint timeout")

    assert captured[0][1]["error"] == "Queue enqueue failed."


def test_mark_job_failed_preserves_invalid_payload_error(monkeypatch):
    captured = []
    monkeypatch.setattr(
        llm_async_queue_service,
        "_update_job",
        lambda job_id, updates: captured.append((job_id, updates)),
    )
    monkeypatch.setattr(llm_async_queue_service, "_now_iso", lambda: "2026-03-11T00:00:00+00:00")

    llm_async_queue_service.mark_job_failed("job-3", "Invalid async job payload.")

    assert captured[0][1]["error"] == "Invalid async job payload."
