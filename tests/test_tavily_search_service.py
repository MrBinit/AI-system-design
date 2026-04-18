import pytest

from app.services import tavily_search_service as service


def test_search_google_normalizes_tavily_payload(monkeypatch):
    monkeypatch.setattr(service.settings.web_search, "enabled", True)
    monkeypatch.setenv("TAVILY_WEB_SEARCH", "test-key")

    captured = {}

    monkeypatch.setattr(service.settings.web_search, "search_depth", "basic")

    def _fake_request_json(
        query: str, *, timeout_seconds: float, num: int, search_depth: str = "advanced"
    ):
        captured["query"] = query
        captured["timeout"] = timeout_seconds
        captured["num"] = num
        captured["search_depth"] = search_depth
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
    assert result["organic_results"][0]["title"] == "TUM AI"
    assert result["organic_results"][0]["link"] == "https://www.tum.de/ai"
    assert result["ai_overview"]["text"] == "summary answer"


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
