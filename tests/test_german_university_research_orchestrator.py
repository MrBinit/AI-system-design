import os

os.environ.setdefault("SECURITY_JWT_SECRET", "unit-test-secret-1234567890-abcdefghijklmnopqrstuvwxyz")

import pytest

from app.services import german_university_research_orchestrator as orchestrator_module
from app.services.german_university_research_orchestrator import (
    GermanUniversityResearchOrchestrator,
)


@pytest.mark.asyncio
async def test_german_research_orchestrator_builds_official_evidence_ledger():
    seen_queries: list[str] = []

    async def fake_search_batch(queries: list[str]) -> list[dict]:
        seen_queries.extend(queries)
        responses: list[dict] = []
        for query in queries:
            lowered = query.lower()
            organic_results = []
            if "daad" in lowered:
                organic_results.append(
                    {
                        "title": "DAAD Business Informatics",
                        "link": "https://www2.daad.de/deutschland/studienangebote/international-programmes/en/detail/10610/",
                        "snippet": "Business Informatics language requirements English C1.",
                    }
                )
            elif "auswahlsatzung" in lowered or "mindestnote" in lowered or "ects" in lowered:
                organic_results.append(
                    {
                        "title": "Selection Statutes - University of Mannheim",
                        "link": "https://www.uni-mannheim.de/media/Auswahlsatzung-business-informatics.pdf",
                        "snippet": "Business Informatics Auswahlsatzung Mindestnote and ECTS.",
                    }
                )
            elif "bewerbungsfrist" in lowered or "deadline" in lowered:
                organic_results.append(
                    {
                        "title": "Application deadlines | University of Mannheim",
                        "link": "https://www.uni-mannheim.de/en/academics/applying/application-deadlines/",
                        "snippet": "Application deadline for Business Informatics: 15.05.2026.",
                    }
                )
            elif "portal" in lowered or "apply online" in lowered:
                organic_results.append(
                    {
                        "title": "Online Application | University of Mannheim",
                        "link": "https://bewerbung.uni-mannheim.de/",
                        "snippet": "Apply online through the University of Mannheim application portal.",
                    }
                )
            else:
                organic_results.append(
                    {
                        "title": "Master's Program in Business Informatics | University of Mannheim",
                        "link": "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/masters-program-in-business-informatics",
                        "snippet": "Official University of Mannheim program page. Language of instruction: English.",
                    }
                )
            responses.append({"query": query, "result": {"organic_results": organic_results}})
        return responses

    async def fake_page_fetcher(rows: list[dict]) -> dict[str, dict]:
        output: dict[str, dict] = {}
        for row in rows:
            url = row["url"]
            if url.endswith(".pdf"):
                content = (
                    "Auswahlsatzung Business Informatics. Mindestnote 2.5. "
                    "Applicants need 36 ECTS in informatics and 18 ECTS in business administration. "
                    "Selection criteria use grade and subject credits."
                )
            elif "application-deadlines" in url:
                content = "Application deadline for MSc Business Informatics: 15.05.2026."
            elif "bewerbung.uni-mannheim.de" in url:
                content = "Application portal: https://bewerbung.uni-mannheim.de/ for online application."
            else:
                content = (
                    "Master's Program in Business Informatics. Degree: Master of Science. "
                    "Language of instruction: English. Language requirements: English C1. "
                    "Semester fee: EUR 194."
                )
            output[url] = {"content": content, "url": url}
        return output

    orchestrator = GermanUniversityResearchOrchestrator(
        search_batch=fake_search_batch,
        page_fetcher=fake_page_fetcher,
    )
    result = await orchestrator.research(
        "Tell me about University of Mannheim MSc Business Informatics: "
        "language, IELTS/German requirement, GPA and ECTS requirements, "
        "application deadline, portal, and whether it is competitive."
    )

    assert result["applicable"] is True
    ledger = result["coverage_ledger"]
    found_ids = {row["id"] for row in ledger if row["status"] == "found"}
    assert "language_of_instruction" in found_ids
    assert "gpa_or_grade_threshold" in found_ids
    assert "ects_or_subject_credit_requirements" in found_ids
    assert "application_deadline" in found_ids
    assert "application_portal" in found_ids
    assert "selection_criteria" in found_ids
    assert result["verification"]["required_field_coverage"] >= 0.7
    assert any("site:uni-mannheim.de" in query.lower() for query in seen_queries)
    assert all(
        "edu-link.de" not in str((item.get("metadata") or {}).get("url", ""))
        for item in result["results"]
    )


@pytest.mark.asyncio
async def test_german_research_orchestrator_follows_official_links_and_rejects_wrong_scope():
    async def fake_search_batch(queries: list[str]) -> list[dict]:
        responses: list[dict] = []
        for query in queries:
            responses.append(
                {
                    "query": query,
                    "result": {
                        "organic_results": [
                            {
                                "title": "Master's Program in Business Informatics | University of Mannheim",
                                "link": "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/masters-program-in-business-informatics/",
                                "snippet": "Official University of Mannheim program page. Language of instruction: English.",
                            },
                            {
                                "title": "Bachelor's Program in Business Informatics | University of Mannheim",
                                "link": "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/bsc-business-informatics/",
                                "snippet": "German C1 is required for the bachelor's program.",
                            },
                            {
                                "title": "PO MSc Mannheim Master in Data Science",
                                "link": "https://www.uni-mannheim.de/media/Universitaet/Dokumente/Pruefungsordnungen/msc_wim/PO_MSc_MMDS_2024_en.pdf",
                                "snippet": "Mannheim Master in Data Science 120 ECTS.",
                            },
                            {
                                "title": "Application for exchange students",
                                "link": "https://www.uni-mannheim.de/en/academics/coming-to-mannheim/exchange-students/application/",
                                "snippet": "Online application for exchange students.",
                            },
                        ]
                    },
                }
            )
        return responses

    async def fake_page_fetcher(rows: list[dict]) -> dict[str, dict]:
        output: dict[str, dict] = {}
        for row in rows:
            url = row["url"]
            if url.endswith("masters-program-in-business-informatics/"):
                output[url] = {
                    "url": url,
                    "content": (
                        "Master's Program in Business Informatics. Degree: Master of Science. "
                        "Language of instruction: English. Language requirements: English."
                    ),
                    "links": [
                        {
                            "url": "https://www.uni-mannheim.de/media/Auswahlsatzung-business-informatics.pdf",
                            "title": "Admission requirements and selection",
                            "snippet": "Auswahlsatzung Business Informatics",
                        },
                        {
                            "url": "https://www.uni-mannheim.de/en/academics/dates/application-deadlines/",
                            "title": "Application deadlines",
                            "snippet": "Bewerbungsfrist",
                        },
                        {
                            "url": "https://bewerbung.uni-mannheim.de/",
                            "title": "Online application",
                            "snippet": "Application portal",
                        },
                    ],
                }
            elif url.endswith("Auswahlsatzung-business-informatics.pdf"):
                output[url] = {
                    "url": url,
                    "content": (
                        "Auswahlsatzung Business Informatics. IELTS 6.5 or TOEFL iBT 90. "
                        "Mindestnote 2.5. Applicants need 36 ECTS in informatics and "
                        "18 ECTS in business administration. Selection criteria use grade and ECTS."
                    ),
                }
            elif "application-deadlines" in url:
                output[url] = {
                    "url": url,
                    "content": "Application deadline for MSc Business Informatics: 15.05.2026.",
                }
            elif "bewerbung.uni-mannheim.de" in url:
                output[url] = {
                    "url": url,
                    "content": "Application portal: https://bewerbung.uni-mannheim.de/ for online application.",
                }
            else:
                output[url] = {"url": url, "content": "Wrong source should not be used."}
        return output

    orchestrator = GermanUniversityResearchOrchestrator(
        search_batch=fake_search_batch,
        page_fetcher=fake_page_fetcher,
    )
    result = await orchestrator.research(
        "Tell me about University of Mannheim MSc Business Informatics: "
        "IELTS/German requirement, GPA and ECTS requirements, application deadline, portal."
    )

    ledger = result["coverage_ledger"]
    source_urls = {
        row["source_url"]
        for row in ledger
        if row["status"] == "found" and row.get("source_url")
    }

    assert "https://www.uni-mannheim.de/media/Auswahlsatzung-business-informatics.pdf" in source_urls
    assert "https://www.uni-mannheim.de/en/academics/dates/application-deadlines/" in source_urls
    assert "https://bewerbung.uni-mannheim.de/" in source_urls
    assert all("bsc-business-informatics" not in url for url in source_urls)
    assert all("PO_MSc_MMDS" not in url for url in source_urls)
    assert all("exchange-students" not in url for url in source_urls)


@pytest.mark.asyncio
async def test_german_research_orchestrator_fetches_generic_linked_pages_before_scope_rejecting():
    async def fake_search_batch(queries: list[str]) -> list[dict]:
        return [
            {
                "query": query,
                "result": {
                    "organic_results": [
                        {
                            "title": "Master's Program in Business Informatics | University of Mannheim",
                            "link": "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/masters-program-in-business-informatics/",
                            "snippet": "Official program page.",
                        }
                    ]
                },
            }
            for query in queries
        ]

    async def fake_page_fetcher(rows: list[dict]) -> dict[str, dict]:
        output: dict[str, dict] = {}
        for row in rows:
            url = row["url"]
            if url.endswith("masters-program-in-business-informatics/"):
                output[url] = {
                    "url": url,
                    "content": (
                        "Master's Program in Business Informatics. Language of instruction: English. "
                        "Admission requirements and selection are listed on the master's criteria page."
                    ),
                    "links": [
                        {
                            "url": "https://www.uni-mannheim.de/en/academics/before-your-studies/applying/the-a-to-z-of-applying/masters-programs-admission-criteria/",
                            "title": "Admission requirements and selection",
                            "snippet": "Master's programs admission criteria",
                        },
                        {
                            "url": "https://www.uni-mannheim.de/en/academics/dates/application-deadlines/",
                            "title": "Application deadlines",
                            "snippet": "Master deadlines",
                        },
                    ],
                }
            elif "admission-criteria" in url:
                output[url] = {
                    "url": url,
                    "content": (
                        "Master's Program in Business Informatics Admission requirements. "
                        "Applicants need at least 30 ECTS credits in informatics, 30 ECTS credits "
                        "in business or business informatics and 18 ECTS credits in mathematics or statistics. "
                        "Selection criteria: the final grade of the bachelor's program and semester abroad."
                    ),
                    "links": [
                        {
                            "url": "https://www.uni-mannheim.de/en/academics/before-your-studies/applying/the-a-to-z-of-applying/masters-programs-foreign-language-requirements/",
                            "title": "proficiency in English",
                            "snippet": "Foreign language requirements",
                        }
                    ],
                }
            elif "foreign-language-requirements" in url:
                output[url] = {
                    "url": url,
                    "content": (
                        "Master's Program in Business Informatics Solid knowledge of English. "
                        "TOEFL Internet Based Test (TOEFL iBT) with a score of at least 72 points. "
                        "IELTS (Academic Test) with a band score of 6.0 or better."
                    ),
                }
            elif "application-deadlines" in url:
                output[url] = {
                    "url": url,
                    "content": (
                        "Application deadline Master / Master of Education. "
                        "Business Informatics (taught in English) 1 April – 15 May 15 October – 15 November."
                    ),
                }
            else:
                output[url] = {"url": url, "content": ""}
        return output

    orchestrator = GermanUniversityResearchOrchestrator(
        search_batch=fake_search_batch,
        page_fetcher=fake_page_fetcher,
    )
    result = await orchestrator.research(
        "Tell me about University of Mannheim MSc Business Informatics: "
        "IELTS, ECTS requirements, deadline and selection criteria."
    )

    found = {row["id"]: row for row in result["coverage_ledger"] if row["status"] == "found"}
    assert "language_test_score_thresholds" in found
    assert "ects_or_subject_credit_requirements" in found
    assert "application_deadline" in found
    assert "selection_criteria" in found
    assert "15 November" in found["application_deadline"]["value"]


@pytest.mark.asyncio
async def test_german_research_orchestrator_not_applicable_for_non_university_query():
    orchestrator = GermanUniversityResearchOrchestrator(
        search_batch=lambda _queries: None,  # type: ignore[arg-type]
        page_fetcher=lambda _rows: None,  # type: ignore[arg-type]
    )

    result = await orchestrator.research("What is the weather in Berlin?")

    assert result["applicable"] is False
    assert result["results"] == []


@pytest.mark.asyncio
async def test_german_research_orchestrator_respects_query_budget(monkeypatch):
    seen_queries: list[str] = []

    monkeypatch.setattr(orchestrator_module.settings.web_search, "german_research_total_query_budget", 6)
    monkeypatch.setattr(orchestrator_module.settings.web_search, "german_research_discovery_max_queries", 2)
    monkeypatch.setattr(orchestrator_module.settings.web_search, "german_research_route_max_queries", 2)
    monkeypatch.setattr(orchestrator_module.settings.web_search, "german_research_rescue_max_queries", 2)

    async def fake_search_batch(queries: list[str]) -> list[dict]:
        seen_queries.extend(queries)
        return [
            {
                "query": query,
                "result": {
                    "organic_results": [
                        {
                            "title": "Master's Program in Business Informatics | University of Mannheim",
                            "link": "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/masters-program-in-business-informatics/",
                            "snippet": "Official page.",
                        }
                    ]
                },
            }
            for query in queries
        ]

    async def fake_page_fetcher(rows: list[dict]) -> dict[str, dict]:
        return {
            str(row.get("url", "")): {
                "url": str(row.get("url", "")),
                "content": "Language of instruction: English.",
            }
            for row in rows
        }

    orchestrator = GermanUniversityResearchOrchestrator(
        search_batch=fake_search_batch,
        page_fetcher=fake_page_fetcher,
    )
    result = await orchestrator.research(
        "Tell me about University of Mannheim MSc Business Informatics language, GPA, ECTS, deadline and portal."
    )

    budget_usage = result["retrieval_budget_usage"]
    assert budget_usage["query_budget"] == 6
    assert budget_usage["queries_executed"] <= budget_usage["query_budget"]
    assert len(seen_queries) == budget_usage["queries_executed"]
    assert budget_usage["stage_usage"]["discovery"] <= budget_usage["stage_caps"]["discovery"]
    assert budget_usage["stage_usage"]["route"] <= budget_usage["stage_caps"]["route"]
    assert budget_usage["stage_usage"]["rescue"] <= budget_usage["stage_caps"]["rescue"]


@pytest.mark.asyncio
async def test_german_research_orchestrator_exposes_slot_first_query_plan():
    async def fake_search_batch(queries: list[str]) -> list[dict]:
        return [
            {
                "query": query,
                "result": {
                    "organic_results": [
                        {
                            "title": "Master's Program in Business Informatics | University of Mannheim",
                            "link": "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/masters-program-in-business-informatics/",
                            "snippet": "Official page. Language of instruction: English.",
                        }
                    ]
                },
            }
            for query in queries
        ]

    async def fake_page_fetcher(rows: list[dict]) -> dict[str, dict]:
        return {
            row["url"]: {
                "url": row["url"],
                "content": "Master's Program in Business Informatics. Language of instruction: English.",
            }
            for row in rows
        }

    orchestrator = GermanUniversityResearchOrchestrator(
        search_batch=fake_search_batch,
        page_fetcher=fake_page_fetcher,
    )
    result = await orchestrator.research(
        "Tell me about University of Mannheim MSc Business Informatics language, GPA, ECTS, deadline and portal."
    )

    slot_plan = result["query_plan"]["slot_query_plan"]
    assert "language_of_instruction" in slot_plan
    assert "gpa_or_grade_threshold" in slot_plan
    assert "application_deadline" in slot_plan
    assert result["source_routes_attempted"]["slot_query_plan"] == slot_plan
