import os

os.environ.setdefault("SECURITY_JWT_SECRET", "unit-test-secret-1234567890-abcdefghijklmnopqrstuvwxyz")

import pytest

from app.services import web_retrieval_service as service


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_emits_student_contract_fields(monkeypatch):
    monkeypatch.setattr(service, "_should_run_standard_first_pass", lambda **_kwargs: False)

    async def _fake_run_retrieval(*_args, **_kwargs):
        return {
            "query_variants": ["mannheim msc business informatics deadline"],
            "timings_ms": {"search": 10, "page_fetch": 5, "total": 18},
            "results": [
                {
                    "content": "Application deadline is 31 May 2026.",
                    "metadata": {
                        "url": "https://www.uni-mannheim.de/studium/bewerbung/",
                        "title": "Application",
                    },
                }
            ],
            "field_evidence": [
                {
                    "id": "application_deadline",
                    "status": "found",
                    "value": "31 May 2026",
                    "source_url": "https://www.uni-mannheim.de/studium/bewerbung/",
                    "source_tier": "tier0_official",
                    "confidence": 0.9,
                    "evidence_snippet": "Application deadline is 31 May 2026.",
                }
            ],
            "verification": {"unique_domain_count": 1},
        }

    monkeypatch.setattr(service, "_run_retrieval_with_context", _fake_run_retrieval)
    result = await service.aretrieve_web_chunks(
        "University of Mannheim MSc Business Informatics deadline and portal",
        top_k=2,
        search_mode="deep",
    )
    assert "question_schema_id" in result
    assert "required_slots" in result
    assert "coverage_ledger" in result
    assert "unresolved_slots" in result
    assert "source_policy_decisions" in result
    assert "retrieval_budget_usage" in result
