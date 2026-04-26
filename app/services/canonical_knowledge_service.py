from datetime import datetime, timezone
from app.core.config import get_settings
from app.services.source_policy_engine import TIER0_OFFICIAL


def _settings():
    return get_settings()


def _schema_name() -> str:
    value = str(_settings().postgres.schema_name).strip()
    return value or "unigraph"


def _table(name: str) -> str:
    return f"{_schema_name()}.{name}"


def _like_patterns(values: list[str]) -> list[str]:
    patterns: list[str] = []
    seen: set[str] = set()
    for raw in values:
        compact = " ".join(str(raw or "").split()).strip().lower()
        if len(compact) < 3:
            continue
        key = compact[:120]
        if key in seen:
            continue
        seen.add(key)
        patterns.append(f"%{key}%")
    return patterns[:10]


def _canonical_row(
    *,
    slot_id: str,
    value: str,
    source_url: str,
    evidence_snippet: str,
    confidence: float = 0.92,
) -> dict:
    return {
        "slot_id": slot_id,
        "status": "found",
        "value": " ".join(str(value or "").split()).strip(),
        "source_url": str(source_url or "").strip(),
        "source_tier": TIER0_OFFICIAL,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "evidence_snippet": " ".join(str(evidence_snippet or "").split()).strip()[:320],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "document_type": "canonical_record",
        "extractor_version": "canonical_v1",
    }


def _program_match_rows(query: str, entity_context: dict) -> list[dict]:
    if not bool(getattr(_settings().postgres, "enabled", False)):
        return []
    hints = []
    hints.extend(entity_context.get("university_mentions", []))
    hints.extend(entity_context.get("program_mentions", []))
    hints.extend(entity_context.get("subject_focus_tokens", []))
    hints.append(query)
    patterns = _like_patterns(hints)
    if not patterns:
        return []
    sql = f"""
        SELECT
            p.id AS program_id,
            p.program_name,
            p.program_url,
            p.degree_level,
            p.duration_months,
            p.ects_credits,
            p.tuition_fee,
            p.tuition_currency,
            p.language_primary,
            u.name AS university_name,
            u.website AS university_website,
            u.application_portal
        FROM {_table("programs")} p
        JOIN {_table("universities")} u ON u.id = p.university_id
        WHERE (
            lower(u.name) LIKE ANY(%s)
            OR lower(p.program_name) LIKE ANY(%s)
        )
        ORDER BY p.updated_at DESC
        LIMIT 8
    """
    from app.infra.postgres_client import get_postgres_pool

    pool = get_postgres_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (patterns, patterns))
            rows = cur.fetchall() or []
    return [row for row in rows if isinstance(row, dict)]


def _program_support_rows(program_ids: list[str], table_name: str) -> list[dict]:
    if not program_ids:
        return []
    sql = f"SELECT * FROM {_table(table_name)} WHERE program_id = ANY(%s) LIMIT 64"
    from app.infra.postgres_client import get_postgres_pool

    pool = get_postgres_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (program_ids,))
            rows = cur.fetchall() or []
    return [row for row in rows if isinstance(row, dict)]


def fetch_canonical_slot_facts(
    *,
    query: str,
    required_slots: list[dict],
    entity_context: dict,
) -> list[dict]:
    if not bool(getattr(_settings().postgres, "enabled", False)):
        return []
    try:
        program_rows = _program_match_rows(query, entity_context)
    except Exception:
        return []
    if not program_rows:
        return []

    required_ids = {
        str(slot.get("slot_id", "")).strip()
        for slot in required_slots
        if str(slot.get("slot_id", "")).strip()
    }
    program_ids = [str(row.get("program_id", "")).strip() for row in program_rows if row.get("program_id")]
    intakes = []
    routes = []
    requirements = []
    language_rows = []
    try:
        intakes = _program_support_rows(program_ids, "program_intakes")
        routes = _program_support_rows(program_ids, "application_routes")
        requirements = _program_support_rows(program_ids, "program_requirements")
        language_rows = _program_support_rows(program_ids, "language_requirements")
    except Exception:
        intakes = []
        routes = []
        requirements = []
        language_rows = []

    canonical_rows: list[dict] = []
    primary = program_rows[0]
    primary_url = str(primary.get("program_url", "")).strip() or str(primary.get("university_website", "")).strip()

    if "program_overview" in required_ids:
        canonical_rows.append(
            _canonical_row(
                slot_id="program_overview",
                value=(
                    f"{primary.get('university_name', '')}: {primary.get('program_name', '')} "
                    f"({primary.get('degree_level', '')})"
                ),
                source_url=primary_url,
                evidence_snippet="Program and university record from canonical metadata.",
            )
        )
    if "application_portal" in required_ids:
        portal_value = ""
        for route in routes:
            portal_value = " ".join(str(route.get("portal_url", "")).split()).strip()
            if portal_value:
                break
        if not portal_value:
            portal_value = " ".join(str(primary.get("application_portal", "")).split()).strip()
        if portal_value:
            canonical_rows.append(
                _canonical_row(
                    slot_id="application_portal",
                    value=portal_value,
                    source_url=portal_value if portal_value.startswith("http") else primary_url,
                    evidence_snippet="Application portal from canonical route metadata.",
                )
            )
    if "application_deadline" in required_ids:
        deadline_value = ""
        for intake in intakes:
            raw_date = intake.get("application_deadline")
            if raw_date is None:
                continue
            deadline_value = str(raw_date)
            if deadline_value:
                break
        if deadline_value:
            canonical_rows.append(
                _canonical_row(
                    slot_id="application_deadline",
                    value=deadline_value,
                    source_url=primary_url,
                    evidence_snippet="Application deadline from canonical intake metadata.",
                )
            )
    if "language_requirements" in required_ids or "language_test_score_thresholds" in required_ids:
        language_values: list[str] = []
        for row in language_rows[:6]:
            language = " ".join(str(row.get("language", "")).split()).strip()
            test = " ".join(str(row.get("test_type", "")).split()).strip()
            score = " ".join(str(row.get("min_score", "")).split()).strip()
            if test or score:
                language_values.append(" ".join(part for part in (language, test, score) if part).strip())
        if language_values:
            compact = "; ".join(language_values[:4])
            if "language_requirements" in required_ids:
                canonical_rows.append(
                    _canonical_row(
                        slot_id="language_requirements",
                        value=compact,
                        source_url=primary_url,
                        evidence_snippet="Language requirements from canonical language requirement records.",
                    )
                )
            if "language_test_score_thresholds" in required_ids:
                canonical_rows.append(
                    _canonical_row(
                        slot_id="language_test_score_thresholds",
                        value=compact,
                        source_url=primary_url,
                        evidence_snippet="Language score thresholds from canonical language requirement records.",
                    )
                )
    if "ects_or_prerequisite_credit_breakdown" in required_ids:
        ects_value = primary.get("ects_credits")
        if ects_value is not None and str(ects_value).strip():
            canonical_rows.append(
                _canonical_row(
                    slot_id="ects_or_prerequisite_credit_breakdown",
                    value=f"{ects_value} ECTS",
                    source_url=primary_url,
                    evidence_snippet="ECTS credits from canonical program metadata.",
                )
            )
    if "tuition_or_fees" in required_ids:
        fee_value = primary.get("tuition_fee")
        fee_currency = " ".join(str(primary.get("tuition_currency", "")).split()).strip()
        if fee_value is not None and str(fee_value).strip():
            canonical_rows.append(
                _canonical_row(
                    slot_id="tuition_or_fees",
                    value=f"{fee_value} {fee_currency}".strip(),
                    source_url=primary_url,
                    evidence_snippet="Tuition/fee value from canonical program metadata.",
                )
            )
    if "gpa_or_grade_threshold" in required_ids or "eligibility_requirements" in required_ids:
        requirement_texts: list[str] = []
        for row in requirements[:12]:
            req_type = " ".join(str(row.get("requirement_type", "")).split()).strip()
            req_value = " ".join(str(row.get("requirement_value", "")).split()).strip()
            if req_type or req_value:
                requirement_texts.append(" - ".join(part for part in (req_type, req_value) if part).strip())
        if requirement_texts:
            compact = "; ".join(requirement_texts[:4])
            if "eligibility_requirements" in required_ids:
                canonical_rows.append(
                    _canonical_row(
                        slot_id="eligibility_requirements",
                        value=compact,
                        source_url=primary_url,
                        evidence_snippet="Eligibility requirements from canonical program requirements.",
                    )
                )
            if "gpa_or_grade_threshold" in required_ids:
                canonical_rows.append(
                    _canonical_row(
                        slot_id="gpa_or_grade_threshold",
                        value=compact,
                        source_url=primary_url,
                        evidence_snippet="Grade thresholds from canonical program requirements.",
                    )
                )
    return canonical_rows[:24]
