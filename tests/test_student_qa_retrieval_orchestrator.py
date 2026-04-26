import os

os.environ.setdefault("SECURITY_JWT_SECRET", "unit-test-secret-1234567890-abcdefghijklmnopqrstuvwxyz")

from app.services import student_qa_retrieval_orchestrator as orchestrator


def test_augment_retrieval_result_with_student_contract_adds_optional_fields(monkeypatch):
    monkeypatch.setattr(orchestrator, "fetch_canonical_slot_facts", lambda **_kwargs: [])
    result = {
        "query_variants": ["mannheim msc business informatics deadline portal"],
        "timings_ms": {"search": 100, "page_fetch": 80, "total": 220},
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
    }
    augmented = orchestrator.augment_retrieval_result_with_student_contract(
        "University of Mannheim MSc Business Informatics deadline and portal",
        result,
    )
    assert augmented["question_schema_id"]
    assert isinstance(augmented["required_slots"], list)
    assert isinstance(augmented["coverage_ledger"], list)
    assert isinstance(augmented["unresolved_slots"], list)
    assert isinstance(augmented["source_policy_decisions"], list)
    assert isinstance(augmented["retrieval_budget_usage"], dict)
