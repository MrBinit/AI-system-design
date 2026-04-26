from datetime import datetime, timezone

_STATUS_FOUND = "found"
_STATUS_MISSING = "missing"
_STATUS_CONFLICT = "conflict"
_STATUS_STALE = "stale"

_STATUS_PRIORITY = {
    _STATUS_FOUND: 4,
    _STATUS_STALE: 3,
    _STATUS_CONFLICT: 2,
    _STATUS_MISSING: 1,
}

_SOURCE_TIER_RANK = {
    "tier0_official": 4,
    "tier1_corroboration": 3,
    "tier2_secondary": 2,
    "discovery": 1,
}

_SLOT_ALIASES: dict[str, set[str]] = {
    "gpa_threshold": {"gpa_threshold", "gpa_or_grade_threshold"},
    "gpa_or_grade_threshold": {"gpa_threshold"},
    "ects_prerequisites": {"ects_prerequisites", "ects_breakdown", "ects_or_prerequisite_credit_breakdown"},
    "ects_or_prerequisite_credit_breakdown": {"ects_breakdown"},
    "instruction_language": {"instruction_language", "language_of_instruction"},
    "language_test_thresholds": {"language_test_thresholds", "language_score_thresholds", "language_test_score_thresholds"},
    "language_test_score_thresholds": {"language_score_thresholds"},
    "international_deadline": {"international_deadline", "application_deadline"},
    "program_overview": {"program_overview"},
    "duration_ects": {"duration_ects"},
    "eligibility_requirements": {"admission_requirements"},
    "tuition_or_fees": {"tuition_or_fees", "tuition_fees"},
    "curriculum_focus": {"curriculum_focus", "curriculum_modules"},
    "funding_or_scholarship": {"funding_or_scholarship"},
    "professors_or_supervisors": {"professors_or_supervisors"},
    "labs_or_research_groups": {"labs_or_research_groups"},
    "contact_information": {"contact_information"},
    "visa_or_work_rights": {"visa_or_work_rights"},
    "admission_decision_signal": {"admission_decision_signal"},
}


def _normalize(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _to_iso(value: str) -> str:
    compact = _normalize(value)
    if not compact:
        return ""
    try:
        if compact.endswith("Z"):
            compact = compact[:-1] + "+00:00"
        parsed = datetime.fromisoformat(compact)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _is_stale(*, retrieved_at: str, freshness_days: int, now_utc: datetime) -> bool:
    if freshness_days <= 0:
        return False
    parsed_iso = _to_iso(retrieved_at)
    if not parsed_iso:
        return False
    try:
        parsed = datetime.fromisoformat(parsed_iso)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age_days = max(0, int((now_utc - parsed).total_seconds() // 86400))
    return age_days > freshness_days


def _slot_keys(slot_id: str) -> set[str]:
    compact = _normalize(slot_id)
    if not compact:
        return set()
    aliases = _SLOT_ALIASES.get(compact, set())
    return {compact, *aliases}


def _evidence_rows_for_slot(
    slot_id: str,
    *,
    field_evidence: list[dict],
    canonical_facts: list[dict],
) -> list[dict]:
    keys = _slot_keys(slot_id)
    rows: list[dict] = []
    for row in field_evidence:
        if not isinstance(row, dict):
            continue
        evidence_key = _normalize(str(row.get("id", row.get("field", ""))))
        if evidence_key not in keys:
            continue
        rows.append(dict(row))
    for row in canonical_facts:
        if not isinstance(row, dict):
            continue
        evidence_key = _normalize(str(row.get("slot_id", row.get("id", row.get("field", "")))))
        if evidence_key not in keys:
            continue
        rows.append(dict(row))
    return rows


def _row_score(row: dict) -> tuple[float, int, int]:
    confidence = float(row.get("confidence", 0.0) or 0.0)
    source_tier = _normalize(str(row.get("source_tier", row.get("source_type", "discovery")))).lower()
    tier_rank = _SOURCE_TIER_RANK.get(source_tier, 0)
    status = _normalize(str(row.get("status", _STATUS_MISSING))).lower()
    status_rank = _STATUS_PRIORITY.get(status, 0)
    return confidence, tier_rank, status_rank


def _choose_best_row(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    ranked = sorted(rows, key=_row_score, reverse=True)
    return ranked[0]


def _distinct_found_values(rows: list[dict]) -> set[str]:
    values: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = _normalize(str(row.get("status", ""))).lower()
        if status not in {_STATUS_FOUND, _STATUS_STALE}:
            continue
        value = _normalize(str(row.get("value", ""))).lower()
        if not value:
            continue
        values.add(value)
    return values


def _ledger_row_from_slot(slot: dict, best_row: dict | None, *, now_utc: datetime) -> dict:
    slot_id = _normalize(str(slot.get("slot_id", "")))
    label = _normalize(str(slot.get("label", slot_id))) or slot_id
    freshness_days = int(slot.get("freshness_rule_days", 0) or 0)
    base = {
        "slot_id": slot_id,
        "id": slot_id,
        "field": slot_id,
        "label": label,
        "data_type": _normalize(str(slot.get("data_type", "text"))) or "text",
        "criticality": _normalize(str(slot.get("criticality", "low"))) or "low",
        "critical": bool(slot.get("critical", False)),
        "source_tier_requirement": _normalize(str(slot.get("source_tier_requirement", "tier0_official"))),
        "freshness_rule_days": freshness_days,
        "conflict_rule": _normalize(str(slot.get("conflict_rule", "manual_review"))) or "manual_review",
        "status": _STATUS_MISSING,
        "value": "Not verified from official sources.",
        "unit": "",
        "qualifier": "",
        "source_url": "",
        "source_tier": "tier0_official",
        "confidence": 0.0,
        "evidence_snippet": "",
        "evidence_text": "",
        "retrieved_at": now_utc.isoformat(),
    }
    if not isinstance(best_row, dict):
        return base

    status = _normalize(str(best_row.get("status", _STATUS_FOUND))).lower()
    if status not in {_STATUS_FOUND, _STATUS_CONFLICT, _STATUS_MISSING, _STATUS_STALE}:
        status = _STATUS_FOUND
    value = _normalize(str(best_row.get("value", "")))
    evidence = _normalize(str(best_row.get("evidence_snippet", best_row.get("evidence_text", ""))))
    source_url = _normalize(str(best_row.get("source_url", "")))
    source_tier = _normalize(str(best_row.get("source_tier", best_row.get("source_type", "discovery")))).lower()
    retrieved_at = _to_iso(str(best_row.get("retrieved_at", ""))) or now_utc.isoformat()
    confidence = max(0.0, min(1.0, float(best_row.get("confidence", 0.0) or 0.0)))
    stale = _is_stale(retrieved_at=retrieved_at, freshness_days=freshness_days, now_utc=now_utc)
    final_status = _STATUS_STALE if status == _STATUS_FOUND and stale else status

    base.update(
        {
            "status": final_status,
            "value": value or ("Not verified from official sources." if final_status != _STATUS_FOUND else ""),
            "source_url": source_url,
            "source_tier": source_tier or "discovery",
            "confidence": round(confidence, 4),
            "evidence_snippet": evidence,
            "evidence_text": evidence,
            "retrieved_at": retrieved_at,
        }
    )
    for key in ("rejection_reason", "rejected_candidates", "unit", "qualifier"):
        if key in best_row:
            base[key] = best_row.get(key)
    if final_status == _STATUS_CONFLICT:
        base["value"] = "Conflict between official sources. Manual verification required."
        base["confidence"] = min(base["confidence"], 0.35)
    elif final_status in {_STATUS_MISSING, _STATUS_STALE} and not base["value"]:
        base["value"] = "Not verified from official sources."
    return base


def build_coverage_ledger(
    *,
    required_slots: list[dict],
    field_evidence: list[dict] | None = None,
    canonical_facts: list[dict] | None = None,
) -> list[dict]:
    field_rows = field_evidence if isinstance(field_evidence, list) else []
    canonical_rows = canonical_facts if isinstance(canonical_facts, list) else []
    now_utc = datetime.now(timezone.utc)
    ledger: list[dict] = []
    for slot in required_slots:
        if not isinstance(slot, dict):
            continue
        slot_id = _normalize(str(slot.get("slot_id", "")))
        if not slot_id:
            continue
        rows = _evidence_rows_for_slot(
            slot_id,
            field_evidence=field_rows,
            canonical_facts=canonical_rows,
        )
        values = _distinct_found_values(rows)
        best = _choose_best_row(rows)
        if len(values) > 1:
            best = dict(best or {})
            best["status"] = _STATUS_CONFLICT
        ledger.append(_ledger_row_from_slot(slot, best, now_utc=now_utc))
    return ledger


def unresolved_slots_from_ledger(ledger: list[dict]) -> list[str]:
    unresolved: list[str] = []
    seen: set[str] = set()
    for row in ledger:
        if not isinstance(row, dict):
            continue
        slot_id = _normalize(str(row.get("slot_id", row.get("id", ""))))
        status = _normalize(str(row.get("status", _STATUS_MISSING))).lower()
        if not slot_id or status == _STATUS_FOUND or slot_id in seen:
            continue
        seen.add(slot_id)
        unresolved.append(slot_id)
    return unresolved


def coverage_score_from_ledger(ledger: list[dict]) -> float:
    rows = [row for row in ledger if isinstance(row, dict)]
    if not rows:
        return 1.0
    found = 0
    for row in rows:
        status = _normalize(str(row.get("status", _STATUS_MISSING))).lower()
        if status == _STATUS_FOUND:
            found += 1
    return round(found / max(1, len(rows)), 4)
