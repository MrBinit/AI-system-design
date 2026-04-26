import pytest

from app.services import tavily_search_service as service


def test_search_google_normalizes_tavily_payload(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "enabled", True)
    monkeypatch.setenv("TAVILY_WEB_SEARCH", "test-key")

    captured = {}

    monkeypatch.setattr(service.settings.web_search, "search_depth", "basic")

    def _fake_request_json(query: str, *, timeout_seconds: float, num: int, search_depth: str = "advanced", **kwargs):
        captured["query"] = query
        captured["timeout"] = timeout_seconds
        captured["num"] = num
        captured["search_depth"] = search_depth
        captured["kwargs"] = kwargs
        return {
            "answer": "summary answer",
            "results": [
                {
                    "title": "TUM AI",
                    "url": "https://www.tum.de/ai",
                    "content": "Program information",
                    "published_date": "2026-04-17",
                }
            ],
        }

    monkeypatch.setattr(service, "_request_json", _fake_request_json)

    result = service.search_google("oxford ai masters", gl="us", hl="en", num=5)
    assert captured["query"] == "oxford ai masters"
    assert captured["num"] == 5
    assert captured["search_depth"] == "basic"
    assert captured["kwargs"]["include_answer"] is None
    assert result["organic_results"][0]["title"] == "TUM AI"
    assert result["organic_results"][0]["link"] == "https://www.tum.de/ai"
    assert result["ai_overview"]["text"] == "summary answer"


def test_normalize_tavily_payload_uses_raw_content_when_content_missing():
    payload = service._normalize_tavily_payload(
        {
            "results": [
                {
                    "title": "Admissions Statute",
                    "url": "https://uni.example.edu/statute.pdf",
                    "raw_content": "A" * 2400,
                    "published_date": "2026-01-12",
                }
            ]
        },
        query="uni admissions",
    )

    assert payload["organic_results"][0]["title"] == "Admissions Statute"
    assert payload["organic_results"][0]["link"] == "https://uni.example.edu/statute.pdf"
    assert len(payload["organic_results"][0]["snippet"]) == 2000


@pytest.mark.asyncio
async def test_asearch_google_batch_uses_queue_workers(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "enabled", True)
    monkeypatch.setattr(service.settings.web_search, "queue_workers", 2)
    monkeypatch.setattr(service.settings.web_search, "queue_max_size", 10)

    async def _fake_asearch_google(query: str, **kwargs):
        if query == "bad":
            raise RuntimeError("boom")
        return {"query_echo": query}

    monkeypatch.setattr(service, "asearch_google", _fake_asearch_google)
    results = await service.asearch_google_batch(["q1", "bad", "q2"])

    assert [item["query"] for item in results] == ["q1", "bad", "q2"]
    assert results[0]["result"]["query_echo"] == "q1"
    assert "boom" in results[1]["error"]
    assert results[2]["result"]["query_echo"] == "q2"


@pytest.mark.asyncio
async def test_aextract_urls_passes_depth_and_query(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "enabled", True)
    monkeypatch.setattr(service.settings.web_search, "timeout_seconds", 10.0)

    captured = {}

    def _fake_extract_sync(urls: list[str], *, extract_depth: str, query: str | None):
        captured["urls"] = urls
        captured["extract_depth"] = extract_depth
        captured["query"] = query
        return {"results": [{"url": urls[0], "raw_content": "Extracted text"}], "failed_results": []}

    monkeypatch.setattr(service, "_extract_tavily_sync", _fake_extract_sync)

    payload = await service.aextract_urls(
        [" https://uni.example.edu/admissions ", "", "https://uni.example.edu/statute.pdf"],
        extract_depth="advanced",
        query="admission requirements",
    )

    assert captured["urls"] == [
        "https://uni.example.edu/admissions",
        "https://uni.example.edu/statute.pdf",
    ]
    assert captured["extract_depth"] == "advanced"
    assert captured["query"] == "admission requirements"
    assert payload["results"][0]["url"] == "https://uni.example.edu/admissions"
