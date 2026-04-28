import pytest

from app.services import unigraph_research as service


def test_canonicalize_url_removes_tracking_params_and_fragment():
    assert (
        service.canonicalize_url("https://www.tum.de/en/studies?utm_source=x&fbclid=y&id=42#apply")
        == "https://www.tum.de/en/studies?id=42"
    )


def test_select_urls_filters_low_quality_and_prefers_official_pdf():
    plan = service.QueryPlan(
        priority_sources=["tum.de", "daad.de"],
        search_queries=[{"query": "tum data engineering deadline", "priority": 1.0}],
    )
    rows = [
        {
            "query": "tum data engineering deadline",
            "type": "pdf",
            "priority": 1.0,
            "results": [
                {
                    "title": "Admission requirements",
                    "link": "https://www.tum.de/file.pdf?utm_campaign=x",
                    "snippet": "application requirements",
                },
                {
                    "title": "Forum thread",
                    "link": "https://www.reddit.com/r/germany/comments/1",
                    "snippet": "deadline rumor",
                },
            ],
        }
    ]

    selected = service.select_and_deduplicate_urls(rows, plan)

    assert [item["url"] for item in selected] == ["https://www.tum.de/file.pdf"]
    assert selected[0]["source_quality"] == 0.95
    assert selected[0]["document_type"] == "pdf"


def test_fau_german_context_filters_florida_atlantic(monkeypatch):
    plan = service._with_german_fau_focus(
        service.QueryPlan(
            university_short="FAU",
            program="MSc Artificial Intelligence",
            required_info=["english_language_requirement"],
            required_fields=["english_language_requirement"],
            priority_sources=["fau.de"],
            search_queries=[{"query": "FAU MSc Artificial Intelligence IELTS", "priority": 1.0}],
        ),
        "What is the IELTS requirement for MSc Artificial Intelligence at FAU?",
    )
    rows = [
        {
            "query": "FAU MSc Artificial Intelligence IELTS",
            "type": "official_page",
            "priority": 1.0,
            "results": [
                {
                    "title": "FAU Erlangen AI language requirements",
                    "link": "https://www.fau.de/education/degree-programme/artificial-intelligence",
                    "snippet": "English language requirements IELTS",
                },
                {
                    "title": "Florida Atlantic University Artificial Intelligence",
                    "link": "https://www.fau.edu/engineering/artificial-intelligence",
                    "snippet": "Florida Atlantic University IELTS",
                },
            ],
        }
    ]
    debug = {}

    selected = service.select_and_deduplicate_urls(rows, plan, debug_collector=debug)

    assert [item["url"] for item in selected] == [
        "https://www.fau.de/education/degree-programme/artificial-intelligence"
    ]
    assert debug["skipped_urls"][0]["reason"] == "ambiguous_secondary_institution_florida_atlantic"


def test_group_and_rank_evidence_preserves_metadata_and_sections():
    plan = service.QueryPlan(
        required_info=["application_deadline", "english_language_requirement"],
        required_fields=["application_deadline", "english_language_requirement"],
        keywords=["application", "deadline", "language"],
        german_keywords=["bewerbungsfrist", "sprachnachweis"],
        search_queries=[{"query": "official deadline", "priority": 1.0}],
    )
    extracted = [
        service.ExtractedContent(
            url="https://www.tum.de/admissions",
            title="Admissions",
            domain="tum.de",
            source_type="official_university_page",
            document_type="html",
            source_quality=0.95,
            retrieved_at="2026-04-28T00:00:00+00:00",
            query="official deadline",
            pages=[
                service.ExtractedPage(
                    text="The application deadline is 31 May. IELTS is accepted as language proof."
                )
            ],
        )
    ]

    grouped = service.group_and_rank_evidence(extracted, plan)
    selected = service.fan_in_evidence(grouped)

    assert "application_deadline" in grouped
    assert "english_language_requirement" in grouped
    assert selected
    assert selected[0].url == "https://www.tum.de/admissions"
    assert selected[0].source_type == "official_university_page"


def test_language_query_fan_in_excludes_unrelated_admissions_chunks():
    plan = service.QueryPlan(
        required_info=["english_language_requirement"],
        required_fields=["english_language_requirement"],
        keywords=["IELTS", "English proficiency"],
        search_queries=[{"query": "official IELTS", "priority": 1.0}],
    )
    extracted = [
        service.ExtractedContent(
            url="https://www.fau.de/ai",
            title="AI",
            domain="fau.de",
            source_type="official_university_page",
            document_type="html",
            source_quality=0.95,
            retrieved_at="2026-04-28T00:00:00+00:00",
            query="official IELTS",
            pages=[
                service.ExtractedPage(
                    text=(
                        "English proficiency must be proven at CEFR level B2. IELTS and TOEFL "
                        "can be used as English language proof. Tuition fees are listed elsewhere. "
                        "The programme duration is four semesters and transcripts are required."
                    )
                ),
                service.ExtractedPage(
                    text=(
                        "Tuition fee is 1500 EUR. GPA, transcript evaluation, GRE, documents, "
                        "and programme duration are described on this page."
                    )
                ),
            ],
        )
    ]

    grouped = service.group_and_rank_evidence(extracted, plan)
    selected = service.fan_in_evidence(grouped)

    assert selected
    assert all("tuition fee is 1500" not in chunk.text.lower() for chunk in selected)
    assert any("english proficiency" in chunk.text.lower() for chunk in selected)


def test_field_level_confidence_reports_missing_numeric_ielts():
    plan = service.QueryPlan(
        required_info=["english_language_requirement"],
        required_fields=["english_language_requirement"],
    )
    grouped = {
        "english_language_requirement": [
            service.EvidenceChunk(
                text="English proficiency must be proven at CEFR level B2.",
                url="https://www.fau.de/ai",
                title="AI",
                domain="fau.de",
                source_type="official_university_page",
                document_type="html",
                retrieved_at="2026-04-28T00:00:00+00:00",
                query="q",
                score=0.9,
                section="english_language_requirement",
                scoring={"source_quality": 0.95},
            )
        ]
    }

    confidence = service._field_level_confidence("IELTS requirement for FAU AI", plan, grouped)

    assert confidence["English B2 requirement"] == "high"
    assert confidence["numeric IELTS score"] == "not verified"
    assert confidence["per-section IELTS score"] == "not verified"


def test_fallback_plan_detects_deadline_intent_fields_and_exclusions():
    plan = service._fallback_plan(
        "What is the application deadline for MSc Data Science at University of Mannheim?"
    )

    assert plan.intent == "deadline_lookup"
    assert "application_deadline" in plan.required_fields
    assert "intake_or_semester" in plan.required_fields
    assert "application_process" in plan.optional_fields
    assert "ielts_score" in plan.excluded_fields
    assert "tuition_fee" in plan.excluded_fields


def test_fallback_plan_detects_document_intent_fields():
    plan = service._fallback_plan(
        "What documents are required for international students applying to TU Munich MSc Informatics?"
    )

    assert plan.intent == "document_requirement_lookup"
    assert "required_application_documents" in plan.required_fields
    assert "language_proof" in plan.required_fields
    assert "degree_transcript_requirements" in plan.required_fields
    assert "aps_requirement" in plan.optional_fields
    assert "tuition_fee" in plan.excluded_fields


def test_deadline_evidence_filtering_excludes_unrelated_language_and_tuition():
    plan = service._fallback_plan(
        "What is the application deadline for MSc Data Science at University of Mannheim?"
    )
    extracted = [
        service.ExtractedContent(
            url="https://www.uni-mannheim.de/apply",
            title="Apply",
            domain="uni-mannheim.de",
            source_type="official_university_page",
            document_type="html",
            source_quality=0.95,
            retrieved_at="2026-04-28T00:00:00+00:00",
            query=plan.search_queries[0]["query"],
            pages=[
                service.ExtractedPage(
                    text=(
                        "The application deadline for the winter semester is 31 May "
                        "for international applicants."
                    )
                ),
                service.ExtractedPage(
                    text=(
                        "IELTS 6.5 is accepted. Tuition fee information and GPA are "
                        "described elsewhere."
                    )
                ),
            ],
        )
    ]
    debug = {}

    grouped = service.group_and_rank_evidence(extracted, plan, debug_collector=debug)
    selected = service.fan_in_evidence(grouped)

    assert selected
    assert all(
        chunk.field
        in {
            "application_deadline",
            "intake_or_semester",
            "applicant_category",
            "application_process",
        }
        for chunk in selected
    )
    assert all("ielts 6.5" not in chunk.text.lower() for chunk in selected)
    assert debug["excluded_evidence_chunks"]


def test_selected_evidence_debug_includes_field_mapping_metadata():
    chunk = service.EvidenceChunk(
        text="The application deadline is 31 May.",
        url="https://www.uni-mannheim.de/apply",
        title="Apply",
        domain="uni-mannheim.de",
        source_type="official_university_page",
        document_type="html",
        retrieved_at="2026-04-28T00:00:00+00:00",
        query="deadline",
        score=0.9,
        section="application_deadline",
        field="application_deadline",
        support_level="direct",
        selection_reason="matched keywords for application_deadline",
    )

    payload = service._chunk_debug_payload(chunk)

    assert payload["field"] == "application_deadline"
    assert payload["support_level"] == "direct"
    assert payload["selection_reason"] == "matched keywords for application_deadline"


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_returns_compatibility_dict(monkeypatch):
    result = service.ResearchResult(
        query="q",
        answer="answer [E1]",
        evidence_chunks=[
            service.EvidenceChunk(
                text="Official application deadline evidence.",
                url="https://www.tum.de/deadline",
                title="Deadline",
                domain="tum.de",
                source_type="official_university_page",
                document_type="html",
                retrieved_at="2026-04-28T00:00:00+00:00",
                query="deadline",
                score=0.9,
                section="application_deadline",
            )
        ],
        query_plan=service.QueryPlan(required_info=["application_deadline"]),
        debug_info={"final_confidence": 0.9, "fields_not_verified": []},
    )

    async def _fake_research(_query):
        return result

    monkeypatch.setattr(service, "research_university_question", _fake_research)

    payload = await service.aretrieve_web_chunks("q")

    assert payload["results"][0]["metadata"]["source_type"] == "official_university_page"
    assert payload["coverage_ledger"][0]["status"] == "found"
    assert payload["web_retrieval_verified"] is True
    assert "debug" not in payload


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_includes_debug_only_when_requested(monkeypatch):
    result = service.ResearchResult(
        query="q",
        answer="answer [E1]",
        evidence_chunks=[],
        query_plan=service.QueryPlan(required_info=["application_deadline"]),
        debug_info={
            "query_decomposition": {"university": "TUM"},
            "generated_search_queries": [{"query": "tum deadline"}],
            "raw_search_results": [],
            "skipped_urls": [],
            "final_confidence": 0.2,
            "fields_not_verified": ["application_deadline"],
        },
    )

    async def _fake_research(_query):
        return result

    monkeypatch.setattr(service, "research_university_question", _fake_research)

    normal_payload = await service.aretrieve_web_chunks("q")
    debug_payload = await service.aretrieve_web_chunks("q", debug=True)

    assert "debug" not in normal_payload
    assert debug_payload["debug"]["query_decomposition"]["university"] == "TUM"
