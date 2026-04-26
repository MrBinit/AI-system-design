from app.services import student_qa_schema_registry as registry


def test_resolve_question_schema_for_admissions_prompt():
    payload = registry.resolve_question_schema(
        "Tell me admission requirements, language of instruction, IELTS score, GPA, ECTS, deadline and portal for University of Mannheim MSc Business Informatics."
    )
    assert payload["schema_id"] == "admissions_profile"
    slot_ids = {item["slot_id"] for item in payload["required_slots"]}
    assert "eligibility_requirements" in slot_ids
    assert "instruction_language" in slot_ids
    assert "gpa_threshold" in slot_ids
    assert "ects_prerequisites" in slot_ids
    assert "language_test_score_thresholds" in slot_ids
    assert "application_deadline" in slot_ids
    assert "application_portal" in slot_ids
    assert "international_deadline" not in slot_ids
    assert "program_overview" not in slot_ids


def test_resolve_question_schema_avoids_alias_duplicates_for_competitiveness_prompt():
    payload = registry.resolve_question_schema(
        "Tell me about University of Mannheim MSc Business Informatics. "
        "Need language of instruction, IELTS/German requirement, GPA and ECTS requirements, "
        "application deadline for international students, and portal link. "
        "Also tell me if this is competitive for 3.2 GPA."
    )
    slot_ids = [item["slot_id"] for item in payload["required_slots"]]
    assert payload["schema_id"] == "competitiveness_assessment"
    assert slot_ids.count("gpa_threshold") == 1
    assert slot_ids.count("ects_prerequisites") == 1
    assert slot_ids.count("language_test_score_thresholds") == 1
    assert slot_ids.count("international_deadline") == 1
    assert "gpa_or_grade_threshold" not in slot_ids
    assert "ects_or_prerequisite_credit_breakdown" not in slot_ids
    assert "language_test_thresholds" not in slot_ids


def test_resolve_question_schema_for_research_prompt():
    payload = registry.resolve_question_schema(
        "Find professors, labs, and contact details for AI research at University of Bonn."
    )
    assert payload["schema_id"] == "researcher_discovery"
    slot_ids = {item["slot_id"] for item in payload["required_slots"]}
    assert "professors_or_supervisors" in slot_ids
    assert "labs_or_research_groups" in slot_ids
    assert "contact_information" in slot_ids


def test_required_answer_fields_from_schema_returns_aliases():
    fields = registry.required_answer_fields_from_schema(
        "What are tuition fees and scholarships for this program?"
    )
    assert "tuition_or_fees" in fields
    assert "funding_or_scholarship" in fields
