import os

os.environ.setdefault("SECURITY_JWT_SECRET", "unit-test-secret-1234567890-abcdefghijklmnopqrstuvwxyz")

import pytest

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
async def test_german_research_orchestrator_not_applicable_for_non_university_query():
    orchestrator = GermanUniversityResearchOrchestrator(
        search_batch=lambda _queries: None,  # type: ignore[arg-type]
        page_fetcher=lambda _rows: None,  # type: ignore[arg-type]
    )

    result = await orchestrator.research("What is the weather in Berlin?")

    assert result["applicable"] is False
    assert result["results"] == []
