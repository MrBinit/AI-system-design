import pytest

from app.infra.io_limiters import DependencyBackpressureError
from app.services import web_retrieval_service as service


@pytest.fixture(autouse=True)
def _disable_llm_planner_by_default(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "query_planner_use_llm", False)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_use_llm", False)
    monkeypatch.setattr(service.settings.web_search, "retrieval_min_unique_domains", 1)
    monkeypatch.setattr(service.settings.web_search, "deep_min_unique_domains", 1)
    monkeypatch.setattr(service.settings.web_search, "official_source_filter_enabled", False)


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_merges_ai_overview_and_organic(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "max_context_results", 3)
    monkeypatch.setattr(service.settings.web_search, "max_page_chars", 1200)
    monkeypatch.setattr(service.settings.web_search, "default_num", 10)
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "max_query_variants", 3)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

    async def _fake_batch(queries: list[str], **kwargs):
        assert any("oxford ai admission" in item for item in queries)
        assert len(queries) >= 1
        responses = [
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
                            "link": "https://uni-example.de/oxford-ai",
                            "snippet": "Entry requirements and deadlines.",
                        }
                    ],
                },
                "error": "",
            }
        ]
        if len(queries) > 1:
            responses.append(
                {
                    "query": queries[1],
                    "result": {
                        "organic_results": [
                            {
                                "title": "Oxford MSc AI",
                                "link": "https://uni-example.de/oxford-ai",
                                "snippet": "Duplicate row from another variant.",
                            }
                        ],
                    },
                    "error": "",
                }
            )
        return responses

    async def _fake_fetch_pages(rows: list[dict]):
        assert rows[0]["url"] == "https://uni-example.de/oxford-ai"
        return {"https://uni-example.de/oxford-ai": "Detailed page content from source site."}

    async def _should_not_call_single(*_args, **_kwargs):
        raise AssertionError("single-query search should not run when multi-query is enabled")

    monkeypatch.setattr(service, "asearch_google_batch", _fake_batch)
    monkeypatch.setattr(service, "asearch_google", _should_not_call_single)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks("oxford ai admission", top_k=2)
    assert result["retrieval_strategy"] == "web_search"
    assert len(result["query_variants"]) >= 2
    assert len(result["results"]) >= 1
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
    monkeypatch.setattr(service.settings.web_search, "max_context_results", 4)
    monkeypatch.setattr(service.settings.web_search, "max_page_chars", 1200)
    monkeypatch.setattr(service.settings.web_search, "default_num", 10)
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "max_query_variants", 3)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [".de", ".eu"])

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
                            "title": "EU Research",
                            "link": "https://research.example.eu/ai",
                            "snippet": "Should be filtered because it is not an official university page.",
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
                            "link": "https://www2.daad.de/programmes/ai",
                            "snippet": "DAAD source.",
                        }
                    ],
                },
                "error": "",
            },
        ]

    async def _fake_fetch_pages(rows: list[dict]):
        assert len(rows) == 3
        urls = {row["url"] for row in rows}
        assert "https://www.lmu.de/programs/ai" in urls
        assert "https://www2.daad.de/programmes/ai" in urls
        assert "https://research.example.eu/ai" in urls
        return {
            "https://www.lmu.de/programs/ai": "DE content",
            "https://www2.daad.de/programmes/ai": "DAAD content",
            "https://research.example.eu/ai": "EU content",
        }

    monkeypatch.setattr(service, "asearch_google_batch", _fake_batch)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks("eu ai universities", top_k=3)
    assert result["retrieval_strategy"] == "web_search"
    assert len(result["results"]) == 3
    hosts = {
        service._domain_group_key(
            service._normalized_host(str(item.get("metadata", {}).get("url", "")))
        )
        for item in result["results"]
    }
    assert hosts == {"lmu.de", "daad.de", "example.eu"}


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_dedupes_same_url_from_multiple_variants(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "max_context_results", 4)
    monkeypatch.setattr(service.settings.web_search, "max_page_chars", 1200)
    monkeypatch.setattr(service.settings.web_search, "default_num", 10)
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "max_query_variants", 3)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

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
    monkeypatch.setattr(service.settings.web_search, "max_context_results", 3)
    monkeypatch.setattr(service.settings.web_search, "max_page_chars", 1200)
    monkeypatch.setattr(service.settings.web_search, "default_num", 10)
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

    async def _fake_single(_query: str, **_kwargs):
        return {
            "organic_results": [
                {
                    "title": "University News",
                    "link": "https://uni-example.de/news/ai",
                    "snippet": "Scholarship updates.",
                    "date": "2026-03-20",
                }
            ]
        }

    async def _fake_fetch_pages(rows: list[dict]):
        assert len(rows) == 1
        return {
            "https://uni-example.de/news/ai": {
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
    monkeypatch.setattr(service.settings.web_search, "max_context_results", 3)
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "query_planner_use_llm", False)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_steps", 2)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_gap_queries", 1)
    monkeypatch.setattr(service.settings.web_search, "retrieval_gap_min_token_coverage", 0.6)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

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
                            "link": "https://uni-example.de/language",
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
                        "link": "https://uni-example.de/tuition",
                        "snippet": "Tuition fees are EUR 2000.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict]):
        urls = {row["url"] for row in rows}
        payload = {}
        if "https://uni-example.de/tuition" in urls:
            payload["https://uni-example.de/tuition"] = {
                "content": "Tuition fees are EUR 2000 per semester.",
                "published_date": "2026-02-01",
            }
        if "https://uni-example.de/language" in urls:
            payload["https://uni-example.de/language"] = {
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
    monkeypatch.setattr(service.settings.web_search, "max_context_results", 4)
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "query_planner_use_llm", False)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_steps", 2)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_gap_queries", 2)
    monkeypatch.setattr(service.settings.web_search, "retrieval_min_unique_domains", 2)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

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
                            "link": "https://uni-second.de/admissions",
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
                        "link": "https://uni-first.de/admissions",
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
    monkeypatch.setattr(service.settings.web_search, "max_context_results", 2)
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

    async def _fake_single(_query: str, **_kwargs):
        return {
            "organic_results": [
                {
                    "title": "Official Program Page",
                    "link": "https://uni-example.de/programs/ai",
                    "snippet": "Admission requirements and curriculum.",
                }
            ]
        }

    async def _fake_fetch_pages(_rows: list[dict]):
        return {
            "https://uni-example.de/programs/ai": {
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
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_use_llm", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_steps", 2)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_gap_queries", 1)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

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
                            "link": "https://uni-example.de/lang",
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
                        "link": "https://uni-example.de/tuition",
                        "snippet": "Tuition fees are EUR 2000.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict]):
        urls = {row["url"] for row in rows}
        payload = {}
        if "https://uni-example.de/tuition" in urls:
            payload["https://uni-example.de/tuition"] = {
                "content": "Tuition fees are EUR 2000.",
                "published_date": "2026-02-01",
            }
        if "https://uni-example.de/lang" in urls:
            payload["https://uni-example.de/lang"] = {
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
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "query_planner_use_llm", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

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
        assert "germany ai admissions official site" in queries
        assert any("tuition fees" in item for item in queries)
        call_order.append("search")
        return [
            {
                "organic_results": [
                    {
                        "title": "Admissions",
                        "link": "https://uni-example.de/admissions",
                        "snippet": "Tuition fees and requirements.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict]):
        assert rows and rows[0]["url"] == "https://uni-example.de/admissions"
        return {
            "https://uni-example.de/admissions": {
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
    assert "germany ai admissions official site" in result["query_variants"]
    assert any("tuition fees" in item for item in result["query_variants"])


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_fast_mode_skips_deep_loop(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)

    async def _should_not_call_resolve_plan(*_args, **_kwargs):
        raise AssertionError("fast mode should not call deep planner resolver")

    call_count = {"search": 0}

    async def _fake_payloads(queries: list[str], *, top_k: int):
        assert 1 <= len(queries) <= 2
        call_count["search"] += 1
        return [
            {
                "organic_results": [
                    {
                        "title": "Admissions",
                        "link": "https://uni-example.de/admissions",
                        "snippet": "Admission summary.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict], **_kwargs):
        return {
            "https://uni-example.de/admissions": {
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


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_standard_mode_skips_deep_loop(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)

    async def _should_not_call_resolve_plan(*_args, **_kwargs):
        raise AssertionError("standard mode should not call deep planner resolver")

    call_count = {"search": 0}

    async def _fake_payloads(queries: list[str], *, top_k: int):
        assert 1 <= len(queries) <= 2
        _ = queries, top_k
        call_count["search"] += 1
        return [
            {
                "organic_results": [
                    {
                        "title": "Admissions",
                        "link": "https://uni-example.de/admissions",
                        "snippet": "Admission summary.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(_rows: list[dict], **_kwargs):
        return {
            "https://uni-example.de/admissions": {
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
        search_mode="standard",
    )

    assert call_count["search"] == 1
    assert result["search_mode"] == "standard"
    assert result["retrieval_loop"]["enabled"] is False
    assert result["retrieval_loop"]["iterations"] == 1


@pytest.mark.asyncio
async def test_asearch_payloads_uses_mode_specific_search_depth(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "default_num", 3)
    captured_depths: list[str] = []

    async def _fake_batch(queries: list[str], **kwargs):
        _ = queries
        captured_depths.append(str(kwargs.get("search_depth", "")))
        return [
            {
                "query": "q1",
                "result": {"organic_results": []},
                "error": "",
            }
        ]

    monkeypatch.setattr(service, "asearch_google_batch", _fake_batch)

    token = service._RETRIEVAL_MODE_CTX.set("deep")
    try:
        await service._asearch_payloads(["q1", "q2"], top_k=2)
    finally:
        service._RETRIEVAL_MODE_CTX.reset(token)

    token = service._RETRIEVAL_MODE_CTX.set("standard")
    try:
        await service._asearch_payloads(["q1", "q2"], top_k=2)
    finally:
        service._RETRIEVAL_MODE_CTX.reset(token)

    assert captured_depths == ["advanced", "basic"]


@pytest.mark.asyncio
async def test_asearch_payloads_uses_mode_specific_result_count(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "default_num", 3)
    monkeypatch.setattr(service.settings.web_search, "deep_default_num", 6)
    captured_nums: list[int] = []

    async def _fake_batch(queries: list[str], **kwargs):
        _ = queries
        captured_nums.append(int(kwargs.get("num", 0)))
        return [
            {
                "query": "q1",
                "result": {"organic_results": []},
                "error": "",
            }
        ]

    monkeypatch.setattr(service, "asearch_google_batch", _fake_batch)

    token = service._RETRIEVAL_MODE_CTX.set("deep")
    try:
        await service._asearch_payloads(["q1", "q2"], top_k=2)
    finally:
        service._RETRIEVAL_MODE_CTX.reset(token)

    token = service._RETRIEVAL_MODE_CTX.set("standard")
    try:
        await service._asearch_payloads(["q1", "q2"], top_k=2)
    finally:
        service._RETRIEVAL_MODE_CTX.reset(token)

    assert captured_nums == [6, 3]


@pytest.mark.asyncio
async def test_asearch_payloads_applies_official_deep_policy(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "default_num", 3)
    monkeypatch.setattr(service.settings.web_search, "deep_default_num", 6)
    captured_kwargs: list[dict] = []

    async def _fake_batch(queries: list[str], **kwargs):
        _ = queries
        captured_kwargs.append(dict(kwargs))
        return [
            {
                "query": "q1",
                "result": {"organic_results": []},
                "error": "",
            }
        ]

    monkeypatch.setattr(service, "asearch_google_batch", _fake_batch)

    mode_token = service._RETRIEVAL_MODE_CTX.set("deep")
    query_token = service._RETRIEVAL_QUERY_CTX.set(
        "University of Mannheim MSc Business Informatics IELTS GPA ECTS deadline portal"
    )
    strict_token = service._RETRIEVAL_STRICT_OFFICIAL_CTX.set(True)
    target_domains_token = service._RETRIEVAL_TARGET_DOMAINS_CTX.set(
        ("uni-mannheim.de", "portal2.uni-mannheim.de", "daad.de")
    )
    required_ids_token = service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.set(
        ("language_score_thresholds", "gpa_threshold", "application_deadline", "application_portal")
    )
    try:
        await service._asearch_payloads(
            ["university of mannheim msc business informatics site:uni-mannheim.de"],
            top_k=3,
        )
    finally:
        service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.reset(required_ids_token)
        service._RETRIEVAL_TARGET_DOMAINS_CTX.reset(target_domains_token)
        service._RETRIEVAL_STRICT_OFFICIAL_CTX.reset(strict_token)
        service._RETRIEVAL_QUERY_CTX.reset(query_token)
        service._RETRIEVAL_MODE_CTX.reset(mode_token)

    assert captured_kwargs
    kwargs = captured_kwargs[0]
    assert kwargs["search_depth"] == "advanced"
    assert kwargs["include_raw_content"] == "markdown"
    assert kwargs["include_answer"] is False
    include_domains = kwargs.get("include_domains") or []
    assert "uni-mannheim.de" in include_domains
    assert "daad.de" in include_domains


@pytest.mark.asyncio
async def test_atry_tavily_extract_rows_returns_cleaned_content(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "max_page_chars", 1200)
    captured: dict[str, object] = {}

    async def _fake_extract(urls: list[str], *, extract_depth: str, query: str | None = None):
        captured["urls"] = list(urls)
        captured["extract_depth"] = extract_depth
        captured["query"] = query
        return {
            "results": [
                {
                    "url": "https://uni-example.de/apply",
                    "raw_content": "  Apply online through the official portal.  ",
                    "published_date": "2026-04-01",
                }
            ]
        }

    monkeypatch.setattr(service, "aextract_urls", _fake_extract)

    mode_token = service._RETRIEVAL_MODE_CTX.set("deep")
    try:
        extracted = await service._atry_tavily_extract_rows(
            [
                {
                    "title": "Apply Online",
                    "url": "https://uni-example.de/apply",
                    "snippet": "Apply online",
                }
            ],
            query="where to apply",
            allowed_suffixes=[],
            strict_official=False,
            target_domain_groups=None,
            enforce_target_domain_scope=False,
            max_urls=4,
        )
    finally:
        service._RETRIEVAL_MODE_CTX.reset(mode_token)

    assert captured["urls"] == ["https://uni-example.de/apply"]
    assert captured["extract_depth"] == "advanced"
    assert captured["query"] == "where to apply"
    assert "https://uni-example.de/apply" in extracted
    assert extracted["https://uni-example.de/apply"]["content"] == "Apply online through the official portal."


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_prefers_tavily_extract_content_when_fetch_is_empty(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

    async def _fake_plan(_query: str, _allowed_suffixes: list[str]):
        return {
            "queries": ["uni example apply portal"],
            "subquestions": [],
            "planner": "heuristic",
            "llm_used": False,
        }

    async def _fake_payloads(_queries: list[str], *, top_k: int):
        _ = top_k
        return [
            {
                "organic_results": [
                    {
                        "title": "Apply Online",
                        "link": "https://uni-example.de/apply",
                        "snippet": "Portal information",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(_rows: list[dict], **_kwargs):
        return {
            "https://uni-example.de/apply": {
                "content": "",
                "published_date": "2026-03-01",
            }
        }

    async def _fake_extract_rows(*_args, **_kwargs):
        return {
            "https://uni-example.de/apply": {
                "content": "Apply via https://uni-example.de/apply portal.",
                "published_date": "2026-03-02",
                "internal_links": [],
            }
        }

    monkeypatch.setattr(service, "_resolve_query_plan", _fake_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)
    monkeypatch.setattr(service, "_atry_tavily_extract_rows", _fake_extract_rows)

    result = await service.aretrieve_web_chunks(
        "where to apply for uni example msc",
        top_k=2,
        search_mode="deep",
    )

    assert result["results"]
    assert any(
        "https://uni-example.de/apply portal" in str(item.get("content", ""))
        for item in result["results"]
    )


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_deep_uses_standard_first_without_escalation_when_complete(
    monkeypatch,
):
    calls: list[str] = []

    async def _fake_impl(_query: str, *, top_k: int, search_mode: str = "deep"):
        _ = top_k
        calls.append(search_mode)
        if search_mode == "standard":
            return {
                "results": [
                    {
                        "content": (
                            "MSc Business Informatics is taught in English. "
                            "Language requirement: IELTS 6.0 or TOEFL iBT 72. "
                            "Apply via portal https://portal2.uni-mannheim.de/."
                        ),
                        "metadata": {"url": "https://uni-mannheim.de/en/admission"},
                    }
                ]
            }
        raise AssertionError("deep mode should not run when standard pass is complete")

    monkeypatch.setattr(service, "_aretrieve_web_chunks_impl", _fake_impl)

    result = await service.aretrieve_web_chunks(
        (
            "Tell me about University of Mannheim MSc Business Informatics language requirements "
            "and where to apply."
        ),
        top_k=3,
        search_mode="deep",
    )

    assert calls == ["standard"]
    assert result["results"]


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_deep_escalates_after_standard_when_required_fields_missing(
    monkeypatch,
):
    calls: list[str] = []

    async def _fake_impl(_query: str, *, top_k: int, search_mode: str = "deep"):
        _ = top_k
        calls.append(search_mode)
        if search_mode == "standard":
            return {
                "results": [
                    {
                        "content": "Proof of English proficiency is required.",
                        "metadata": {"url": "https://uni-mannheim.de/en/admission"},
                    }
                ]
            }
        return {
            "results": [
                {
                    "content": (
                        "MSc Business Informatics language requirement: IELTS 6.0 or TOEFL iBT 72. "
                        "Apply via https://portal2.uni-mannheim.de/."
                    ),
                    "metadata": {"url": "https://uni-mannheim.de/en/admission"},
                }
            ],
            "search_mode": "deep",
        }

    monkeypatch.setattr(service, "_aretrieve_web_chunks_impl", _fake_impl)

    result = await service.aretrieve_web_chunks(
        (
            "Tell me about University of Mannheim MSc Business Informatics language requirements "
            "and where to apply."
        ),
        top_k=3,
        search_mode="deep",
    )

    assert calls == ["standard", "deep"]
    assert result.get("search_mode") == "deep"


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_deep_skips_standard_first_for_high_cost_admissions_scope(
    monkeypatch,
):
    calls: list[str] = []

    async def _fake_impl(_query: str, *, top_k: int, search_mode: str = "deep"):
        _ = top_k
        calls.append(search_mode)
        return {
            "results": [
                {
                    "content": (
                        "Language requirement: IELTS 6.5. "
                        "Minimum grade requirement is 2.5 and at least 30 ECTS. "
                        "Application deadline for international students: 15 July 2026. "
                        "Apply via https://portal2.uni-mannheim.de/."
                    ),
                    "metadata": {"url": "https://uni-mannheim.de/en/admission"},
                }
            ],
            "search_mode": search_mode,
        }

    monkeypatch.setattr(service, "_aretrieve_web_chunks_impl", _fake_impl)

    result = await service.aretrieve_web_chunks(
        (
            "Tell me about University of Mannheim MSc Business Informatics: "
            "IELTS/German requirement, GPA and ECTS requirements, "
            "application deadline for international students, and where to apply."
        ),
        top_k=3,
        search_mode="deep",
    )

    assert calls == ["deep"]
    assert result.get("search_mode") == "deep"


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_stops_loop_when_coverage_is_not_improving(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_steps", 4)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_gap_queries", 1)
    monkeypatch.setattr(service, "_retrieval_loop_max_stagnant_steps", lambda: 1)
    monkeypatch.setattr(service.settings.web_search, "deep_required_field_rescue_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

    async def _fake_plan(_query: str, _allowed_suffixes: list[str]):
        return {
            "queries": ["uni sample msc ai language requirements"],
            "subquestions": [],
            "planner": "heuristic",
            "llm_used": False,
        }

    async def _fake_payloads(_queries: list[str], *, top_k: int):
        _ = top_k
        return [
            {
                    "organic_results": [
                        {
                            "title": "Language Overview",
                            "link": "https://uni-sample.de/language",
                            "snippet": "English proficiency required.",
                        }
                    ]
                }
            ]

    async def _fake_fetch_pages(rows: list[dict], **_kwargs):
        return {
            str(rows[0].get("url", "")): {
                "content": "Applicants must provide proof of English proficiency.",
                "published_date": "2026-03-10",
            }
        }

    monkeypatch.setattr(service, "_resolve_query_plan", _fake_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service._aretrieve_web_chunks_impl(
        (
            "Tell me University of Sample MSc AI language requirements and where to apply."
        ),
        top_k=3,
        search_mode="deep",
    )

    assert result["retrieval_loop"]["iterations"] in {1, 2}
    stop_reason = str((result.get("metrics", {}) or {}).get("stop_reason", "")).strip()
    if stop_reason:
        assert stop_reason in {"no_progress", "budget_cap", "coverage_reached"}


def test_program_scope_bias_penalizes_program_drift():
    token = service._RETRIEVAL_QUERY_CTX.set(
        "University of Mannheim MSc Business Informatics requirements"
    )
    try:
        matching_bias = service._program_scope_bias(
            title="MSc Business Informatics",
            url="https://uni-mannheim.de/en/academics/msc-business-informatics/",
            snippet="Master's program overview.",
            content="MSc Business Informatics is taught in English.",
        )
        drift_bias = service._program_scope_bias(
            title="BSc Business Informatics",
            url="https://uni-mannheim.de/en/academics/bsc-business-informatics/",
            snippet="Bachelor program details.",
            content="Bachelor level modules and undergraduate track.",
        )
    finally:
        service._RETRIEVAL_QUERY_CTX.reset(token)

    assert matching_bias > drift_bias


def test_passes_degree_level_lock_rejects_bachelor_only_for_master_query():
    token = service._RETRIEVAL_QUERY_CTX.set(
        "University of Mannheim MSc Business Informatics requirements"
    )
    try:
        assert (
            service._passes_degree_level_lock(
                title="BSc Business Informatics",
                url="https://uni-mannheim.de/en/academics/bsc-business-informatics/",
                snippet="Bachelor program",
                content="Undergraduate modules",
            )
            is False
        )
        assert (
            service._passes_degree_level_lock(
                title="MSc Business Informatics",
                url="https://uni-mannheim.de/en/academics/masters-program-in-business-informatics/",
                snippet="Master program",
                content="Graduate level",
            )
            is True
        )
    finally:
        service._RETRIEVAL_QUERY_CTX.reset(token)


def test_filter_rows_by_program_scope_rejects_off_target_program_pages():
    token = service._RETRIEVAL_QUERY_CTX.set(
        "University of Mannheim MSc Business Informatics requirements"
    )
    try:
        rows = [
            {
                "title": "Mannheim Master in Operations and Supply Chain Management",
                "url": "https://www.uni-mannheim.de/en/academics/masters-program-operations-supply-chain/",
                "snippet": "Master program details",
            },
            {
                "title": "Master's Program in Business Informatics",
                "url": "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/masters-program-in-business-informatics",
                "snippet": "MSc Business Informatics program facts",
            },
        ]
        filtered = service._filter_rows_by_program_scope(rows, allow_fallback_on_empty=False)
    finally:
        service._RETRIEVAL_QUERY_CTX.reset(token)

    assert len(filtered) == 1
    assert "business-informatics" in filtered[0]["url"]


def test_required_field_evidence_table_builds_found_and_missing_rows():
    required_fields = service._required_fields_from_query(
        "University of Mannheim MSc Business Informatics language requirements and application portal"
    )
    candidates = [
        {
            "content": "English requirement: IELTS 6.0 or TOEFL iBT 72.",
            "metadata": {
                "url": "https://uni-mannheim.de/en/academics/before-your-studies/programs/masters-program-in-business-informatics",
                "trust_score": 0.83,
            },
        },
        {
            "content": "Apply online via portal: https://portal2.uni-mannheim.de/",
            "metadata": {
                "url": "https://portal2.uni-mannheim.de/",
                "trust_score": 0.8,
            },
        },
    ]
    rows = service._required_field_evidence_table(required_fields, candidates)
    by_id = {row["id"]: row for row in rows}
    assert by_id["language_requirements"]["status"] == "found"
    assert "ielts" in by_id["language_requirements"]["value"].lower()
    assert by_id["application_portal"]["status"] == "found"
    assert "portal2.uni-mannheim.de" in by_id["application_portal"]["value"].lower()


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_prioritizes_field_routing_before_generic_planner_query(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_steps", 1)
    monkeypatch.setattr(service.settings.web_search, "deep_required_field_rescue_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

    async def _fake_plan(_query: str, _allowed_suffixes: list[str]):
        return {
            "queries": ["generic planner query that should not run first"],
            "subquestions": [],
            "planner": "heuristic",
            "llm_used": False,
        }

    seen_batches: list[list[str]] = []

    async def _fake_payloads(queries: list[str], *, top_k: int):
        _ = top_k
        seen_batches.append(list(queries))
        return [
            {
                "organic_results": [
                    {
                        "title": "Master's Program in Business Informatics",
                        "link": "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/masters-program-in-business-informatics",
                        "snippet": "Program information and admission details.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict], **_kwargs):
        return {
            str(rows[0].get("url", "")): {
                "content": "Application deadline: 15 May 2026. Apply via https://portal2.uni-mannheim.de/",
                "published_date": "2026-03-01",
            }
        }

    monkeypatch.setattr(service, "_resolve_query_plan", _fake_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    await service.aretrieve_web_chunks(
        (
            "Tell me about University of Mannheim MSc Business Informatics: IELTS/German, GPA/ECTS, "
            "deadline and portal using official sources only."
        ),
        top_k=3,
        search_mode="deep",
    )

    assert seen_batches
    first_batch = " ".join(seen_batches[0]).lower()
    assert "generic planner query" not in first_batch
    assert (
        "official selection statute" in first_batch
        or "official application deadlines" in first_batch
        or "official apply online portal" in first_batch
    )


@pytest.mark.asyncio
async def test_query_planner_uses_cache_before_llm_call(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "query_planner_use_llm", True)
    monkeypatch.setattr(service.settings.web_search, "query_planner_cache_enabled", True)

    async def _fake_cache_read(_cache_key: str):
        return {
            "queries": ["germany ai admissions official site"],
            "subquestions": ["tuition fees"],
        }

    async def _should_not_call_create(**_kwargs):
        raise AssertionError("planner should use cache and skip model call")

    from app.infra import bedrock_chat_client

    monkeypatch.setattr(service, "_read_cache_json", _fake_cache_read)
    monkeypatch.setattr(
        bedrock_chat_client.client.chat.completions,
        "create",
        _should_not_call_create,
    )

    plan = await service._aplan_queries_with_llm("germany ai admissions", [])

    assert plan is not None
    assert plan["planner"] == "llm_cache"
    assert plan["llm_used"] is True


@pytest.mark.asyncio
async def test_query_planner_backpressure_falls_back(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "query_planner_use_llm", True)
    monkeypatch.setattr(service.settings.web_search, "query_planner_cache_enabled", False)

    async def _raise_backpressure(**_kwargs):
        raise DependencyBackpressureError("llm_planner", 0.75)

    from app.infra import bedrock_chat_client

    monkeypatch.setattr(
        bedrock_chat_client.client.chat.completions,
        "create",
        _raise_backpressure,
    )

    plan = await service._aplan_queries_with_llm("germany ai admissions", [])

    assert plan is None


def test_build_query_variants_includes_suffix_scoped_variant(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "max_query_variants", 6)

    variants = service._build_query_variants(
        "Compare TUM vs LMU data science admissions",
        [".de", ".eu"],
    )

    assert any("site:.de" in item and "site:.eu" in item for item in variants)


def test_build_query_variants_prioritizes_official_source_routes(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "max_query_variants", 5)
    monkeypatch.setattr(service.settings.web_search, "official_source_allowlist", ["daad.de"])

    variants = service._build_query_variants(
        "University of Mannheim MSc Business Informatics admission deadline language requirements",
        [".de", ".eu"],
    )
    lowered = [item.lower() for item in variants]

    assert any("site:uni-mannheim.de" in item for item in lowered)
    assert any("admission requirements" in item for item in lowered)


def test_build_official_source_route_queries_includes_daad_and_uni_assist(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "official_source_allowlist", ["daad.de"])

    queries = service._build_official_source_route_queries(
        "University of Stuttgart autonomous systems master admission",
        [{"id": "application_portal"}, {"id": "admission_requirements"}],
        max_queries=12,
    )
    lowered = [item.lower() for item in queries]

    assert any("site:uni-stuttgart.de" in item for item in lowered)
    assert any("site:daad.de" in item for item in lowered)
    assert any("site:uni-assist.de" in item for item in lowered)


def test_build_query_variants_adds_entity_focused_queries(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "max_query_variants", 8)

    variants = service._build_query_variants(
        "Compare TUM vs LMU for English-taught data science master's programs",
        [".de", ".eu"],
    )

    assert any("tum data science master's program" in item.lower() for item in variants)
    assert any("lmu data science master's program" in item.lower() for item in variants)


def test_build_query_variants_fast_mode_is_lightweight(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "max_query_variants", 5)

    token = service._RETRIEVAL_MODE_CTX.set("fast")
    try:
        variants = service._build_query_variants(
            "University of Hamburg MSc Data Science and Artificial Intelligence",
            [".de", ".eu"],
        )
    finally:
        service._RETRIEVAL_MODE_CTX.reset(token)

    assert 1 <= len(variants) <= 2


def test_max_query_variants_for_mode_uses_deep_override(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "max_query_variants", 3)
    monkeypatch.setattr(service.settings.web_search, "deep_max_query_variants", 5)

    token = service._RETRIEVAL_MODE_CTX.set("deep")
    try:
        deep_variants = service._max_query_variants_for_mode()
    finally:
        service._RETRIEVAL_MODE_CTX.reset(token)

    token = service._RETRIEVAL_MODE_CTX.set("standard")
    try:
        standard_variants = service._max_query_variants_for_mode()
    finally:
        service._RETRIEVAL_MODE_CTX.reset(token)

    assert deep_variants == 5
    assert standard_variants == 2


def test_url_matches_allowed_suffix_filters_to_de_and_eu():
    assert service._url_matches_allowed_suffix("https://www.uni-tuebingen.de/en/", [".de", ".eu"])
    assert service._url_matches_allowed_suffix("https://research.example.eu/ai", [".de", ".eu"])
    assert not service._url_matches_allowed_suffix("https://example.com/ai", [".de", ".eu"])


def test_normalized_allowed_domain_suffixes_reads_settings(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", ["de", ".eu", "DE"])
    assert service._normalized_allowed_domain_suffixes() == [".de", ".eu"]


def test_retrieval_min_unique_domains_uses_deep_override(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "retrieval_min_unique_domains", 1)
    monkeypatch.setattr(service.settings.web_search, "deep_min_unique_domains", 3)

    deep_token = service._RETRIEVAL_MODE_CTX.set("deep")
    try:
        deep_value = service._retrieval_min_unique_domains()
    finally:
        service._RETRIEVAL_MODE_CTX.reset(deep_token)

    standard_token = service._RETRIEVAL_MODE_CTX.set("standard")
    try:
        standard_value = service._retrieval_min_unique_domains()
    finally:
        service._RETRIEVAL_MODE_CTX.reset(standard_token)

    assert deep_value == 3
    assert standard_value == 1


def test_filter_rows_by_allowed_domains_keeps_official_and_daad_only(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "official_source_filter_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "official_source_allowlist", ["daad.de"])

    rows = [
        {
            "title": "M.Sc. Program",
            "url": "https://www.uni-hamburg.de/en/studium/master/programs/data-science.html",
            "snippet": "University of Hamburg master's program details.",
        },
        {
            "title": "DAAD Program Entry",
            "url": "https://www2.daad.de/deutschland/studienangebote/international-programmes/en/detail/5634/",
            "snippet": "DAAD international program details.",
        },
        {
            "title": "Forum discussion",
            "url": "https://research-forum.eu/ai",
            "snippet": "Community notes about admissions.",
        },
    ]
    filtered = service._filter_rows_by_allowed_domains(rows, [".de", ".eu"])
    urls = {str(item["url"]) for item in filtered}
    assert "https://www.uni-hamburg.de/en/studium/master/programs/data-science.html" in urls
    assert (
        "https://www2.daad.de/deutschland/studienangebote/international-programmes/en/detail/5634/"
        in urls
    )
    assert "https://research-forum.eu/ai" not in urls


def test_source_filter_decisions_report_rejection_reasons(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "official_source_filter_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "official_source_allowlist", ["daad.de"])

    rows = [
        {
            "title": "DAAD Program Entry",
            "url": "https://www2.daad.de/deutschland/studienangebote/international-programmes/en/detail/5634/",
            "snippet": "DAAD international program details.",
        },
        {
            "title": "Forum discussion",
            "url": "https://example.com/ai",
            "snippet": "Community notes about admissions.",
        },
        {
            "title": "Research portal",
            "url": "https://research-forum.eu/ai",
            "snippet": "Community notes about admissions.",
        },
    ]

    decisions = service._source_filter_decisions(
        rows,
        allowed_suffixes=[".de", ".eu"],
        strict_official=False,
    )
    summary = service._source_filter_summary(decisions)

    assert summary["kept_count"] == 1
    assert summary["rejected_count"] == 2
    assert summary["reason_counts"]["kept"] == 1
    assert summary["reason_counts"]["domain_suffix_not_allowed"] == 1
    assert summary["reason_counts"]["non_official_host"] == 1


def test_official_domains_for_query_infers_university_domains():
    domains = service._official_domains_for_query(
        "tell me university of tubingen msc machine learning requirements"
    )
    assert "uni-tubingen.de" in domains or "uni-tuebingen.de" in domains

    fau_domains = service._official_domains_for_query(
        "tell me fau erlangen nurnberg msc artificial intelligence"
    )
    assert "fau.de" in fau_domains
    tum_domains = service._official_domains_for_query(
        "tell me about technical university of munich msc data engineering"
    )
    assert "tum.de" in tum_domains
    assert "uni-munich.de" not in tum_domains

    mannheim_domains = service._official_domains_for_query(
        "tell me about university of mannheim msc business informatics requirements"
    )
    assert "uni-mannheim.de" in mannheim_domains
    assert "portal2.uni-mannheim.de" in mannheim_domains
    assert "tu-mannheim.de" not in mannheim_domains


def test_strict_official_policy_rejects_non_official_admissions_sources(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "official_source_filter_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "official_source_allowlist", ["daad.de"])

    rows = [
        {
            "title": "University of Tuebingen MSc Machine Learning",
            "url": "https://www.uni-tuebingen.de/en/study/finding-a-course/degree-programs-available/detail/course/machine-learning-master/",
            "snippet": "Official university program page with application details.",
        },
        {
            "title": "MSc Machine Learning Guide",
            "url": "https://myguide.de/program/tuebingen-machine-learning",
            "snippet": "External guide for university applications.",
        },
        {
            "title": "DAAD Program Entry",
            "url": "https://www2.daad.de/deutschland/studienangebote/international-programmes/en/detail/5634/",
            "snippet": "DAAD entry for the program.",
        },
    ]
    filtered = service._filter_rows_by_allowed_domains_with_policy(
        rows,
        [".de", ".eu"],
        strict_official=True,
    )
    urls = {str(item["url"]) for item in filtered}
    assert (
        "https://www.uni-tuebingen.de/en/study/finding-a-course/degree-programs-available/detail/course/machine-learning-master/"
        in urls
    )
    assert (
        "https://www2.daad.de/deutschland/studienangebote/international-programmes/en/detail/5634/"
        in urls
    )
    assert "https://myguide.de/program/tuebingen-machine-learning" not in urls


def test_filter_rows_by_target_domain_groups_enforces_scope_without_fallback(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "official_source_allowlist", ["daad.de"])
    rows = [
        {
            "title": "University of Bonn MSc Computer Science",
            "url": "https://www.uni-bonn.de/en/studying/degree-programs/msc-computer-science",
            "snippet": "Official admissions and requirements page.",
        },
        {
            "title": "HBRS Master Program",
            "url": "https://www.h-brs.de/en/cs/master-computer-science",
            "snippet": "Another university page.",
        },
        {
            "title": "DAAD Program Entry",
            "url": "https://www2.daad.de/deutschland/studienangebote/international-programmes/en/detail/1234/",
            "snippet": "DAAD source.",
        },
    ]
    filtered = service._filter_rows_by_target_domain_groups(
        rows,
        target_groups=["uni-bonn.de"],
        allow_fallback_on_empty=False,
    )
    urls = {str(item["url"]) for item in filtered}
    assert "https://www.uni-bonn.de/en/studying/degree-programs/msc-computer-science" in urls
    assert (
        "https://www2.daad.de/deutschland/studienangebote/international-programmes/en/detail/1234/"
        in urls
    )
    assert "https://www.h-brs.de/en/cs/master-computer-science" not in urls


def test_collect_search_rows_enforces_target_domain_scope(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "official_source_filter_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "official_source_allowlist", ["daad.de"])

    payloads = [
        {
            "organic_results": [
                {
                    "title": "University of Bonn MSc Computer Science",
                    "link": "https://www.uni-bonn.de/en/studying/degree-programs/msc-computer-science",
                    "snippet": "Official program page with requirements.",
                },
                {
                    "title": "HBRS Computer Science MSc",
                    "link": "https://www.h-brs.de/en/cs/master-computer-science",
                    "snippet": "Official page from a different university.",
                },
                {
                    "title": "DAAD Program Entry",
                    "link": "https://www2.daad.de/deutschland/studienangebote/international-programmes/en/detail/1234/",
                    "snippet": "DAAD entry.",
                },
            ]
        }
    ]

    rows = service._collect_search_rows(
        payloads,
        ["university of bonn msc computer science requirements"],
        top_k=3,
        allowed_suffixes=[".de", ".eu"],
        strict_official=True,
        target_domain_groups=["uni-bonn.de"],
        enforce_target_domain_scope=True,
    )
    urls = {str(item["url"]) for item in rows}
    assert "https://www.uni-bonn.de/en/studying/degree-programs/msc-computer-science" in urls
    assert (
        "https://www2.daad.de/deutschland/studienangebote/international-programmes/en/detail/1234/"
        in urls
    )
    assert "https://www.h-brs.de/en/cs/master-computer-science" not in urls


def test_fetch_page_data_sync_extracts_pdf(monkeypatch):
    class _FakePdfPage:
        def extract_text(self):
            return "Admission requirements\nLanguage: IELTS 6.5"

    class _FakePdfReader:
        def __init__(self, _buffer):
            self.pages = [_FakePdfPage()]

    class _FakeResponse:
        def __init__(self):
            self.headers = {"Content-Type": "application/pdf"}

        def read(self, _max_bytes):
            return b"%PDF-sample"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(service, "PdfReader", _FakePdfReader)
    monkeypatch.setattr(service.urllib.request, "urlopen", lambda *_args, **_kwargs: _FakeResponse())

    page = service._fetch_page_data_sync(
        "https://uni-example.de/program.pdf",
        timeout_seconds=5.0,
        max_chars=500,
    )
    assert "Admission requirements" in page["content"]
    assert page["published_date"] == ""
    assert page["internal_links"] == []


@pytest.mark.asyncio
async def test_afetch_organic_pages_preserves_fetch_error(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "fetch_page_content", True)
    monkeypatch.setattr(service.settings.web_search, "queue_workers", 1)

    async def _raise_fetch(_url: str):
        raise TimeoutError("blocked by test")

    monkeypatch.setattr(service, "_afetch_page_data", _raise_fetch)

    pages = await service._afetch_organic_pages(
        [{"url": "https://uni-example.de/admission", "title": "Admission"}]
    )

    page = pages["https://uni-example.de/admission"]
    assert page["content"] == ""
    assert "TimeoutError" in page["fetch_error"]
    assert page["internal_links"] == []


def test_merge_extracted_page_payload_preserves_internal_links_and_fetch_error():
    merged = service._merge_extracted_page_payload(
        {
            "content": "",
            "fetch_error": "TimeoutError: blocked",
            "internal_links": [{"url": "https://uni-example.de/apply", "text": "Apply"}],
        },
        {"content": "Recovered extracted page content.", "internal_links": []},
    )

    assert merged["content"] == "Recovered extracted page content."
    assert merged["internal_links"] == [{"url": "https://uni-example.de/apply", "text": "Apply"}]
    assert merged["direct_fetch_error"] == "TimeoutError: blocked"


def test_extract_internal_links_keeps_same_domain_and_prioritizes_relevant_paths():
    html = """
    <html><body>
      <a href="/admissions/requirements">Admission requirements</a>
      <a href="https://www.uni-example.de/apply/portal">Apply now</a>
      <a href="https://blog.example.com/post">External blog</a>
      <a href="/files/regulations.pdf">Regulations PDF</a>
    </body></html>
    """
    links = service._extract_internal_links(
        html,
        base_url="https://www.uni-example.de/program/msc-ai",
        max_links=10,
    )
    urls = [str(item.get("url", "")) for item in links]
    assert "https://www.uni-example.de/admissions/requirements" in urls
    assert "https://www.uni-example.de/apply/portal" in urls
    assert "https://www.uni-example.de/files/regulations.pdf" in urls
    assert "https://blog.example.com/post" not in urls


@pytest.mark.asyncio
async def test_acrawl_internal_pages_fetches_second_level_internal_pages(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "deep_internal_crawl_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "deep_internal_crawl_max_depth", 2)
    monkeypatch.setattr(service.settings.web_search, "deep_internal_crawl_max_pages", 6)
    monkeypatch.setattr(service.settings.web_search, "deep_internal_crawl_per_parent_limit", 3)
    monkeypatch.setattr(service.settings.web_search, "deep_internal_crawl_links_per_page", 8)

    seed_rows = [
        {
            "title": "Program",
            "url": "https://www.uni-example.de/program",
            "snippet": "Program page",
            "published_date": "",
        }
    ]
    seed_page_data = {
        "https://www.uni-example.de/program": {
            "content": "Program overview.",
            "published_date": "",
            "internal_links": [
                {
                    "url": "https://www.uni-example.de/admission",
                    "text": "Admission requirements",
                    "score": 2.0,
                },
                {
                    "url": "https://www.uni-example.de/language",
                    "text": "Language requirements",
                    "score": 1.8,
                },
            ],
        }
    }

    async def _fake_fetch_pages(rows: list[dict], **_kwargs):
        payload: dict[str, dict] = {}
        for row in rows:
            url = str(row.get("url", "")).strip()
            if url.endswith("/admission"):
                payload[url] = {
                    "content": "Minimum grade 2.5 and at least 30 ECTS.",
                    "published_date": "",
                    "internal_links": [
                        {
                            "url": "https://www.uni-example.de/deadline",
                            "text": "Application deadline",
                            "score": 1.7,
                        }
                    ],
                }
            elif url.endswith("/language"):
                payload[url] = {
                    "content": "IELTS 6.5 or TOEFL iBT 90.",
                    "published_date": "",
                    "internal_links": [],
                }
            elif url.endswith("/deadline"):
                payload[url] = {
                    "content": "Application deadline is 31 May.",
                    "published_date": "",
                    "internal_links": [],
                }
        return payload

    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    rows, pages, summary = await service._acrawl_internal_pages(
        seed_rows=seed_rows,
        seed_page_data_by_url=seed_page_data,
        required_fields=[
            {"id": "admission_requirements"},
            {"id": "language_score_thresholds"},
            {"id": "application_deadline"},
        ],
        allowed_suffixes=[],
        target_domain_groups=[],
        enforce_target_domain_scope=False,
    )

    crawled_urls = {str(item.get("url", "")) for item in rows}
    assert "https://www.uni-example.de/admission" in crawled_urls
    assert "https://www.uni-example.de/language" in crawled_urls
    assert "https://www.uni-example.de/deadline" in crawled_urls
    assert "https://www.uni-example.de/deadline" in pages
    assert summary["enabled"] is True
    assert summary["depth_reached"] >= 1
    assert summary["pages_fetched"] >= 2


def test_domain_group_key_collapses_official_subdomains():
    assert service._domain_group_key("cit.tum.de") == "tum.de"
    assert service._domain_group_key("www.tum.de") == "tum.de"


def test_domain_authority_prefers_de_or_eu_over_com():
    de_score = service._domain_authority_score("https://www.lmu.de/programs/ai", [".de", ".eu"])
    eu_score = service._domain_authority_score("https://research.example.eu/ai", [".de", ".eu"])
    com_score = service._domain_authority_score("https://example.com/ai", [".de", ".eu"])

    assert de_score > com_score
    assert eu_score > com_score


def test_required_fields_from_query_detects_explicit_fields():
    fields = service._required_fields_from_query(
        "Tell me course requirements, language requirements for international students, and application deadline."
    )
    ids = [str(item.get("id", "")).strip() for item in fields]
    assert "admission_requirements" in ids
    assert "gpa_threshold" in ids
    assert "ects_breakdown" in ids
    assert "language_requirements" in ids
    assert "language_score_thresholds" in ids
    assert "application_deadline" in ids


def test_required_fields_from_query_includes_application_portal():
    fields = service._required_fields_from_query(
        "Tell me course requirements, language requirements, admission deadline, and application portal."
    )
    ids = [str(item.get("id", "")).strip() for item in fields]
    assert "admission_requirements" in ids
    assert "language_requirements" in ids
    assert "application_deadline" in ids
    assert "application_portal" in ids


def test_required_fields_from_query_detects_where_can_i_apply_as_portal():
    fields = service._required_fields_from_query(
        "tell me where can i apply for university of mannheim msc business informatics"
    )
    ids = [str(item.get("id", "")).strip() for item in fields]
    assert "application_portal" in ids


def test_required_fields_from_query_language_requirement_only_does_not_force_gpa_or_ects():
    fields = service._required_fields_from_query(
        "what is the language requirement for international students in msc business informatics"
    )
    ids = [str(item.get("id", "")).strip() for item in fields]
    assert "language_requirements" in ids
    assert "language_score_thresholds" in ids
    assert "gpa_threshold" not in ids
    assert "ects_breakdown" not in ids


def test_required_fields_from_query_language_plus_gpa_and_ects_includes_threshold_fields():
    fields = service._required_fields_from_query(
        "language requirements plus GPA and ECTS requirements for msc business informatics"
    )
    ids = [str(item.get("id", "")).strip() for item in fields]
    assert "language_requirements" in ids
    assert "language_score_thresholds" in ids
    assert "gpa_threshold" in ids
    assert "ects_breakdown" in ids


def test_required_fields_from_query_broad_program_profile_adds_depth_bundle():
    fields = service._required_fields_from_query(
        "tell me about technical university of munich msc data engineering"
    )
    ids = [str(item.get("id", "")).strip() for item in fields]
    assert "program_overview" in ids
    assert "duration_ects" in ids
    assert "admission_requirements" in ids
    assert "language_requirements" in ids
    assert "application_deadline" in ids
    assert "curriculum_modules" in ids


def test_required_field_coverage_target_is_strict_for_multi_field_university_queries():
    query = (
        "tell me about university of tubingen msc machine learning course requirements "
        "language requirements application deadline and application portal"
    )
    required_fields = service._required_fields_from_query(query)
    target = service._required_field_coverage_target(query, required_fields)
    assert target == 1.0


def test_effective_retrieval_loop_max_steps_boosts_for_program_queries(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_steps", 2)
    required_fields = service._required_fields_from_query(
        "tell me about technical university of munich msc data engineering"
    )
    boosted = service._effective_retrieval_loop_max_steps(
        "tell me about technical university of munich msc data engineering",
        required_fields,
        deep_mode=True,
    )
    assert boosted >= 4


def test_build_required_field_queries_includes_statute_and_language_routes_for_official_domain():
    query = "University of Mannheim MSc Business Informatics admission requirements"
    fields = [
        {
            "id": "gpa_threshold",
            "query_focus": "minimum GPA grade threshold admission score requirement",
        },
        {
            "id": "ects_breakdown",
            "query_focus": "required ECTS prerequisite credits mathematics computer science",
        },
        {
            "id": "language_score_thresholds",
            "query_focus": "IELTS TOEFL CEFR minimum score thresholds exact values",
        },
    ]

    queries = service._build_required_field_queries(
        query,
        missing_required_fields=fields,
        allowed_suffixes=[],
        unique_domains=["uni-mannheim.de"],
    )

    lowered = [item.lower() for item in queries]
    assert any("selection statute" in item and "site:uni-mannheim.de" in item for item in lowered)
    assert any("ielts toefl minimum score official" in item and "site:uni-mannheim.de" in item for item in lowered)
    assert any("official pdf" in item for item in lowered)


def test_build_required_field_queries_keeps_deadline_portal_and_grade_routes_together():
    query = (
        "Tell me University of Mannheim MSc Business Informatics language requirements, "
        "GPA and ECTS requirements, application deadline, and where to apply."
    )
    fields = service._required_fields_from_query(query)

    queries = service._build_required_field_queries(
        query,
        missing_required_fields=fields,
        allowed_suffixes=[],
        unique_domains=["uni-mannheim.de"],
    )

    lowered = [item.lower() for item in queries]
    assert any("application deadlines international students" in item for item in lowered)
    assert any("minimum grade" in item or "mindestnote" in item for item in lowered)
    assert any("apply online application portal" in item for item in lowered)


def test_build_required_field_queries_trim_to_tavily_safe_length():
    query = (
        "Tell me about University of Mannheim MSc Business Informatics language requirements "
        "for international students, GPA and ECTS requirements, application deadline and portal "
        "plus all official details in one response with verification and additional notes."
    )
    fields = service._required_fields_from_query(query)
    queries = service._build_required_field_queries(
        query,
        missing_required_fields=fields,
        allowed_suffixes=[".de", ".eu"],
        unique_domains=["uni-mannheim.de", "daad.de"],
    )
    assert queries
    assert max(len(item) for item in queries) <= 380
    assert len(queries) == len({item.lower() for item in queries})


def test_normalize_query_list_trims_and_dedupes_after_trim():
    long_a = "mannheim " + ("business informatics " * 60)
    long_b = (long_a + " official").strip()
    normalized = service._normalize_query_list([long_a, long_b, long_a], limit=5)
    assert normalized
    assert all(len(item) <= 380 for item in normalized)
    assert len(normalized) == len({item.lower() for item in normalized})


def test_normalize_query_list_preserves_distinct_site_and_filetype_intents():
    queries = service._normalize_query_list(
        [
            "mannheim business informatics admission requirements site:uni-mannheim.de",
            "mannheim business informatics admission requirements site:daad.de",
            "mannheim business informatics admission requirements filetype:pdf site:uni-mannheim.de",
        ],
        limit=5,
    )

    assert len(queries) == 3
    assert any("site:uni-mannheim.de" in item for item in queries)
    assert any("site:daad.de" in item for item in queries)
    assert any("filetype:pdf" in item for item in queries)


def test_required_field_coverage_for_application_portal_requires_url():
    required_fields = service._required_fields_from_query(
        "application portal for msc machine learning"
    )
    portal_only_text = [
        {
            "content": "Applications are submitted via the online application portal.",
            "metadata": {"url": "https://uni-example.de/admissions"},
        }
    ]
    with_portal_url = [
        {
            "content": "Apply online through the application portal: https://campus.uni-example.de",
            "metadata": {"url": "https://uni-example.de/admissions"},
        }
    ]
    weak = service._required_field_coverage(required_fields, portal_only_text)
    strong = service._required_field_coverage(required_fields, with_portal_url)
    assert "application_portal" in weak["missing_ids"]
    assert "application_portal" not in strong["missing_ids"]


def test_required_field_coverage_for_application_portal_accepts_direct_portal2_url():
    required_fields = service._required_fields_from_query(
        "where to apply for msc business informatics and application portal"
    )
    with_portal2_url = [
        {
            "content": "Apply here: https://portal2.uni-mannheim.de/",
            "metadata": {"url": "https://portal2.uni-mannheim.de/"},
        }
    ]
    status = service._required_field_coverage(required_fields, with_portal2_url)
    assert "application_portal" not in status["missing_ids"]


def test_extract_portal_value_rejects_generic_apply_index_source_url():
    value = service._extract_portal_value(
        "Applications are managed centrally. Please refer to the application information page.",
        "https://www.uni-mannheim.de/en/academics/before-your-studies/applying/the-a-to-z-of-applying/selection-statutes/",
    )
    assert value == ""


def test_extract_portal_value_accepts_portal_source_url():
    value = service._extract_portal_value(
        "Use the official application portal for submission.",
        "https://portal2.uni-mannheim.de/",
    )
    assert "portal2.uni-mannheim.de" in value.lower()


def test_collect_search_rows_high_precision_drops_noise_without_fallback():
    payloads = [
        {
            "organic_results": [
                {
                    "title": "Course Catalog Spring 2026",
                    "link": "https://www.uni-mannheim.de/en/academics/coming-to-mannheim/engageeu-study-offers/course-catalog-spring-2026/",
                    "snippet": "Course offerings for exchange students.",
                }
            ]
        }
    ]
    mode_token = service._RETRIEVAL_MODE_CTX.set("deep")
    query_token = service._RETRIEVAL_QUERY_CTX.set(
        "University of Mannheim MSc Business Informatics application deadline and portal"
    )
    required_token = service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.set(
        ("application_deadline", "application_portal", "language_score_thresholds")
    )
    try:
        rows = service._collect_search_rows(
            payloads,
            query_variants=["mannheim msc business informatics admissions"],
            top_k=3,
            allowed_suffixes=[],
            strict_official=False,
            target_domain_groups=[],
            enforce_target_domain_scope=False,
        )
    finally:
        service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.reset(required_token)
        service._RETRIEVAL_QUERY_CTX.reset(query_token)
        service._RETRIEVAL_MODE_CTX.reset(mode_token)
    assert rows == []


def test_retrieval_min_unique_domains_allows_single_domain_for_strict_official_scope(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "retrieval_min_unique_domains", 2)
    monkeypatch.setattr(service.settings.web_search, "deep_min_unique_domains", 2)
    mode_token = service._RETRIEVAL_MODE_CTX.set("deep")
    strict_token = service._RETRIEVAL_STRICT_OFFICIAL_CTX.set(True)
    target_token = service._RETRIEVAL_TARGET_DOMAINS_CTX.set(("uni-mannheim.de",))
    try:
        assert service._retrieval_min_unique_domains() == 1
    finally:
        service._RETRIEVAL_TARGET_DOMAINS_CTX.reset(target_token)
        service._RETRIEVAL_STRICT_OFFICIAL_CTX.reset(strict_token)
        service._RETRIEVAL_MODE_CTX.reset(mode_token)


def test_required_field_coverage_for_language_separates_requirement_and_score_slots():
    required_fields = service._required_fields_from_query(
        "language requirements for international students"
    )
    language_only = [
        {
            "content": "Applicants must provide proof of English proficiency.",
            "metadata": {"url": "https://uni-example.de/admission"},
        }
    ]
    with_scores = [
        {
            "content": "English requirement: IELTS 6.5 or TOEFL iBT 90.",
            "metadata": {"url": "https://uni-example.de/admission"},
        }
    ]

    weak = service._required_field_coverage(required_fields, language_only)
    strong = service._required_field_coverage(required_fields, with_scores)

    assert "language_requirements" not in weak["missing_ids"]
    assert "language_score_thresholds" in weak["missing_ids"]
    assert weak["coverage"] < 1.0
    assert "language_requirements" not in strong["missing_ids"]
    assert strong["coverage"] == 1.0


def test_required_field_evidence_rejects_ranking_directory_language_noise():
    rows = service._required_field_evidence_table(
        [{"id": "language_requirements", "label": "language requirements"}],
        [
            {
                "content": "Architecture Biochemistry Business Informatics German Language and Literature.",
                "metadata": {
                    "url": "https://www.daad.de/en/studying-in-germany/universities/che-ranking/?che-a=department-detail",
                    "title": "CHE Ranking Department Detail",
                    "snippet": "German Language and Literature",
                },
            }
        ],
        emit_events=False,
    )

    assert rows[0]["status"] == "missing"
    assert rows[0]["rejected_candidates"][0]["reason"] == "source_page_type_ranking_or_directory"


def test_finalize_candidates_preserves_required_field_evidence(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "max_context_results", 2)
    monkeypatch.setattr(service.settings.web_search, "deep_max_context_results", 2)
    candidates = [
        {
            "_final_score": 0.99,
            "content": "General overview of the program with no test scores.",
            "metadata": {"url": "https://uni-example.de/overview", "title": "Overview"},
        },
        {
            "_final_score": 0.98,
            "content": "Curriculum modules and student life information.",
            "metadata": {"url": "https://uni-example.de/curriculum", "title": "Curriculum"},
        },
        {
            "_final_score": 0.1,
            "content": "English requirement: IELTS 6.5 or TOEFL iBT 90.",
            "metadata": {"url": "https://uni-example.de/language", "title": "Language requirements"},
        },
    ]

    results = service._finalize_candidates(
        candidates,
        required_fields=[{"id": "language_score_thresholds", "label": "language score thresholds"}],
    )

    assert len(results) == 2
    assert any("IELTS 6.5" in item["content"] for item in results)


def test_required_field_coverage_for_gpa_and_ects_requires_numeric_thresholds():
    required_fields = service._required_fields_from_query(
        "course requirements eligibility and prerequisite credits"
    )
    weak = [
        {
            "content": "Applicants need a relevant bachelor's degree and strong background.",
            "metadata": {"url": "https://uni-example.de/admission"},
        }
    ]
    strong = [
        {
            "content": (
                "Minimum grade requirement: 2.5 (German scale). "
                "At least 30 ECTS in mathematics/computer science are required."
            ),
            "metadata": {"url": "https://uni-example.de/admission"},
        }
    ]

    weak_status = service._required_field_coverage(required_fields, weak)
    strong_status = service._required_field_coverage(required_fields, strong)

    assert "gpa_threshold" in weak_status["missing_ids"]
    assert "ects_breakdown" in weak_status["missing_ids"]
    assert "gpa_threshold" not in strong_status["missing_ids"]
    assert "ects_breakdown" not in strong_status["missing_ids"]


@pytest.mark.asyncio
async def test_deep_university_query_uses_iterative_loop_path(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "deep_standard_first_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "deterministic_controller_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_steps", 2)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_gap_queries", 1)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

    async def _should_not_use_deterministic(*_args, **_kwargs):
        raise AssertionError("Deep university queries should run through iterative retrieval loop.")

    async def _fake_plan(_query: str, _allowed_suffixes: list[str]):
        return {
            "queries": ["university of mannheim msc business informatics application deadline portal"],
            "subquestions": [],
            "planner": "heuristic",
            "llm_used": False,
        }

    async def _fake_payloads(_queries: list[str], *, top_k: int):
        _ = top_k
        return [
            {
                "organic_results": [
                    {
                        "title": "Selection Statutes",
                        "link": "https://www.uni-mannheim.de/en/academics/before-your-studies/applying/the-a-to-z-of-applying/selection-statutes/",
                        "snippet": "Program-specific statutes and admission details.",
                    },
                    {
                        "title": "Application Portal",
                        "link": "https://portal2.uni-mannheim.de/",
                        "snippet": "Apply online.",
                    },
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict], **_kwargs):
        payload: dict[str, dict] = {}
        for row in rows:
            url = str(row.get("url", "")).strip()
            if "portal2" in url:
                payload[url] = {
                    "content": "Apply online via https://portal2.uni-mannheim.de/.",
                    "published_date": "2026-03-10",
                }
            else:
                payload[url] = {
                    "content": "Language of instruction: English. IELTS 6.5. Application deadline: 15 July 2026.",
                    "published_date": "2026-03-10",
                }
        return payload

    monkeypatch.setattr(service, "_aretrieve_web_chunks_impl_deterministic", _should_not_use_deterministic)
    monkeypatch.setattr(service, "_resolve_query_plan", _fake_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks(
        "Tell me about University of Mannheim MSc Business Informatics language and deadline and portal",
        top_k=3,
        search_mode="deep",
    )

    assert result["search_mode"] == "deep"
    assert result["retrieval_loop"]["enabled"] is True
    assert result["retrieval_loop"]["iterations"] >= 1


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_deep_loop_requeries_until_required_field_is_complete(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_steps", 2)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_gap_queries", 1)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

    async def _fake_plan(_query: str, _allowed_suffixes: list[str]):
        return {
            "queries": ["uni sample msc ai language requirements"],
            "subquestions": [],
            "planner": "heuristic",
            "llm_used": False,
        }

    calls: list[list[str]] = []

    async def _fake_payloads(queries: list[str], *, top_k: int):
        _ = top_k
        calls.append(list(queries))
        query_text = " ".join(queries).lower()
        if "minimum score" in query_text or "ielts" in query_text or "toefl" in query_text:
            return [
                {
                    "organic_results": [
                        {
                            "title": "Language Requirements",
                            "link": "https://uni-example.de/language",
                            "snippet": "IELTS 6.5 or TOEFL iBT 90.",
                        }
                    ]
                }
            ]
        return [
            {
                "organic_results": [
                    {
                        "title": "Admission Overview",
                        "link": "https://uni-example.de/admission",
                        "snippet": "Proof of English proficiency is required.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict], **_kwargs):
        payload: dict[str, dict] = {}
        for row in rows:
            url = str(row.get("url", "")).strip()
            if "language" in url:
                payload[url] = {
                    "content": "Language requirement: IELTS 6.5 overall or TOEFL iBT 90.",
                    "published_date": "2026-03-10",
                }
            else:
                payload[url] = {
                    "content": "Applicants must provide proof of English proficiency.",
                    "published_date": "2026-03-10",
                }
        return payload

    monkeypatch.setattr(service, "_resolve_query_plan", _fake_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks(
        "tell me language requirements for international students",
        top_k=3,
        search_mode="deep",
    )

    assert len(calls) == 2
    assert result["verification"]["required_field_coverage"] == 1.0
    assert result["verification"]["required_fields_missing"] == []
    assert result["verification"]["verified"] is True


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_runs_required_field_rescue_when_still_missing(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_steps", 1)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_gap_queries", 1)
    monkeypatch.setattr(service.settings.web_search, "deep_required_field_rescue_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "deep_required_field_rescue_max_queries", 2)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

    async def _fake_plan(_query: str, _allowed_suffixes: list[str]):
        return {
            "queries": ["uni sample msc ai language requirements"],
            "subquestions": [],
            "planner": "heuristic",
            "llm_used": False,
        }

    calls: list[list[str]] = []

    async def _fake_payloads(queries: list[str], *, top_k: int):
        _ = top_k
        calls.append(list(queries))
        query_text = " ".join(queries).lower()
        if "ielts" in query_text or "toefl" in query_text:
            return [
                {
                    "organic_results": [
                        {
                            "title": "Language Scores",
                            "link": "https://uni-example.de/language-scores",
                            "snippet": "IELTS 6.5 or TOEFL iBT 90.",
                        }
                    ]
                }
            ]
        return [
            {
                "organic_results": [
                    {
                        "title": "Language Overview",
                        "link": "https://uni-example.de/language",
                        "snippet": "Proof of English proficiency is required.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict], **_kwargs):
        payload: dict[str, dict] = {}
        for row in rows:
            url = str(row.get("url", "")).strip()
            if "scores" in url:
                payload[url] = {
                    "content": "Accepted tests: IELTS 6.5 and TOEFL iBT 90 minimum.",
                    "published_date": "2026-03-10",
                }
            else:
                payload[url] = {
                    "content": "Applicants must provide proof of English proficiency.",
                    "published_date": "2026-03-10",
                }
        return payload

    monkeypatch.setattr(service, "_resolve_query_plan", _fake_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks(
        "tell me language requirements for international students",
        top_k=3,
        search_mode="deep",
    )

    assert len(calls) == 2
    assert any("ielts" in " ".join(batch).lower() for batch in calls[1:])
    assert result["verification"]["required_field_coverage"] == 1.0
    assert result["verification"]["required_fields_missing"] == []
    assert result["retrieval_loop"]["steps"][-1]["step"] in {2, "required_field_rescue"}


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_uses_internal_crawl_to_close_missing_language_scores(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_steps", 1)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_gap_queries", 1)
    monkeypatch.setattr(service.settings.web_search, "deep_required_field_rescue_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "deep_internal_crawl_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "deep_internal_crawl_max_depth", 2)
    monkeypatch.setattr(service.settings.web_search, "deep_internal_crawl_max_pages", 4)
    monkeypatch.setattr(service.settings.web_search, "deep_internal_crawl_links_per_page", 8)
    monkeypatch.setattr(service.settings.web_search, "deep_internal_crawl_per_parent_limit", 3)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

    async def _fake_plan(_query: str, _allowed_suffixes: list[str]):
        return {
            "queries": ["uni sample msc ai language requirements"],
            "subquestions": [],
            "planner": "heuristic",
            "llm_used": False,
        }

    async def _fake_payloads(queries: list[str], *, top_k: int):
        _ = queries, top_k
        return [
            {
                "organic_results": [
                    {
                        "title": "Language Overview",
                        "link": "https://uni-example.de/language",
                        "snippet": "Proof of English proficiency is required.",
                    }
                ]
            }
        ]

    async def _fake_fetch_pages(rows: list[dict], **_kwargs):
        payload: dict[str, dict] = {}
        for row in rows:
            url = str(row.get("url", "")).strip()
            if "language-scores" in url:
                payload[url] = {
                    "content": "Accepted tests: IELTS 6.5 and TOEFL iBT 90 minimum.",
                    "published_date": "2026-03-10",
                    "internal_links": [],
                }
            else:
                payload[url] = {
                    "content": "Applicants must provide proof of English proficiency.",
                    "published_date": "2026-03-10",
                    "internal_links": [
                        {
                            "url": "https://uni-example.de/language-scores",
                            "text": "IELTS TOEFL minimum score",
                            "score": 2.0,
                        }
                    ],
                }
        return payload

    monkeypatch.setattr(service, "_resolve_query_plan", _fake_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks(
        "tell me language requirements and ielts toefl minimum score for international students",
        top_k=3,
        search_mode="deep",
    )

    assert result["verification"]["required_field_coverage"] == 1.0
    assert result["verification"]["required_fields_missing"] == []
    steps = result["retrieval_loop"]["steps"]
    assert steps
    assert "crawl_internal_links" in set(steps[0].get("actions", []))


@pytest.mark.asyncio
async def test_aretrieve_web_chunks_routes_missing_fields_to_statute_deadline_and_portal_sources(
    monkeypatch,
):
    monkeypatch.setattr(service.settings.web_search, "multi_query_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_steps", 2)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_gap_queries", 1)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

    async def _fake_plan(_query: str, _allowed_suffixes: list[str]):
        return {
            "queries": ["university of mannheim msc business informatics"],
            "subquestions": [],
            "planner": "heuristic",
            "llm_used": False,
        }

    async def _fake_payloads(queries: list[str], *, top_k: int):
        _ = top_k
        query_text = " ".join(queries).lower()
        rows: list[dict] = []
        if (
            "selection statute" in query_text
            or "auswahlsatzung" in query_text
            or "minimum grade" in query_text
        ):
            rows.append(
                {
                    "title": "Selection Statute",
                    "link": "https://uni-mannheim.de/business-informatics-selection-statute.pdf",
                    "snippet": "Minimum grade and ECTS prerequisites.",
                }
            )
        if "ielts" in query_text or "foreign language requirements" in query_text:
            rows.append(
                {
                    "title": "Foreign Language Requirements",
                    "link": "https://uni-mannheim.de/masters-programs-foreign-language-requirements/",
                    "snippet": "IELTS and TOEFL minimum scores.",
                }
            )
        if "deadline" in query_text or "application deadlines international" in query_text:
            rows.append(
                {
                    "title": "Application Deadlines",
                    "link": "https://uni-mannheim.de/application-deadlines/",
                    "snippet": "Application deadlines for international students.",
                }
            )
        if "apply online" in query_text or "where to apply" in query_text or "portal" in query_text:
            rows.append(
                {
                    "title": "Apply Online",
                    "link": "https://portal2.uni-mannheim.de/",
                    "snippet": "Official application portal.",
                }
            )
        if not rows:
            rows.append(
                {
                    "title": "Program Overview",
                    "link": "https://uni-mannheim.de/msc-business-informatics/",
                    "snippet": "English-taught master's program.",
                }
            )
        return [{"organic_results": rows}]

    async def _fake_fetch_pages(rows: list[dict], **_kwargs):
        payload: dict[str, dict] = {}
        for row in rows:
            url = str(row.get("url", "")).strip()
            if "selection-statute" in url:
                payload[url] = {
                    "content": (
                        "Admission requirements: minimum grade 2.5. "
                        "ECTS prerequisites: at least 30 ECTS in informatics and 18 ECTS in mathematics/statistics."
                    ),
                    "published_date": "2026-03-01",
                }
            elif "foreign-language-requirements" in url:
                payload[url] = {
                    "content": "English requirement: IELTS 6.0 or TOEFL iBT 72 minimum.",
                    "published_date": "2026-03-01",
                }
            elif "application-deadlines" in url:
                payload[url] = {
                    "content": "Application deadline for international students: 15 May 2026.",
                    "published_date": "2026-03-01",
                }
            elif "portal2.uni-mannheim.de" in url:
                payload[url] = {
                    "content": "Apply online via portal: https://portal2.uni-mannheim.de/",
                    "published_date": "2026-03-01",
                }
            else:
                payload[url] = {
                    "content": (
                        "MSc Business Informatics is an English-taught master's program "
                        "with duration 4 semesters and 120 ECTS."
                    ),
                    "published_date": "2026-03-01",
                }
        return payload

    monkeypatch.setattr(service, "_resolve_query_plan", _fake_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks(
        (
            "Tell me University of Mannheim MSc Business Informatics language requirements, "
            "GPA and ECTS requirements, application deadline for international students, "
            "and where to apply."
        ),
        top_k=3,
        search_mode="deep",
    )

    assert result["verification"]["required_field_coverage"] == 1.0
    assert result["verification"]["required_fields_missing"] == []
    assert result["verification"]["verified"] is True
    assert any(
        "selection statute" in query.lower() or "application deadlines" in query.lower()
        for query in result["query_variants"]
    )


def test_required_fields_from_query_adds_instruction_language_slot():
    fields = service._required_fields_from_query(
        "Tell me the language of instruction for University of Mannheim MSc Business Informatics."
    )
    field_ids = {str(item.get("id", "")) for item in fields if isinstance(item, dict)}
    assert "instruction_language" in field_ids


def test_required_field_evidence_table_requires_typed_gpa_value():
    required_fields = [{"id": "gpa_threshold", "label": "GPA threshold"}]
    candidates = [
        {
            "content": "Admission requires a strong grade profile.",
            "metadata": {
                "url": "https://www.uni-example.de/admission",
                "source_tier": "tier0_official",
                "trust_score": 0.7,
            },
        }
    ]
    rows = service._required_field_evidence_table(required_fields, candidates)
    assert rows[0]["status"] == "missing"

    candidates[0]["content"] = "Minimum grade (Mindestnote) is 2.5 for admission."
    rows = service._required_field_evidence_table(required_fields, candidates)
    assert rows[0]["status"] == "found"
    assert rows[0]["value"] == "2.5"


def test_required_field_evidence_table_extracts_instruction_language():
    required_fields = [{"id": "instruction_language", "label": "Language of instruction"}]
    candidates = [
        {
            "content": "Language of instruction: English and German.",
            "metadata": {
                "url": "https://www.uni-example.de/program",
                "source_tier": "tier0_official",
                "trust_score": 0.8,
            },
        }
    ]
    rows = service._required_field_evidence_table(required_fields, candidates)
    assert rows[0]["status"] == "found"
    assert "English" in rows[0]["value"]


def test_student_schema_required_fields_drive_mannheim_prompt():
    fields = service._required_fields_from_query(
        "Tell me about University of Mannheim MSc Business Informatics: language of instruction, "
        "IELTS/German requirement, GPA and ECTS requirements, application deadline for "
        "international students, and where to apply. Also tell me if this course is competitive."
    )
    field_ids = [str(item.get("id", "")) for item in fields]
    assert "instruction_language" in field_ids
    assert "language_score_thresholds" in field_ids
    assert "gpa_threshold" in field_ids
    assert "ects_breakdown" in field_ids
    assert "application_deadline" in field_ids
    assert "application_portal" in field_ids
    assert "admission_decision_signal" in field_ids


def test_required_field_evidence_table_promotes_official_portal_url():
    required_fields = [{"id": "application_portal", "label": "Application portal"}]
    candidates = [
        {
            "content": "",
            "metadata": {
                "title": "Apply online",
                "snippet": "",
                "url": "https://portal2.uni-mannheim.de/portal2/pages/cs/sys/portal/hisinoneStartPage.faces",
                "source_tier": "tier0_official",
                "trust_score": 0.9,
            },
        }
    ]
    rows = service._required_field_evidence_table(required_fields, candidates)
    assert rows[0]["status"] == "found"
    assert rows[0]["value"].startswith("https://portal2.uni-mannheim.de/")


def test_required_field_evidence_table_extracts_german_admissions_terms():
    required_fields = [
        {"id": "gpa_threshold", "label": "GPA threshold"},
        {"id": "ects_breakdown", "label": "ECTS prerequisites"},
        {"id": "language_score_thresholds", "label": "Language scores"},
        {"id": "application_deadline", "label": "Application deadline"},
    ]
    candidates = [
        {
            "content": (
                "Auswahlsatzung Wirtschaftsinformatik Master: Mindestnote 2,5. "
                "Voraussetzungen: 36 ECTS in Informatik. "
                "Sprachnachweis Englisch IELTS 6,5. "
                "Bewerbungsfrist Wintersemester 31.05.2026."
            ),
            "metadata": {
                "url": "https://www.uni-mannheim.de/auswahlsatzung-wirtschaftsinformatik.pdf",
                "source_tier": "tier0_official",
                "trust_score": 0.9,
            },
        }
    ]
    rows = service._required_field_evidence_table(required_fields, candidates)
    by_id = {row["id"]: row for row in rows}
    assert by_id["gpa_threshold"]["value"] == "2.5"
    assert by_id["ects_breakdown"]["value"] == "36 ECTS"
    assert by_id["language_score_thresholds"]["value"] == "IELTS 6.5"
    assert by_id["application_deadline"]["value"] == "31.05.2026"


def test_program_specific_critical_fields_reject_related_program_evidence():
    query_token = service._RETRIEVAL_QUERY_CTX.set(
        "University of Mannheim MSc Business Informatics GPA and ECTS requirements"
    )
    required_token = service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.set(("gpa_threshold",))
    try:
        rows = service._required_field_evidence_table(
            [{"id": "gpa_threshold", "label": "GPA threshold"}],
            [
                {
                    "content": "Auswahlsatzung Master Management: Mindestnote 2.0.",
                    "metadata": {
                        "title": "Selection statute MSc Management",
                        "url": "https://www.uni-mannheim.de/auswahlsatzung-management.pdf",
                        "source_tier": "tier0_official",
                        "trust_score": 0.9,
                    },
                }
            ],
        )
    finally:
        service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.reset(required_token)
        service._RETRIEVAL_QUERY_CTX.reset(query_token)
    assert rows[0]["status"] == "missing"
    assert rows[0]["rejected_candidates"][0]["reason"] == "program_scope_mismatch"


def test_required_field_evidence_table_rejects_incoming_portal2_ilias_noise():
    query_token = service._RETRIEVAL_QUERY_CTX.set(
        "University of Mannheim MSc Business Informatics eligibility requirements and portal"
    )
    required_token = service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.set(
        ("admission_requirements", "application_portal")
    )
    try:
        rows = service._required_field_evidence_table(
            [
                {"id": "admission_requirements", "label": "Eligibility requirements"},
                {"id": "application_portal", "label": "Application portal"},
            ],
            [
                {
                    "content": (
                        "Portal2 and ILIAS for incoming students. It gives you access to course "
                        "documents such as lecture scripts, videos, PowerPoint slides, and helpful literature."
                    ),
                    "metadata": {
                        "title": "Portal2 and ILIAS",
                        "url": "https://www.wim.uni-mannheim.de/en/internationales/informationen-fuer-incomings/portal2-and-ilias-1/",
                        "source_tier": "tier0_official",
                        "trust_score": 0.9,
                    },
                }
            ],
        )
    finally:
        service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.reset(required_token)
        service._RETRIEVAL_QUERY_CTX.reset(query_token)
    by_id = {row["id"]: row for row in rows}
    assert by_id["admission_requirements"]["status"] == "missing"
    assert by_id["application_portal"]["status"] == "missing"


def test_required_field_evidence_table_rejects_mmm_study_organization_noise():
    query_token = service._RETRIEVAL_QUERY_CTX.set(
        "University of Mannheim MSc Business Informatics ECTS and language requirements"
    )
    required_token = service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.set(
        ("ects_breakdown", "language_requirements")
    )
    try:
        rows = service._required_field_evidence_table(
            [
                {"id": "ects_breakdown", "label": "ECTS prerequisites"},
                {"id": "language_requirements", "label": "Language requirements"},
            ],
            [
                {
                    "content": (
                        "MMM Study Organization Presentation. Master thesis registration requires "
                        "24 ECTS. Language requirements: German/English."
                    ),
                    "metadata": {
                        "title": "MMM Study Organization Presentation Program Management",
                        "url": "https://www.bwl.uni-mannheim.de/media/Fakultaeten/bwl/Dokumente/Studium/MMM/MMM_Study_Organization_Presentation_Program_Management.pdf",
                        "source_tier": "tier0_official",
                        "trust_score": 0.9,
                    },
                }
            ],
        )
    finally:
        service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.reset(required_token)
        service._RETRIEVAL_QUERY_CTX.reset(query_token)
    by_id = {row["id"]: row for row in rows}
    assert by_id["ects_breakdown"]["status"] == "missing"
    assert by_id["language_requirements"]["status"] == "missing"


def test_required_field_evidence_rejects_mannheim_organizing_page_as_ects_prerequisite():
    query_token = service._RETRIEVAL_QUERY_CTX.set(
        "University of Mannheim MSc Business Informatics GPA and ECTS requirements"
    )
    required_token = service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.set(("ects_breakdown",))
    try:
        rows = service._required_field_evidence_table(
            [{"id": "ects_breakdown", "label": "ECTS prerequisites"}],
            [
                {
                    "content": (
                        "MSc Business Informatics. Degree plans and course schedules. "
                        "Recognition of coursework and examinations. Modules include 4 ECTS, "
                        "12 ECTS, and 30 ECTS."
                    ),
                    "metadata": {
                        "title": "MSc Business Informatics - Organizing your studies",
                        "url": "https://www.wim.uni-mannheim.de/en/academics/organizing-your-studies/msc-business-informatics/",
                        "source_tier": "tier0_official",
                        "trust_score": 0.9,
                    },
                }
            ],
        )
    finally:
        service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.reset(required_token)
        service._RETRIEVAL_QUERY_CTX.reset(query_token)

    assert rows[0]["status"] == "missing"
    assert rows[0]["value"] == "Not verified from official sources."


def test_required_field_evidence_rejects_generic_mannheim_master_brochure_language_noise():
    query_token = service._RETRIEVAL_QUERY_CTX.set(
        "University of Mannheim MSc Business Informatics IELTS German requirement"
    )
    required_token = service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.set(("language_score_thresholds",))
    try:
        rows = service._required_field_evidence_table(
            [{"id": "language_score_thresholds", "label": "Language scores"}],
            [
                {
                    "content": (
                        "The GRE General Test. Master in Business Informatics as well as Mannheim "
                        "Master in Data Science offer extended deadlines. DSH passed with at least grade 2."
                    ),
                    "metadata": {
                        "title": "Master brochure University of Mannheim",
                        "url": "https://www.sowi.uni-mannheim.de/media/Einrichtungen/zula/Dokumente_Zula/masterbroschuere_uni_mannheim_en.pdf",
                        "source_tier": "tier0_official",
                        "trust_score": 0.9,
                    },
                }
            ],
        )
    finally:
        service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.reset(required_token)
        service._RETRIEVAL_QUERY_CTX.reset(query_token)

    assert rows[0]["status"] == "missing"
    assert rows[0]["rejected_candidates"][0]["reason"] == "source_page_type_mismatch:generic_pdf_or_brochure"


def test_required_field_evidence_rejects_bachelor_program_for_master_instruction_language():
    query_token = service._RETRIEVAL_QUERY_CTX.set(
        "University of Mannheim MSc Business Informatics language of instruction"
    )
    required_token = service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.set(("instruction_language",))
    try:
        rows = service._required_field_evidence_table(
            [{"id": "instruction_language", "label": "Language of instruction"}],
            [
                {
                    "content": "Bachelor's Program in Business Informatics. Language of instruction: German.",
                    "metadata": {
                        "title": "Bachelor's Program in Business Informatics",
                        "url": "https://www.uni-mannheim.de/en/academics/before-your-studies/programs/bsc-business-informatics/",
                        "source_tier": "tier0_official",
                        "trust_score": 0.9,
                    },
                }
            ],
        )
    finally:
        service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.reset(required_token)
        service._RETRIEVAL_QUERY_CTX.reset(query_token)

    assert rows[0]["status"] == "missing"
    assert rows[0]["rejected_candidates"][0]["reason"] == "degree_level_mismatch"


def test_required_field_evidence_rejects_going_abroad_language_proof_for_admissions():
    query_token = service._RETRIEVAL_QUERY_CTX.set(
        "University of Mannheim MSc Business Informatics IELTS TOEFL requirement"
    )
    required_token = service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.set(("language_score_thresholds",))
    try:
        rows = service._required_field_evidence_table(
            [{"id": "language_score_thresholds", "label": "Language scores"}],
            [
                {
                    "content": "TOEFL with a score of at least 80 from 120 is required.",
                    "metadata": {
                        "title": "Proof of language proficiency for studying abroad",
                        "url": "https://www.uni-mannheim.de/en/academics/going-abroad/studying-abroad/proof-of-language-proficiency/",
                        "source_tier": "tier0_official",
                        "trust_score": 0.9,
                    },
                }
            ],
        )
    finally:
        service._RETRIEVAL_REQUIRED_FIELD_IDS_CTX.reset(required_token)
        service._RETRIEVAL_QUERY_CTX.reset(query_token)

    assert rows[0]["status"] == "missing"
    assert rows[0]["value"] == "Not verified from official sources."


@pytest.mark.asyncio
async def test_deep_retrieval_respects_total_query_budget(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "deep_standard_first_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "query_planner_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_enabled", True)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_steps", 4)
    monkeypatch.setattr(service.settings.web_search, "retrieval_loop_max_gap_queries", 2)
    monkeypatch.setattr(service.settings.web_search, "deep_search_max_queries", 8)
    monkeypatch.setattr(service.settings.web_search, "deep_total_query_budget", 24)
    monkeypatch.setattr(service.settings.web_search, "german_total_query_budget", 7)
    monkeypatch.setattr(service.settings.web_search, "deep_required_field_rescue_enabled", False)
    monkeypatch.setattr(service.settings.web_search, "allowed_domain_suffixes", [])

    async def _fake_plan(_query: str, _allowed_suffixes: list[str]):
        return {
            "queries": [
                "mannheim business informatics admissions",
                "mannheim business informatics deadlines",
                "mannheim business informatics portal",
                "mannheim business informatics language score",
                "mannheim business informatics auswahlsatzung",
            ],
            "subquestions": ["deadline", "portal", "ielts", "ects"],
            "planner": "heuristic",
            "llm_used": False,
        }

    calls: list[list[str]] = []

    async def _fake_payloads(queries: list[str], *, top_k: int):
        calls.append(list(queries))
        return [
            {
                "organic_results": [
                    {
                        "title": "University of Mannheim Program",
                        "link": f"https://www.uni-mannheim.de/en/{index}",
                        "snippet": "Official admissions information page.",
                    }
                ]
            }
            for index, _query in enumerate(queries, start=1)
        ]

    async def _fake_fetch_pages(rows: list[dict], **_kwargs):
        return {
            str(row.get("url", "")): {"content": "General admissions info without all required fields."}
            for row in rows
        }

    monkeypatch.setattr(service, "_resolve_query_plan", _fake_plan)
    monkeypatch.setattr(service, "_asearch_payloads", _fake_payloads)
    monkeypatch.setattr(service, "_afetch_organic_pages", _fake_fetch_pages)

    result = await service.aretrieve_web_chunks(
        "University of Mannheim MSc Business Informatics IELTS ECTS deadline and portal",
        top_k=3,
        search_mode="deep",
    )

    total_executed = sum(len(batch) for batch in calls)
    budget = result["retrieval_budget_usage"]["query_budget"]
    assert budget == 7
    assert total_executed <= budget
    assert len(result["query_variants"]) <= budget
    assert result["retrieval_budget_usage"]["budget_exhausted"] is True
