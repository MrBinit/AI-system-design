import pytest

from app.services import web_retrieval_service as service


@pytest.fixture(autouse=True)
def _configure_defaults(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "query_planner_use_llm", False)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_use_llm", False)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_steps", 2)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_gap_queries", 2)
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "max_query_variants", 4)
    monkeypatch.setattr(service.settings.web_search, "deep_max_query_variants", 5)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])
    monkeypatch.setattr(service.settings.web_search, "official_source_filter_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "retrieval_min_unique_domains", 1)
    monkeypatch.setattr(service.settings.web_search, "deep_min_unique_domains", 1)


@pytest.mark.asyncio
async def test_professor_query_enables_research_objective_mode(monkeypatch):
    seen_queries: list[str] = []

    async def _fake_batch(queries: list[str], **_kwargs):
        seen_queries.extend(queries)
        return [
            {
                "query": queries[0],
                "result": {
                    "organic_results": [
                        {
                            "title": "Faculty Directory",
                            "link": "https://uni-example.de/faculty",
                            "snippet": "Professor list and lab groups.",
                        }
                    ]
                },
                "error": "",
            }
        ]

    async def _fake_fetch_pages(rows: list[dict], max_pages_to_fetch=None):
        assert rows and rows[0]["url"] == "https://uni-example.de/faculty"
        return {
            "https://uni-example.de/faculty": {
                "content": "Professor Jane Doe leads AI Systems Lab. Contact: faculty office.",
                "published_date": "",
            }
        }

    monkeypatch.setattr(service, "asearch_google_batch", _fake_batch)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks(
        "Find professors and research labs in University of Bonn data science",
        top_k=2,
    )

    assert result["query_plan"]["research_objective_mode"] is True
    assert "professors_and_supervision" in result["query_plan"]["research_objectives"]
    assert "labs_and_research" in result["query_plan"]["research_objectives"]
    assert any("professor" in query.lower() or "faculty" in query.lower() for query in seen_queries)
    assert "research_objective_coverage" in result["verification"]
