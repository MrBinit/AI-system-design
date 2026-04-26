import os

os.environ.setdefault("SECURITY_JWT_SECRET", "unit-test-secret-1234567890-abcdefghijklmnopqrstuvwxyz")

from app.services.german_evidence_extractor import (
    coverage_score,
    extract_german_evidence_rows,
    unresolved_slots,
)


def test_extracts_german_admissions_evidence_from_official_source():
    rows = extract_german_evidence_rows(
        [
            {
                "title": "Master's Program in Business Informatics | University of Mannheim",
                "url": "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/masters-program-in-business-informatics",
                "snippet": "Official program page",
                "content": (
                    "Degree: Master of Science. Standard period of study: 4 semesters. "
                    "ECTS credits: 120. Language of instruction: English. "
                    "Language requirements: English C1. Semester fee: EUR 194. "
                    "Application deadline: 15.05.2026. Apply online through the application portal."
                ),
            },
            {
                "title": "Selection statute Business Informatics",
                "url": "https://www.uni-mannheim.de/media/Auswahlsatzung-business-informatics.pdf",
                "content": (
                    "Auswahlsatzung Business Informatics. Mindestnote 2.5. "
                    "Applicants need 36 ECTS in informatics and 18 ECTS in business administration."
                ),
            },
        ],
        required_slots=(
            "language_of_instruction",
            "language_requirements",
            "gpa_or_grade_threshold",
            "ects_or_subject_credit_requirements",
            "application_deadline",
            "application_portal",
        ),
        institution="University of Mannheim",
    )

    found = {row["id"]: row for row in rows if row["status"] == "found"}
    assert "language_of_instruction" in found
    assert "gpa_or_grade_threshold" in found
    assert "ects_or_subject_credit_requirements" in found
    assert "application_deadline" in found
    assert "application_portal" in found
    assert coverage_score(rows) >= 0.8
    assert "gpa_or_grade_threshold" not in unresolved_slots(rows)

