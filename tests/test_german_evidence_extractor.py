import os

os.environ.setdefault("SECURITY_JWT_SECRET", "unit-test-secret-1234567890-abcdefghijklmnopqrstuvwxyz")

from app.services.german_evidence_extractor import (
    classify_source_page_type,
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


def test_extracts_program_section_from_long_mannheim_style_pages():
    nav_noise = " ".join(f"Navigation item {index}" for index in range(250))
    language_page = (
        f"{nav_noise} Master's Program in Economics Solid knowledge of English "
        "TOEFL Internet Based Test with a score of at least 72 points IELTS 6.0 "
        "Master's Program in Business Informatics Solid knowledge of English "
        "TOEFL Internet Based Test (TOEFL iBT) with a score of at least 72 points "
        "IELTS (Academic Test) with a band score of 6.0 or better "
        "Language Certificate at Level B2 throughout "
        "The proof can be submitted until 15 August for fall and 15 January for spring."
    )
    criteria_page = (
        f"{nav_noise} Master's Program in Business Informatics Admission requirements "
        "If you have not yet completed your bachelor's degree, you may still apply as long as "
        "you provide proof that you have obtained at least 130 ECTS credits. Completion of a "
        "bachelor's program in Business Informatics or equivalent, corresponding to at least "
        "180 ECTS credits. Equivalent if it includes at least 30 ECTS credits in informatics, "
        "30 ECTS credits in business or business informatics and 18 ECTS credits in mathematics "
        "or statistics. At least 8 ECTS credits in programming. Selection criteria: The final "
        "grade of the bachelor's program, professional activities, semester abroad."
    )
    deadline_page = (
        f"{nav_noise} Application deadline Master / Master of Education Master's Program "
        "Fall Semester 2026/2027 Spring Semester 2026 Business Informatics (taught in English) "
        "1 April – 15 May 15 October – 15 November Competition Law and Regulation 1 April – 31 May"
    )

    rows = extract_german_evidence_rows(
        [
            {
                "title": "Master's Programs - Foreign Language Requirements",
                "url": "https://www.uni-mannheim.de/en/academics/before-your-studies/applying/the-a-to-z-of-applying/masters-programs-foreign-language-requirements/",
                "content": language_page,
            },
            {
                "title": "Master's Programs - Admission criteria",
                "url": "https://www.uni-mannheim.de/en/academics/before-your-studies/applying/the-a-to-z-of-applying/masters-programs-admission-criteria/",
                "content": criteria_page,
            },
            {
                "title": "Application Deadlines",
                "url": "https://www.uni-mannheim.de/en/academics/dates/application-deadlines/",
                "content": deadline_page,
            },
        ],
        required_slots=(
            "language_test_score_thresholds",
            "ects_or_subject_credit_requirements",
            "application_deadline",
            "selection_criteria",
        ),
        institution="University of Mannheim",
        program="MSc Business Informatics",
    )

    found = {row["id"]: row for row in rows if row["status"] == "found"}
    assert "language_test_score_thresholds" in found
    assert "72" in found["language_test_score_thresholds"]["value"] or "6.0" in found["language_test_score_thresholds"]["value"]
    assert "ects_or_subject_credit_requirements" in found
    assert "application_deadline" in found
    assert "1 April" in found["application_deadline"]["value"]
    assert "15 November" in found["application_deadline"]["value"]
    assert "selection_criteria" in found


def test_rejects_daad_ranking_category_text_as_german_language_requirement():
    rows = extract_german_evidence_rows(
        [
            {
                "title": "CHE Ranking Department Detail",
                "url": "https://www.daad.de/en/studying-in-germany/universities/che-ranking/?che-a=department-detail",
                "snippet": "Architecture Biochemistry Biology Business Informatics German Language and Literature",
                "content": (
                    "Architecture Biochemistry Biology Business Informatics German Language "
                    "and Literature Industrial Engineering Mathematics Political Science."
                ),
            }
        ],
        required_slots=("german_language_requirement",),
        institution="University of Mannheim",
        program="MSc Business Informatics",
    )

    assert rows[0]["status"] == "missing"


def test_prerequisite_ects_does_not_use_total_program_ects():
    rows = extract_german_evidence_rows(
        [
            {
                "title": "Master's Program in Business Informatics",
                "url": "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/masters-program-in-business-informatics/",
                "content": (
                    "Degree: Master of Science. Standard period of study: 4 semesters. "
                    "ECTS credits: 120. Language of instruction: English."
                ),
            },
            {
                "title": "Master's Programs Admission Criteria",
                "url": "https://www.uni-mannheim.de/en/academics/before-your-studies/applying/the-a-to-z-of-applying/masters-programs-admission-criteria/",
                "content": (
                    "Master's Program in Business Informatics Admission requirements. "
                    "Applicants need at least 30 ECTS credits in informatics, 30 ECTS credits "
                    "in business or business informatics and 18 ECTS credits in mathematics "
                    "or statistics. At least 8 ECTS credits in programming."
                ),
            },
        ],
        required_slots=("ects_or_subject_credit_requirements",),
        institution="University of Mannheim",
        program="MSc Business Informatics",
    )

    assert rows[0]["status"] == "found"
    assert "120 ECTS" not in rows[0]["value"]
    assert "30 ECTS" in rows[0]["value"]


def test_rejects_low_module_ects_without_admission_context():
    rows = extract_german_evidence_rows(
        [
            {
                "title": "MSc Business Informatics Organizing your studies",
                "url": "https://www.wim.uni-mannheim.de/en/academics/organizing-your-studies/msc-business-informatics/",
                "content": (
                    "MSc Business Informatics module requirements. Students complete seminars "
                    "worth 2 ECTS, projects worth 4 ECTS, and electives worth 12 ECTS."
                ),
            }
        ],
        required_slots=("ects_or_subject_credit_requirements",),
        institution="University of Mannheim",
        program="MSc Business Informatics",
    )

    assert rows[0]["status"] == "missing"


def test_mannheim_business_informatics_prefers_current_html_over_pdf_noise():
    program_page = {
        "title": "Master's Program in Business Informatics | University of Mannheim",
        "url": "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/masters-program-in-business-informatics/",
        "content": (
            "Master's Program in Business Informatics Degree: Master of Science. "
            "Language of instruction: English. Language requirements: English; for further "
            "information see Admission requirements and selection. Admission requirements. "
            "If you have not yet completed your bachelor's degree, you may still apply as long "
            "as you provide proof that you have obtained at least 130 ECTS credits. Completion "
            "of a bachelor's program in Business Informatics or equivalent, corresponding to at "
            "least 180 ECTS credits. A program of study is recognized as equivalent if it includes "
            "at least 30 ECTS credits in the field of informatics, 30 ECTS credits in the field of "
            "business or business informatics and 18 ECTS credits in the field of mathematics or "
            "statistics. At least 8 ECTS credits in the field of informatics must have been "
            "completed in programming. Proof of proficiency in English. Selection criteria: "
            "The final grade or grade average of the bachelor's program, professional activities, "
            "and semester abroad. Application deadlines. For spring semesters, application is "
            "possible from 15 October until 15 November. Apply now!"
        ),
    }
    language_page = {
        "title": "Master's Programs - Foreign Language Requirements",
        "url": "https://www.uni-mannheim.de/en/academics/before-your-studies/applying/the-a-to-z-of-applying/masters-programs-foreign-language-requirements/",
        "content": (
            "Master's Program in Economics Solid knowledge of English TOEFL Internet Based Test "
            "with a score of at least 72 points IELTS 6.0. Master's Program in Business Informatics "
            "Solid knowledge of English. A first degree that was completed with at least 50 percent "
            "of English as a language of instruction and examination. TOEFL Internet Based Test "
            "(TOEFL iBT) with a score of at least 72 points. IELTS (Academic Test) with a band score "
            "of 6.0 or better. The proof can be submitted until 15 August for the fall semester and "
            "15 January for the spring semester."
        ),
    }
    contaminating_pdf = {
        "title": "PO MSc Wifo 2011 EN",
        "url": "https://www.uni-mannheim.de/media/Universitaet/Dokumente/Pruefungsordnungen/msc_wim/PO_MSc_Wifo_2011_EN.pdf",
        "content": (
            "If you do not have a German university entrance qualification, you must prove German "
            "language proficiency at C1 level. Curriculum includes 24 ECTS credits in an unrelated "
            "module area. Application deadline: 1 June – 15 July; 1 June – 31 August; 1 June – 31 August."
        ),
    }

    rows = extract_german_evidence_rows(
        [program_page, language_page, contaminating_pdf],
        required_slots=(
            "language_of_instruction",
            "language_requirements",
            "language_test_score_thresholds",
            "german_language_requirement",
            "ects_or_subject_credit_requirements",
            "application_deadline",
            "selection_criteria",
            "competitiveness_signal",
        ),
        institution="University of Mannheim",
        program="MSc Business Informatics",
    )
    by_id = {row["id"]: row for row in rows}

    assert by_id["language_of_instruction"]["value"].lower() == "english"
    assert by_id["language_test_score_thresholds"]["status"] == "found"
    assert "72" in by_id["language_test_score_thresholds"]["value"]
    assert "6.0" in by_id["language_test_score_thresholds"]["value"]
    assert by_id["german_language_requirement"]["status"] == "missing"
    assert "24 ECTS" not in by_id["ects_or_subject_credit_requirements"]["value"]
    assert "30 ECTS" in by_id["ects_or_subject_credit_requirements"]["value"]
    assert "18 ECTS" in by_id["ects_or_subject_credit_requirements"]["value"]
    assert "15 October" in by_id["application_deadline"]["value"]
    assert "15 November" in by_id["application_deadline"]["value"]
    assert "final grade" in by_id["selection_criteria"]["value"].lower()
    assert "final grade" in by_id["competitiveness_signal"]["value"].lower()


def test_classifies_expected_german_admissions_source_page_types():
    assert (
        classify_source_page_type(
            {
                "url": "https://www.uni-mannheim.de/en/academics/dates/application-deadlines/",
                "title": "Application deadlines",
            }
        )
        == "deadline_table"
    )
    assert (
        classify_source_page_type(
            {
                "url": "https://portal2.uni-mannheim.de/portal2/pages/cs/sys/portal/hisinoneStartPage.faces",
                "title": "Online application",
            }
        )
        == "application_portal"
    )


def test_rejects_program_ambassador_page_for_critical_admissions_facts():
    rows = extract_german_evidence_rows(
        [
            {
                "title": "Master's Program in Business Informatics - Program Ambassadors",
                "url": "https://www.uni-mannheim.de/en/academics/advice-and-services/services-for-prospective-students/program-ambassadors/masters-program-in-business-informatics/",
                "content": (
                    "Hi, I'm Yannick and I've been enrolled in the master's program in "
                    "Business Informatics since the fall semester 2024. Language of instruction: English."
                ),
            }
        ],
        required_slots=("language_of_instruction",),
        institution="University of Mannheim",
        program="MSc Business Informatics",
    )

    assert rows[0]["status"] == "missing"


def test_rejects_generic_master_brochure_pdf_as_language_requirement_source():
    rows = extract_german_evidence_rows(
        [
            {
                "title": "Master brochure University of Mannheim",
                "url": "https://www.sowi.uni-mannheim.de/media/Einrichtungen/zula/Dokumente_Zula/masterbroschuere_uni_mannheim_en.pdf",
                "content": (
                    "Master's Program in Business Informatics. For some of the master's programs, "
                    "applicants have to prove German language skills. DSH passed with at least grade 2. "
                    "The GRE General Test may be required by selected programs."
                ),
            }
        ],
        required_slots=("german_language_requirement", "language_test_score_thresholds"),
        institution="University of Mannheim",
        program="MSc Business Informatics",
    )

    assert all(row["status"] == "missing" for row in rows)


def test_deadline_table_values_stop_before_neighboring_program_rows():
    rows = extract_german_evidence_rows(
        [
            {
                "title": "Application Deadlines",
                "url": "https://www.uni-mannheim.de/en/academics/dates/application-deadlines/",
                "content": (
                    "Application deadline Master / Master of Education Master's Program "
                    "Fall Semester 2026/2027 Spring Semester 2026 Business Informatics "
                    "(taught in English) 1 April - 15 May 15 October - 15 November "
                    "Competition Law and Regulation 1 April - 31 May Economics 1 April - 15 July"
                ),
            }
        ],
        required_slots=("application_deadline",),
        institution="University of Mannheim",
        program="MSc Business Informatics",
    )

    assert rows[0]["status"] == "found"
    assert rows[0]["value"] == "1 April - 15 May; 15 October - 15 November"
