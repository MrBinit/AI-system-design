import re

_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _normalize(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in _QUERY_TOKEN_RE.findall(_normalize(value).lower())
        if len(token) >= 3
    }


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = _normalize(text).lower()
    return any(marker in lowered for marker in markers)


_SLOT_CATALOG: dict[str, dict] = {
    "program_overview": {
        "slot_id": "program_overview",
        "label": "Program overview",
        "data_type": "text",
        "criticality": "medium",
        "critical": False,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "curriculum_focus",
    },
    "eligibility_requirements": {
        "slot_id": "eligibility_requirements",
        "label": "Eligibility requirements",
        "data_type": "text",
        "criticality": "high",
        "critical": True,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "eligibility_requirements",
    },
    "gpa_or_grade_threshold": {
        "slot_id": "gpa_or_grade_threshold",
        "label": "GPA/grade threshold",
        "data_type": "score",
        "criticality": "high",
        "critical": True,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "gpa_or_grade_threshold",
    },
    "gpa_threshold": {
        "slot_id": "gpa_threshold",
        "label": "GPA/grade threshold",
        "data_type": "score",
        "criticality": "high",
        "critical": True,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "gpa_or_grade_threshold",
    },
    "ects_or_prerequisite_credit_breakdown": {
        "slot_id": "ects_or_prerequisite_credit_breakdown",
        "label": "ECTS/prerequisite credits",
        "data_type": "number",
        "criticality": "high",
        "critical": True,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "ects_or_prerequisite_credit_breakdown",
    },
    "ects_prerequisites": {
        "slot_id": "ects_prerequisites",
        "label": "ECTS/prerequisite credits",
        "data_type": "number",
        "criticality": "high",
        "critical": True,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "ects_or_prerequisite_credit_breakdown",
    },
    "instruction_language": {
        "slot_id": "instruction_language",
        "label": "Language of instruction",
        "data_type": "text",
        "criticality": "high",
        "critical": True,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "instruction_language",
    },
    "language_requirements": {
        "slot_id": "language_requirements",
        "label": "Language requirements",
        "data_type": "text",
        "criticality": "high",
        "critical": True,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "language_requirements",
    },
    "language_test_score_thresholds": {
        "slot_id": "language_test_score_thresholds",
        "label": "Language score thresholds",
        "data_type": "score",
        "criticality": "high",
        "critical": True,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "language_test_score_thresholds",
    },
    "language_test_thresholds": {
        "slot_id": "language_test_thresholds",
        "label": "Language score thresholds",
        "data_type": "score",
        "criticality": "high",
        "critical": True,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "language_test_score_thresholds",
    },
    "application_deadline": {
        "slot_id": "application_deadline",
        "label": "Application deadline",
        "data_type": "date",
        "criticality": "high",
        "critical": True,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 180,
        "conflict_rule": "prefer_newer",
        "answer_field_alias": "application_deadline",
    },
    "international_deadline": {
        "slot_id": "international_deadline",
        "label": "Application deadline (international)",
        "data_type": "date",
        "criticality": "high",
        "critical": True,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 180,
        "conflict_rule": "prefer_newer",
        "answer_field_alias": "application_deadline",
    },
    "application_portal": {
        "slot_id": "application_portal",
        "label": "Application portal",
        "data_type": "url",
        "criticality": "high",
        "critical": True,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 180,
        "conflict_rule": "prefer_tier0",
        "answer_field_alias": "application_portal",
    },
    "tuition_or_fees": {
        "slot_id": "tuition_or_fees",
        "label": "Tuition or fees",
        "data_type": "money",
        "criticality": "medium",
        "critical": False,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "tuition_or_fees",
    },
    "curriculum_focus": {
        "slot_id": "curriculum_focus",
        "label": "Curriculum focus",
        "data_type": "text",
        "criticality": "medium",
        "critical": False,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "curriculum_focus",
    },
    "duration_ects": {
        "slot_id": "duration_ects",
        "label": "Duration and ECTS",
        "data_type": "number",
        "criticality": "medium",
        "critical": False,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "curriculum_focus",
    },
    "professors_or_supervisors": {
        "slot_id": "professors_or_supervisors",
        "label": "Professors or supervisors",
        "data_type": "list",
        "criticality": "medium",
        "critical": False,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "professors_or_supervisors",
    },
    "labs_or_research_groups": {
        "slot_id": "labs_or_research_groups",
        "label": "Labs or research groups",
        "data_type": "list",
        "criticality": "medium",
        "critical": False,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "labs_or_research_groups",
    },
    "contact_information": {
        "slot_id": "contact_information",
        "label": "Contact information",
        "data_type": "text",
        "criticality": "medium",
        "critical": False,
        "source_tier_requirement": "tier0_official",
        "freshness_rule_days": 365,
        "conflict_rule": "prefer_tier0",
        "answer_field_alias": "contact_information",
    },
    "visa_or_work_rights": {
        "slot_id": "visa_or_work_rights",
        "label": "Visa or work rights",
        "data_type": "text",
        "criticality": "medium",
        "critical": False,
        "source_tier_requirement": "tier1_corroboration",
        "freshness_rule_days": 180,
        "conflict_rule": "prefer_tier1",
        "answer_field_alias": "visa_or_work_rights",
    },
    "funding_or_scholarship": {
        "slot_id": "funding_or_scholarship",
        "label": "Funding or scholarship",
        "data_type": "text",
        "criticality": "medium",
        "critical": False,
        "source_tier_requirement": "tier1_corroboration",
        "freshness_rule_days": 365,
        "conflict_rule": "manual_review",
        "answer_field_alias": "funding_or_scholarship",
    },
    "admission_decision_signal": {
        "slot_id": "admission_decision_signal",
        "label": "Admission competitiveness signal",
        "data_type": "text",
        "criticality": "low",
        "critical": False,
        "source_tier_requirement": "tier2_secondary",
        "freshness_rule_days": 90,
        "conflict_rule": "manual_review",
        "answer_field_alias": "admission_decision_signal",
    },
}


def _slot(slot_id: str) -> dict:
    return dict(_SLOT_CATALOG[slot_id])


def _dedupe_slot_ids(slot_ids: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for slot_id in slot_ids:
        key = _normalize(slot_id)
        if not key or key in seen:
            continue
        if key not in _SLOT_CATALOG:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


def resolve_question_schema(prompt: str, *, intent: str = "") -> dict:
    text = _normalize(prompt).lower()
    query_tokens = _tokens(text)
    has_university_context = bool(
        query_tokens
        & {"university", "universitat", "universitaet", "hochschule", "college", "program", "programme"}
    )
    has_admission = _contains_any(
        text,
        (
            "admission",
            "eligibility",
            "requirements",
            "required documents",
            "deadline",
            "apply",
            "application portal",
            "ielts",
            "toefl",
            "ects",
            "gpa",
            "grade",
        ),
    )
    has_instruction_language = _contains_any(
        text,
        ("language of instruction", "teaching language", "taught in", "unterrichtssprache"),
    )
    has_language = _contains_any(
        text,
        ("language", "ielts", "toefl", "cefr", "testdaf", "dsh", "sprachnachweis"),
    )
    has_deadline = _contains_any(
        text,
        ("deadline", "application period", "last date", "bewerbungsfrist", "apply by"),
    )
    has_portal = _contains_any(
        text,
        (
            "application portal",
            "where to apply",
            "where can i apply",
            "apply online",
            "portal",
            "bewerbungsportal",
        ),
    )
    has_grade_credit_scope = _contains_any(
        text,
        ("gpa", "grade", "cgpa", "ects", "credit", "credits", "prerequisite"),
    )
    has_eligibility_scope = _contains_any(
        text,
        (
            "admission requirements",
            "course requirements",
            "eligibility",
            "entry requirements",
            "required documents",
        ),
    )
    has_research = _contains_any(
        text,
        (
            "professor",
            "supervisor",
            "faculty",
            "research group",
            "lab",
            "laboratory",
            "publication",
        ),
    )
    has_finance = _contains_any(
        text,
        ("tuition", "fee", "fees", "scholarship", "funding", "grant", "cost"),
    )
    has_visa = _contains_any(text, ("visa", "work rights", "residence permit", "aps"))
    asks_competitiveness = _contains_any(
        text,
        (
            "competitive",
            "safe",
            "chances",
            "chance",
            "likely",
            "risky",
            "admission decision",
            "verdict",
        ),
    )
    asks_overview = _contains_any(
        text,
        ("tell me about", "about", "overview", "program snapshot", "details", "curriculum"),
    )
    asks_international_scope = _contains_any(
        text,
        ("international", "international students", "foreign applicants", "overseas applicants"),
    )
    has_explicit_slot_scope = bool(
        has_grade_credit_scope
        or has_eligibility_scope
        or has_language
        or has_deadline
        or has_portal
        or has_finance
        or has_research
        or has_visa
    )

    slot_ids: list[str] = []
    if has_admission:
        if has_eligibility_scope or has_grade_credit_scope:
            slot_ids.append("eligibility_requirements")
        if has_grade_credit_scope or has_eligibility_scope:
            slot_ids.extend(
                [
                    "gpa_threshold",
                    "ects_prerequisites",
                ]
            )
        if has_instruction_language:
            slot_ids.append("instruction_language")
        if has_language:
            slot_ids.extend(
                [
                    "language_requirements",
                    "language_test_score_thresholds",
                ]
            )
        if has_deadline:
            slot_ids.append("international_deadline" if asks_international_scope else "application_deadline")
        if has_portal:
            slot_ids.append("application_portal")
        if not (has_grade_credit_scope or has_language or has_deadline or has_portal):
            slot_ids.append("eligibility_requirements")
    if has_university_context and ((asks_overview and not has_explicit_slot_scope) or not has_admission):
        slot_ids.append("program_overview")
        if asks_overview and not has_explicit_slot_scope:
            slot_ids.extend(
                [
                    "eligibility_requirements",
                    "duration_ects",
                    "instruction_language",
                    "language_requirements",
                    "application_deadline",
                    "application_portal",
                    "curriculum_focus",
                ]
            )
    if has_instruction_language:
        slot_ids.append("instruction_language")
    if has_language:
        slot_ids.extend(["language_requirements", "language_test_score_thresholds"])
    if has_deadline:
        slot_ids.append("international_deadline" if asks_international_scope else "application_deadline")
    if has_portal:
        slot_ids.append("application_portal")
    if has_finance:
        slot_ids.extend(["tuition_or_fees", "funding_or_scholarship"])
    if _contains_any(text, ("curriculum", "modules", "module", "course structure")):
        slot_ids.append("curriculum_focus")
    if has_research:
        slot_ids.extend(
            [
                "professors_or_supervisors",
                "labs_or_research_groups",
                "contact_information",
            ]
        )
    if has_visa:
        slot_ids.append("visa_or_work_rights")
    if asks_competitiveness:
        slot_ids.append("admission_decision_signal")

    if not slot_ids:
        if intent == "comparison":
            slot_ids = ["program_overview", "eligibility_requirements", "application_deadline"]
        elif has_university_context:
            slot_ids = ["program_overview"]
        else:
            slot_ids = ["program_overview", "contact_information"]

    slot_ids = _dedupe_slot_ids(slot_ids)
    schema_id = "student_general"
    if asks_competitiveness:
        schema_id = "competitiveness_assessment"
    elif has_research:
        schema_id = "researcher_discovery"
    elif has_visa:
        schema_id = "visa_and_compliance"
    elif has_finance:
        schema_id = "financial_planning"
    elif has_admission:
        schema_id = "admissions_profile"
    elif has_university_context:
        schema_id = "program_overview"

    required_slots = [_slot(slot_id) for slot_id in slot_ids]
    return {
        "schema_id": schema_id,
        "required_slots": required_slots,
        "required_slot_ids": [row["slot_id"] for row in required_slots],
    }


def required_answer_fields_from_schema(prompt: str, *, intent: str = "") -> list[str]:
    resolved = resolve_question_schema(prompt, intent=intent)
    fields: list[str] = []
    seen: set[str] = set()
    for slot in resolved.get("required_slots", []):
        if not isinstance(slot, dict):
            continue
        alias = _normalize(str(slot.get("answer_field_alias", "")))
        if not alias or alias in seen:
            continue
        seen.add(alias)
        fields.append(alias)
    return fields
