import os

os.environ.setdefault("SECURITY_JWT_SECRET", "unit-test-secret-1234567890-abcdefghijklmnopqrstuvwxyz")

from app.services.german_source_routes import (
    build_slot_route_queries,
    is_likely_german_university_query,
    resolve_german_research_task,
)


def test_resolve_german_research_task_extracts_generic_institution_and_slots():
    task = resolve_german_research_task(
        "Tell me about University of Hamburg MSc Data Science: IELTS, GPA, ECTS, deadline, portal"
    )

    assert task.institution == "University of Hamburg"
    assert task.degree_level == "master"
    assert "language_test_score_thresholds" in task.required_slots
    assert "gpa_or_grade_threshold" in task.required_slots
    assert "application_deadline" in task.required_slots
    assert "application_portal" in task.required_slots


def test_german_route_generation_uses_domains_without_university_hardcoding():
    task = resolve_german_research_task(
        "University of Bremen Master Space Engineering admission deadline and language requirements"
    )
    queries = build_slot_route_queries(
        task,
        official_domains=["uni-bremen.de"],
        max_queries=20,
    )

    joined = "\n".join(queries).lower()
    assert "site:uni-bremen.de" in joined
    assert "bewerbungsfrist" in joined or "application deadline" in joined
    assert "sprachnachweis" in joined or "language requirements" in joined
    assert "auswahlsatzung" in joined


def test_likely_german_university_query_detection():
    assert is_likely_german_university_query(
        "RWTH Aachen MSc Computer Science application deadline and ECTS requirements"
    )
    assert not is_likely_german_university_query("What is the weather in Berlin tomorrow?")

