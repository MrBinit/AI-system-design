import pytest

from app.services import web_retrieval_service as service


@pytest.fixture(autouse=True)
def _disable_llm_planner_by_default(monkeypatch):
    monkeypatch.setattr(service.settings.serpapi, "query_planner_use_llm", False)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_loop_use_llm", False)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_min_unique_domains", 1)


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_merges_ai_overview_and_organic(monkeypatch):
    monkeypatch.setattr(service.settings.serpapi, "max_context_results", 3)
    monkeypatch.setattr(service.settings.serpapi, "max_page_chars", 1200)
    monkeypatch.setattr(service.settings.serpapi, "default_num", 10)
    monkeypatch.setattr(service.settings.serpapi, "multi_query_enabled", True)
    monkeypatch.setattr(service.settings.serpapi, "max_query_variants", 3)
    monkeypatch.setattr(service.settings.serpapi, "allowed_domain_suffixes", [])

    async def _fake_batch(queries: list[str], **kwargs):
        assert "oxford ai admission" in queries
        assert len(queries) >= 2
        return [
            {
                "query": queries[0],
                "result": {
                    "ai_overview": {
                        "title": "Summary",
                        "text": "Oxford admission requires strong profile.",
                    },
                    "organic_results": [
                        {
                            "title": "Oxford MSc AI",
                            "link": "https://example.edu/oxford-ai",
                            "snippet": "Entry requirements and deadlines.",
                        }
                    ],
                },
                "error": "",
            },
            {
                "query": queries[1],
                "result": {
                    "organic_results": [
                        {
                            "title": "Oxford MSc AI",
                            "link": "https://example.edu/oxford-ai",
                            "snippet": "Duplicate row from another variant.",
                        }
                    ],
                },
                "error": "",
            },
        ]

    async def _fake_fetch_pages(rows: list[dict]):
        assert rows[0]["url"] == "https://example.edu/oxford-ai"
        return {"https://example.edu/oxford-ai": "Detailed page content from source site."}

    async def _should_not_call_single(*_args, **_kwargs):
        raise AssertionError("single-query search should not run when multi-query is enabled")

    monkeypatch.setattr(service, "asearch_google_batch", _fake_batch)
    monkeypatch.setattr(service, "asearch_google", _should_not_call_single)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks("oxford ai admission", top_k=2)
    assert result["retrieval_strategy"] == "web_search"
    assert len(result["query_variants"]) >= 2
    assert len(result["results"]) >= 2
    assert "Oxford admission requires strong profile" in result["results"][0]["content"]
    assert any("Detailed page content" in item["content"] for item in result["results"])
    organic_items = [
        item
        for item in result["results"]
        if item.get("metadata", {}).get("source_type") == "google_organic"
    ]
    assert organic_items
    assert "title" in organic_items[0]["metadata"]
    assert "published_date" in organic_items[0]["metadata"]


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_filters_to_allowed_domain_suffixes(monkeypatch):
    monkeypatch.setattr(service.settings.serpapi, "max_context_results", 4)
    monkeypatch.setattr(service.settings.serpapi, "max_page_chars", 1200)
    monkeypatch.setattr(service.settings.serpapi, "default_num", 10)
    monkeypatch.setattr(service.settings.serpapi, "multi_query_enabled", True)
    monkeypatch.setattr(service.settings.serpapi, "max_query_variants", 3)
    monkeypatch.setattr(service.settings.serpapi, "allowed_domain_suffixes", [".de", ".eu"])

    async def _fake_batch(queries: list[str], **kwargs):
        assert "eu ai universities" in queries
        return [
            {
                "query": queries[0],
                "result": {
                    "ai_overview": {
                        "title": "Summary",
                        "text": "Should be excluded when domain allowlist is active.",
                    },
                    "organic_results": [
                        {
                            "title": "LMU Munich AI",
                            "link": "https://www.lmu.de/programs/ai",
                            "snippet": "German source.",
                        },
                        {
                            "title": "US Blog",
                            "link": "https://example.com/ai",
                            "snippet": "Should be filtered.",
                        },
                    ],
                },
                "error": "",
            },
            {
                "query": queries[1],
                "result": {
                    "organic_results": [
                        {
                            "title": "EU Research",
                            "link": "https://research.example.eu/ai",
                            "snippet": "EU source.",
                        }
                    ],
                },
                "error": "",
            },
        ]

    async def _fake_fetch_pages(rows: list[dict]):
        assert len(rows) == 2
        urls = {row["url"] for row in rows}
        assert "https://www.lmu.de/programs/ai" in urls
        assert "https://research.example.eu/ai" in urls
        return {
            "https://www.lmu.de/programs/ai": "DE content",
            "https://research.example.eu/ai": "EU content",
        }

    monkeypatch.setattr(service, "asearch_google_batch", _fake_batch)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks("eu ai universities", top_k=3)
    assert result["retrieval_strategy"] == "web_search"
    assert len(result["results"]) == 2
    assert all(
        item["metadata"]["url"].endswith((".de/programs/ai", ".eu/ai"))
        for item in result["results"]
    )


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_dedupes_same_url_from_multiple_variants(monkeypatch):
    monkeypatch.setattr(service.settings.serpapi, "max_context_results", 4)
    monkeypatch.setattr(service.settings.serpapi, "max_page_chars", 1200)
    monkeypatch.setattr(service.settings.serpapi, "default_num", 10)
    monkeypatch.setattr(service.settings.serpapi, "multi_query_enabled", True)
    monkeypatch.setattr(service.settings.serpapi, "max_query_variants", 3)
    monkeypatch.setattr(service.settings.serpapi, "allowed_domain_suffixes", [])

    async def _fake_batch(queries: list[str], **kwargs):
        return [
            {
                "query": queries[0],
                "result": {
                    "organic_results": [
                        {
                            "title": "RWTH Program",
                            "link": "https://www.rwth-aachen.de/ai",
                            "snippet": "Program overview.",
                        }
                    ],
                },
                "error": "",
            },
            {
                "query": queries[1],
                "result": {
                    "organic_results": [
                        {
                            "title": "RWTH Program Duplicate",
                            "link": "https://www.rwth-aachen.de/ai",
                            "snippet": "Same URL from another variant.",
                        },
                        {
                            "title": "TUM Program",
                            "link": "https://www.tum.de/ai",
                            "snippet": "Second unique URL.",
                        },
                    ],
                },
                "error": "",
            },
        ]

    async def _fake_fetch_pages(rows: list[dict]):
        urls = [row["url"] for row in rows]
        assert urls.count("https://www.rwth-aachen.de/ai") == 1
        return {
            "https://www.rwth-aachen.de/ai": "RWTH details",
            "https://www.tum.de/ai": "TUM details",
        }

    monkeypatch.setattr(service, "asearch_google_batch", _fake_batch)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks("germany ai program", top_k=3)
    urls = [
        item.get("metadata", {}).get("url", "")
        for item in result["results"]
        if item.get("metadata")
    ]
    assert urls.count("https://www.rwth-aachen.de/ai") == 1


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_preserves_published_date(monkeypatch):
    monkeypatch.setattr(service.settings.serpapi, "max_context_results", 3)
    monkeypatch.setattr(service.settings.serpapi, "max_page_chars", 1200)
    monkeypatch.setattr(service.settings.serpapi, "default_num", 10)
    monkeypatch.setattr(service.settings.serpapi, "multi_query_enabled", False)
    monkeypatch.setattr(service.settings.serpapi, "allowed_domain_suffixes", [])

    async def _fake_single(_query: str, **_kwargs):
        return {
            "organic_results": [
                {
                    "title": "University News",
                    "link": "https://www.example.edu/news/ai",
                    "snippet": "Scholarship updates.",
                    "date": "2026-03-20",
                }
            ]
        }

    async def _fake_fetch_pages(rows: list[dict]):
        assert len(rows) == 1
        return {
            "https://www.example.edu/news/ai": {
                "content": "Scholarship updates and eligibility details.",
                "published_date": "2026-03-19",
            }
        }

    monkeypatch.setattr(service, "asearch_google", _fake_single)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks("example scholarship update", top_k=2)
    organic = [
        item
        for item in result["results"]
        if item.get("metadata", {}).get("source_type") == "google_organic"
    ]
    assert organic
    assert organic[0]["metadata"]["published_date"] == "2026-03-20"
    assert organic[0]["metadata"]["title"] == "University News"


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_requeries_for_missing_subquestions(monkeypatch):
    monkeypatch.setattr(service.settings.serpapi, "max_context_results", 3)
    monkeypatch.setattr(service.settings.serpapi, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.serpapi, "query_planner_use_llm", False)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_loop_max_steps", 2)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_loop_max_gap_queries", 1)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_gap_min_token_coverage", 0.6)
    monkeypatch.setattr(service.settings.serpapi, "allowed_domain_suffixes", [])

    async def _fake_plan(_query: str, _allowed_suffixes: list[str]):
        return {
            "queries": ["germany ai admissions"],
            "subquestions": ["tuition fees", "language requirement"],
            "planner": "heuristic",
            "llm_used": False,
        }

    calls = {"count": 0}

    async def _fake_payloads(queries: list[str], *, top_k: int):
        calls["count"] += 1
        if any("language requirement" in query.lower() for query in queries):
            return [
                {
                    "organic_results": [
                        {
                            "title": "Language Requirement",
                            "link": "https://example.edu/language",
                            "snippet": "English language requirement IELTS 6.5",
                        }
                    ]
                }
            ]
        return [
            {
                "organic_results": [
                    {
                        "title": "Tuition Details",
                        "link": "https://example.edu/tuition",
                        "snippet": "Tuition fees are EUR 2000.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict]):
        urls = {row["url"] for row in rows}
        payload = {}
        if "https://example.edu/tuition" in urls:
            payload["https://example.edu/tuition"] = {
                "content": "Tuition fees are EUR 2000 per semester.",
                "published_date": "2026-02-01",
            }
        if "https://example.edu/language" in urls:
            payload["https://example.edu/language"] = {
                "content": "Language requirement: IELTS 6.5 overall.",
                "published_date": "2026-02-02",
            }
        return payload

    monkeypatch.setattr(service, "_resolve_query_plan", _fake_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks("germany ai admissions", top_k=3)

    assert calls["count"] == 2
    assert result["retrieval_loop"]["iterations"] == 2
    facts_text = " ".join(fact["fact"].lower() for fact in result["facts"])
    assert "tuition fees" in facts_text
    assert "language requirement" in facts_text


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_requeries_for_domain_diversity(monkeypatch):
    monkeypatch.setattr(service.settings.serpapi, "max_context_results", 4)
    monkeypatch.setattr(service.settings.serpapi, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.serpapi, "query_planner_use_llm", False)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_loop_max_steps", 2)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_loop_max_gap_queries", 2)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_min_unique_domains", 2)
    monkeypatch.setattr(service.settings.serpapi, "allowed_domain_suffixes", [])

    async def _fake_plan(_query: str, _allowed_suffixes: list[str]):
        return {
            "queries": ["germany ai admissions"],
            "subquestions": [],
            "planner": "heuristic",
            "llm_used": False,
        }

    calls: list[list[str]] = []

    async def _fake_payloads(queries: list[str], *, top_k: int):
        calls.append(list(queries))
        if any(query.strip().lower() != "germany ai admissions" for query in queries):
            return [
                {
                    "organic_results": [
                        {
                            "title": "Second Source",
                            "link": "https://second.example.org/admissions",
                            "snippet": "Independent confirmation.",
                        }
                    ]
                }
            ]
        return [
            {
                "organic_results": [
                    {
                        "title": "Primary Source",
                        "link": "https://first.example.edu/admissions",
                        "snippet": "Official admissions details.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict]):
        payload = {}
        for row in rows:
            url = row["url"]
            payload[url] = {
                "content": f"Admissions details from {url}.",
                "published_date": "2026-03-01",
            }
        return payload

    monkeypatch.setattr(service, "_resolve_query_plan", _fake_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks("germany ai admissions", top_k=3)

    assert len(calls) == 2
    assert result["retrieval_loop"]["iterations"] == 2
    assert result["verification"]["unique_domain_count"] >= 2
    assert result["verification"]["verified"] is True


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_adds_trust_score_metadata(monkeypatch):
    monkeypatch.setattr(service.settings.serpapi, "max_context_results", 2)
    monkeypatch.setattr(service.settings.serpapi, "query_planner_enabled", False)
    monkeypatch.setattr(service.settings.serpapi, "multi_query_enabled", False)
    monkeypatch.setattr(service.settings.serpapi, "allowed_domain_suffixes", [])

    async def _fake_single(_query: str, **_kwargs):
        return {
            "organic_results": [
                {
                    "title": "Official Program Page",
                    "link": "https://www.example.edu/programs/ai",
                    "snippet": "Admission requirements and curriculum.",
                }
            ]
        }

    async def _fake_fetch_pages(_rows: list[dict]):
        return {
            "https://www.example.edu/programs/ai": {
                "content": "Admission requirements include transcripts and language proof.",
                "published_date": "2026-03-01",
            }
        }

    monkeypatch.setattr(service, "asearch_google", _fake_single)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks("example ai admission", top_k=2)
    assert result["results"]
    metadata = result["results"][0]["metadata"]
    assert "trust_score" in metadata
    assert metadata["trust_score"] >= 0.0
    assert "trust_components" in metadata


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_uses_llm_gap_queries_when_enabled(monkeypatch):
    monkeypatch.setattr(service.settings.serpapi, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_loop_use_llm", True)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_loop_max_steps", 2)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_loop_max_gap_queries", 1)
    monkeypatch.setattr(service.settings.serpapi, "allowed_domain_suffixes", [])

    async def _fake_plan(_query: str, _allowed_suffixes: list[str]):
        return {
            "queries": ["germany ai admissions"],
            "subquestions": ["tuition fees", "language requirement"],
            "planner": "llm",
            "llm_used": True,
        }

    async def _fake_gap_plan(_query: str, **_kwargs):
        return {
            "missing_subquestions": ["language requirement"],
            "queries": ["germany ai admissions official language requirement"],
        }

    call_queries: list[list[str]] = []

    async def _fake_payloads(queries: list[str], *, top_k: int):
        call_queries.append(list(queries))
        if any("official language requirement" in query.lower() for query in queries):
            return [
                {
                    "organic_results": [
                        {
                            "title": "Language Requirement",
                            "link": "https://example.edu/lang",
                            "snippet": "English language requirement IELTS 6.5",
                        }
                    ]
                }
            ]
        return [
            {
                "organic_results": [
                    {
                        "title": "Tuition",
                        "link": "https://example.edu/tuition",
                        "snippet": "Tuition fees are EUR 2000.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict]):
        urls = {row["url"] for row in rows}
        payload = {}
        if "https://example.edu/tuition" in urls:
            payload["https://example.edu/tuition"] = {
                "content": "Tuition fees are EUR 2000.",
                "published_date": "2026-02-01",
            }
        if "https://example.edu/lang" in urls:
            payload["https://example.edu/lang"] = {
                "content": "Language requirement IELTS 6.5.",
                "published_date": "2026-02-03",
            }
        return payload

    monkeypatch.setattr(service, "_resolve_query_plan", _fake_plan)
    monkeypatch.setattr(service, "_aidentify_gap_plan_with_llm", _fake_gap_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks("germany ai admissions", top_k=3)

    assert len(call_queries) == 2
    assert any("official language requirement" in query.lower() for query in call_queries[1])
    assert result["retrieval_loop"]["llm_used"] is True


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_runs_llm_query_planner_before_search(monkeypatch):
    monkeypatch.setattr(service.settings.serpapi, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.serpapi, "query_planner_use_llm", True)
    monkeypatch.setattr(service.settings.serpapi, "retrieval_loop_enabled", False)
    monkeypatch.setattr(service.settings.serpapi, "allowed_domain_suffixes", [])

    call_order: list[str] = []

    async def _fake_llm_plan(query: str, allowed_suffixes: list[str]):
        assert query == "germany ai admissions"
        assert allowed_suffixes == []
        call_order.append("planner")
        return {
            "queries": ["germany ai admissions official site"],
            "subquestions": ["tuition fees"],
            "planner": "llm",
            "llm_used": True,
        }

    async def _fake_payloads(queries: list[str], *, top_k: int):
        assert top_k == 2
        # Search must start only after planner has produced query variants.
        assert call_order == ["planner"]
        assert queries == ["germany ai admissions official site"]
        call_order.append("search")
        return [
            {
                "organic_results": [
                    {
                        "title": "Admissions",
                        "link": "https://example.edu/admissions",
                        "snippet": "Tuition fees and requirements.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict]):
        assert rows and rows[0]["url"] == "https://example.edu/admissions"
        return {
            "https://example.edu/admissions": {
                "content": "Tuition fees are EUR 2000 and language requirement is IELTS 6.5.",
                "published_date": "2026-03-20",
            }
        }

    monkeypatch.setattr(service, "_aplan_queries_with_llm", _fake_llm_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks("germany ai admissions", top_k=2)

    assert call_order == ["planner", "search"]
    assert result["query_plan"]["planner"] == "llm"
    assert result["query_plan"]["llm_used"] is True
    assert result["query_variants"] == ["germany ai admissions official site"]


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_fast_mode_skips_deep_loop(monkeypatch):
    monkeypatch.setattr(service.settings.serpapi, "retrieval_loop_enabled", True)

    async def _should_not_call_resolve_plan(*_args, **_kwargs):
        raise AssertionError("fast mode should not call deep planner resolver")

    call_count = {"search": 0}

    async def _fake_payloads(queries: list[str], *, top_k: int):
        call_count["search"] += 1
        return [
            {
                "organic_results": [
                    {
                        "title": "Admissions",
                        "link": "https://example.edu/admissions",
                        "snippet": "Admission summary.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict], **_kwargs):
        return {
            "https://example.edu/admissions": {
                "content": "Admissions details from official page.",
                "published_date": "2026-03-21",
            }
        }

    monkeypatch.setattr(service, "_resolve_query_plan", _should_not_call_resolve_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks(
        "germany ai admissions",
        top_k=2,
        search_mode="fast",
    )

    assert call_count["search"] == 1
    assert result["search_mode"] == "fast"
    assert result["retrieval_loop"]["enabled"] is False
    assert result["retrieval_loop"]["iterations"] == 1
