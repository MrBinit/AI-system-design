import os

os.environ.setdefault("SECURITY_JWT_SECRET", "unit-test-secret-1234567890-abcdefghijklmnopqrstuvwxyz")

from app.services.german_source_policy import (
    TIER0_OFFICIAL,
    TIER1_CORROBORATION,
    TIER2_DISCOVERY,
    classify_german_source,
    discover_official_domains,
    validate_german_program_scope,
)


def test_classifies_official_german_university_domain():
    result = classify_german_source(
        "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/masters-program-in-business-informatics",
        title="Master's Program in Business Informatics | University of Mannheim",
        institution="University of Mannheim",
    )

    assert result["source_tier"] == TIER0_OFFICIAL
    assert result["source_type"] == "official"


def test_classifies_daad_as_corroboration_not_university_official():
    result = classify_german_source(
        "https://www2.daad.de/deutschland/studienangebote/international-programmes/en/detail/10610/",
        title="Business Informatics",
    )

    assert result["source_tier"] == TIER1_CORROBORATION


def test_blocks_third_party_discovery_domains_from_final_official_tier():
    result = classify_german_source(
        "https://edu-link.de/university-of-mannheim-business-informatics",
        title="University of Mannheim Business Informatics",
        institution="University of Mannheim",
    )

    assert result["source_tier"] == TIER2_DISCOVERY


def test_discovers_official_domains_from_search_rows():
    rows = [
        {
            "title": "Study portal listing",
            "url": "https://edu-link.de/program",
            "snippet": "Third-party listing",
        },
        {
            "title": "Master's Program in Business Informatics | University of Mannheim",
            "url": "https://www.uni-mannheim.de/en/academics/programs/business-informatics",
            "snippet": "Official University of Mannheim page",
        },
    ]

    assert discover_official_domains(rows, institution="University of Mannheim") == [
        "uni-mannheim.de"
    ]


def test_rejects_bachelor_page_for_master_query_scope():
    result = validate_german_program_scope(
        "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/bsc-business-informatics/",
        title="Bachelor's Program in Business Informatics | University of Mannheim",
        program="MSc Business Informatics",
        degree_level="master",
    )

    assert result["accepted"] is False
    assert result["reason"] == "degree_level_mismatch_bachelor_source"


def test_rejects_unrelated_program_pdf_for_program_specific_scope():
    result = validate_german_program_scope(
        "https://www.uni-mannheim.de/media/Universitaet/Dokumente/Pruefungsordnungen/msc_wim/PO_MSc_MMDS_2024_en.pdf",
        title="PO MSc Mannheim Master in Data Science",
        snippet="Mannheim Master in Data Science examination regulations",
        program="MSc Business Informatics",
        degree_level="master",
    )

    assert result["accepted"] is False
    assert result["reason"] == "program_mismatch_program_specific_source"


def test_rejects_exchange_student_application_page_for_degree_applicant_scope():
    result = validate_german_program_scope(
        "https://www.uni-mannheim.de/en/academics/coming-to-mannheim/exchange-students/application/",
        title="Application for exchange students",
        program="MSc Business Informatics",
        degree_level="master",
    )

    assert result["accepted"] is False
    assert result["reason"] == "audience_mismatch_exchange_student_source"
