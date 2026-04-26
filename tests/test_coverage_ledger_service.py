from app.services import coverage_ledger_service as service


def _slot(slot_id: str, *, freshness_rule_days: int = 365) -> dict:
    return {
        "slot_id": slot_id,
        "label": slot_id.replace("_", " "),
        "data_type": "text",
        "criticality": "high",
        "critical": True,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": freshness_rule_days,
        "conflict_rule": "manual_review",
    }


def test_build_coverage_ledger_detects_conflict():
    required_slots = [_slot("application_deadline")]
    field_evidence = [
        {
            "id": "application_deadline",
            "status": "found",
            "value": "2026-05-31",
            "source_url": "https://uni.example.de/deadline-a",
            "source_tier": "tier0_official",
            "confidence": 0.9,
        },
        {
            "id": "application_deadline",
            "status": "found",
            "value": "2026-06-15",
            "source_url": "https://uni.example.de/deadline-b",
            "source_tier": "tier0_official",
            "confidence": 0.91,
        },
    ]
    ledger = service.build_coverage_ledger(
        required_slots=required_slots,
        field_evidence=field_evidence,
    )
    assert ledger[0]["status"] == "conflict"


def test_build_coverage_ledger_marks_stale_by_freshness_rule():
    required_slots = [_slot("application_deadline", freshness_rule_days=1)]
    field_evidence = [
        {
            "id": "application_deadline",
            "status": "found",
            "value": "2026-05-31",
            "source_url": "https://uni.example.de/deadline",
            "source_tier": "tier0_official",
            "confidence": 0.9,
            "retrieved_at": "2020-01-01T00:00:00+00:00",
        }
    ]
    ledger = service.build_coverage_ledger(
        required_slots=required_slots,
        field_evidence=field_evidence,
    )
    assert ledger[0]["status"] == "stale"


def test_unresolved_slots_from_ledger_returns_non_found_only():
    ledger = [
        {"slot_id": "application_deadline", "status": "found"},
        {"slot_id": "gpa_or_grade_threshold", "status": "missing"},
    ]
    unresolved = service.unresolved_slots_from_ledger(ledger)
    assert unresolved == ["gpa_or_grade_threshold"]


def test_build_coverage_ledger_maps_new_slot_aliases():
    required_slots = [_slot("international_deadline"), _slot("gpa_threshold")]
    field_evidence = [
        {
            "id": "application_deadline",
            "status": "found",
            "value": "2026-05-31",
            "source_url": "https://uni.example.de/deadline",
            "source_tier": "tier0_official",
            "confidence": 0.9,
        },
        {
            "id": "gpa_or_grade_threshold",
            "status": "found",
            "value": "2.5",
            "source_url": "https://uni.example.de/requirements",
            "source_tier": "tier0_official",
            "confidence": 0.9,
        },
    ]
    ledger = service.build_coverage_ledger(
        required_slots=required_slots,
        field_evidence=field_evidence,
    )
    by_slot = {row["slot_id"]: row for row in ledger}
    assert by_slot["international_deadline"]["status"] == "found"
    assert by_slot["gpa_threshold"]["status"] == "found"
