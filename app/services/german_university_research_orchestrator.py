import asyncio
import logging
import re
import time
import urllib.request
from html import unescape
from typing import Awaitable, Callable
from urllib.parse import urldefrag, urljoin

from app.core.config import get_settings
from app.services.german_evidence_extractor import (
    coverage_score,
    extract_german_evidence_rows,
    unresolved_slots,
)
from app.services.german_source_policy import (
    TIER0_OFFICIAL,
    TIER1_CORROBORATION,
    classify_german_source,
    discover_official_domains,
    validate_german_program_scope,
)
from app.services.german_source_routes import (
    GermanResearchTask,
    build_discovery_queries,
    build_slot_route_queries,
    is_likely_german_university_query,
    research_plan_for_task,
    resolve_german_research_task,
)
from app.services.tavily_search_service import asearch_google_batch

try:
    from app.services.chat_trace_service import emit_trace_event
except Exception:  # pragma: no cover - trace service optional in isolated tests
    def emit_trace_event(*_args, **_kwargs):
        return None

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover - optional dependency
    PdfReader = None

settings = get_settings()
logger = logging.getLogger(__name__)

SearchBatchFn = Callable[[list[str]], Awaitable[list[dict]]]
PageFetchFn = Callable[[list[dict]], Awaitable[dict[str, dict]]]

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", flags=re.IGNORECASE | re.DOTALL)
_BLOCK_RE = re.compile(r"</?(p|br|li|tr|td|th|div|section|article|h[1-6])\b[^>]*>", flags=re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_ANCHOR_RE = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*['\"]?([^'\"\s>]+)[^>]*>(.*?)</a>",
    flags=re.IGNORECASE | re.DOTALL,
)
_PDF_HINT_RE = re.compile(
    r"\b(auswahlsatzung|zulassungssatzung|pruefungsordnung|prüfungsordnung|modulhandbuch|selection statute)\b|\.pdf(\b|$|\?)",
    flags=re.IGNORECASE,
)
_OFFICIAL_ROUTE_LINK_RE = re.compile(
    r"\b(admission|admissions|application|apply|deadline|deadlines|selection|requirements?|"
    r"bewerbung|bewerbungsfrist|bewerbungsportal|zulassung|zulassungssatzung|auswahlsatzung|"
    r"sprachnachweis|sprachkenntnisse|ielts|toefl|ects|mindestnote|portal|online application|"
    r"pruefungsordnung|prüfungsordnung|modulhandbuch)\b|\.pdf($|\?)",
    flags=re.IGNORECASE,
)


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _normalize(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


def _strip_html(raw_html: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", str(raw_html or ""))
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    return _normalize(unescape(text))


def _extract_links(raw_html: str, *, base_url: str, limit: int = 120) -> list[dict]:
    links: list[dict] = []
    seen: set[str] = set()
    for match in _ANCHOR_RE.finditer(str(raw_html or "")):
        href = unescape(str(match.group(1) or "")).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        if not url or url.lower() in seen:
            continue
        label = _strip_html(str(match.group(2) or ""))
        seen.add(url.lower())
        links.append({"url": url, "title": label[:180], "snippet": label[:260]})
        if len(links) >= limit:
            break
    return links


def _extract_pdf_text(raw_bytes: bytes, *, max_chars: int) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(__import__("io").BytesIO(raw_bytes))
    except Exception:
        return ""
    chunks: list[str] = []
    for page in reader.pages[:8]:
        try:
            chunks.append(str(page.extract_text() or ""))
        except Exception:
            continue
        if len(" ".join(chunks)) >= max_chars:
            break
    return _normalize(" ".join(chunks))[:max_chars]


async def _fetch_one_page(url: str, *, timeout_seconds: float, max_chars: int) -> dict:
    clean_url = urldefrag(str(url or "").strip())[0]
    if not clean_url:
        return {"content": "", "url": url}

    def _read() -> dict:
        request = urllib.request.Request(
            clean_url,
            headers={"User-Agent": "unigraph-germany-researcher/1.0"},
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(max(2048, max_chars * 4))
            content_type = str(response.headers.get("content-type", "")).lower()
        if "pdf" in content_type or clean_url.lower().endswith(".pdf"):
            return {"content": _extract_pdf_text(raw, max_chars=max_chars), "url": clean_url, "links": []}
        text = raw.decode("utf-8", errors="ignore")
        return {
            "content": _strip_html(text)[:max_chars],
            "url": clean_url,
            "links": _extract_links(text, base_url=clean_url),
        }

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.debug("German researcher page fetch failed url=%s error=%s", clean_url, exc)
        return {"content": "", "url": clean_url, "error": str(exc)}


async def default_page_fetcher(rows: list[dict]) -> dict[str, dict]:
    timeout_seconds = float(getattr(settings.web_search, "page_fetch_timeout_seconds", 8.0) or 8.0)
    max_chars = int(getattr(settings.web_search, "max_page_chars", 8000) or 8000)
    max_pages = min(20, max(1, int(getattr(settings.web_search, "deep_max_pages_to_fetch", 8) or 8)))
    urls: list[str] = []
    seen: set[str] = set()
    ranked = sorted(
        [row for row in rows if isinstance(row, dict)],
        key=lambda row: 1 if _PDF_HINT_RE.search(str(row.get("url", ""))) else 0,
        reverse=True,
    )
    for row in ranked:
        url = str(row.get("url", row.get("link", ""))).strip()
        url = urldefrag(url)[0]
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= max_pages:
            break
    payloads = await asyncio.gather(
        *[
            _fetch_one_page(url, timeout_seconds=timeout_seconds, max_chars=max_chars)
            for url in urls
        ]
    )
    return {str(item.get("url", "") or url): item for url, item in zip(urls, payloads)}


def _rows_from_search_payloads(payloads: list[dict]) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        result = payload.get("result", payload)
        if not isinstance(result, dict):
            continue
        organic = result.get("organic_results", result.get("results", []))
        if not isinstance(organic, list):
            continue
        for item in organic:
            if not isinstance(item, dict):
                continue
            url = str(item.get("link", item.get("url", ""))).strip()
            if not url:
                continue
            url = urldefrag(url)[0]
            key = url.lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "title": _normalize(str(item.get("title", ""))),
                    "url": url,
                    "snippet": _normalize(str(item.get("snippet", item.get("content", "")))),
                    "published_date": _normalize(str(item.get("date", item.get("published_date", "")))),
                }
            )
    return rows


def _accepted_research_source(row: dict, *, task: GermanResearchTask, content: str = "") -> bool:
    classification = classify_german_source(
        str(row.get("url", "")),
        title=str(row.get("title", "")),
        snippet=str(row.get("snippet", "")),
        institution=task.institution,
    )
    if str(classification.get("source_tier", "")) not in {TIER0_OFFICIAL, TIER1_CORROBORATION}:
        return False
    scope = validate_german_program_scope(
        str(row.get("url", "")),
        title=str(row.get("title", "")),
        snippet=str(row.get("snippet", "")),
        content=content,
        program=task.program,
        degree_level=task.degree_level,
    )
    return bool(scope.get("accepted", False))


def _route_link_rows_from_pages(
    seed_rows: list[dict],
    page_data_by_url: dict[str, dict],
    *,
    task: GermanResearchTask,
    limit: int = 32,
) -> list[dict]:
    output: list[dict] = []
    seen: set[str] = set()
    for seed in seed_rows:
        if not isinstance(seed, dict):
            continue
        seed_url = str(seed.get("url", "")).strip()
        payload = page_data_by_url.get(seed_url, {})
        payload = payload if isinstance(payload, dict) else {}
        links = payload.get("links", [])
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, dict):
                continue
            url = urldefrag(str(link.get("url", "")).strip())[0]
            if not url or url.lower() in seen:
                continue
            title = _normalize(str(link.get("title", "")))
            snippet = _normalize(str(link.get("snippet", title)))
            route_text = f"{url} {title} {snippet}"
            if not _OFFICIAL_ROUTE_LINK_RE.search(route_text):
                continue
            row = {
                "title": title or "Official linked admission source",
                "url": url,
                "snippet": snippet,
                "published_date": "",
                "discovered_from": seed_url,
            }
            if not _accepted_research_source(row, task=task):
                continue
            seen.add(url.lower())
            output.append(row)
            if len(output) >= limit:
                return output
    return output


def _source_payloads_from_rows(
    rows: list[dict],
    page_data_by_url: dict[str, dict],
    *,
    task: GermanResearchTask,
) -> list[dict]:
    sources: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url", "")).strip()
        page_payload = page_data_by_url.get(url, {})
        page_payload = page_payload if isinstance(page_payload, dict) else {}
        content = _normalize(str(page_payload.get("content", "")))
        if not _accepted_research_source(row, task=task, content=content):
            continue
        sources.append(
            {
                "title": str(row.get("title", "")),
                "url": url,
                "snippet": str(row.get("snippet", "")),
                "published_date": str(row.get("published_date", "")),
                "content": content,
            }
        )
    return sources


def _result_rows_from_evidence(evidence_rows: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in evidence_rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status", "")).strip().lower() != "found":
            continue
        url = str(row.get("source_url", "")).strip()
        if not url:
            continue
        content = _normalize(str(row.get("evidence_text", row.get("evidence_snippet", ""))))
        if not content:
            continue
        item = grouped.setdefault(
            url,
            {
                "url": url,
                "source_tier": str(row.get("source_tier", "")),
                "retrieved_at": str(row.get("retrieved_at", "")),
                "confidence": 0.0,
                "labels": [],
                "snippets": [],
            },
        )
        item["confidence"] = max(float(item.get("confidence", 0.0) or 0.0), float(row.get("confidence", 0.0) or 0.0))
        label = str(row.get("label", "")).strip()
        if label and label not in item["labels"]:
            item["labels"].append(label)
        if content not in item["snippets"]:
            item["snippets"].append(content)

    results: list[dict] = []
    for index, item in enumerate(grouped.values(), start=1):
        content = _normalize(" ".join(str(snippet) for snippet in item.get("snippets", [])))
        if not content:
            continue
        results.append(
            {
                "chunk_id": f"german-research:{index}",
                "source_path": str(item.get("url", "")),
                "distance": round(max(0.0, 1.0 - float(item.get("confidence", 0.0) or 0.0)), 4),
                "content": content,
                "metadata": {
                    "title": "; ".join(item.get("labels", [])[:4]) or "German Research Evidence",
                    "section_heading": "German Research Evidence",
                    "url": str(item.get("url", "")),
                    "published_date": str(item.get("retrieved_at", "")),
                    "source_type": "german_researcher",
                    "source_tier": str(item.get("source_tier", "")),
                    "trust_score": float(item.get("confidence", 0.0) or 0.0),
                },
            }
        )
    return results


class GermanUniversityResearchOrchestrator:
    def __init__(
        self,
        *,
        search_batch: SearchBatchFn | None = None,
        page_fetcher: PageFetchFn | None = None,
    ) -> None:
        self._search_batch = search_batch or self._default_search_batch
        self._page_fetcher = page_fetcher or default_page_fetcher

    async def _default_search_batch(self, queries: list[str]) -> list[dict]:
        if not queries:
            return []
        return await asearch_google_batch(
            queries,
            num=max(4, int(getattr(settings.web_search, "deep_default_num", 6) or 6)),
            search_depth="advanced",
            include_answer=False,
        )

    async def _sources_from_rows(
        self,
        rows: list[dict],
        *,
        task: GermanResearchTask,
        existing_page_data: dict[str, dict] | None = None,
    ) -> tuple[list[dict], dict[str, dict], list[dict]]:
        page_data = dict(existing_page_data or {})
        accepted_rows = [row for row in rows if _accepted_research_source(row, task=task)]
        rows_to_fetch = [
            row
            for row in accepted_rows
            if str(row.get("url", "")).strip()
            and str(row.get("url", "")).strip() not in page_data
        ]
        if rows_to_fetch:
            page_data.update(await self._page_fetcher(rows_to_fetch))

        linked_rows = _route_link_rows_from_pages(accepted_rows, page_data, task=task)
        if linked_rows:
            rows = self._dedupe_rows(rows + linked_rows, limit=96)
            accepted_rows = [row for row in rows if _accepted_research_source(row, task=task)]
            linked_to_fetch = [
                row
                for row in accepted_rows
                if str(row.get("url", "")).strip()
                and str(row.get("url", "")).strip() not in page_data
            ]
            if linked_to_fetch:
                page_data.update(await self._page_fetcher(linked_to_fetch))

        sources = _source_payloads_from_rows(accepted_rows, page_data, task=task)
        return rows, page_data, sources

    async def research(self, query: str) -> dict:
        started_at = time.perf_counter()
        task = resolve_german_research_task(query)
        if not is_likely_german_university_query(query):
            return {
                "applicable": False,
                "query": query,
                "research_strategy": "germany_researcher_not_applicable",
                "results": [],
                "coverage_ledger": [],
                "unresolved_slots": [],
            }

        emit_trace_event(
            "german_research_started",
            {
                "institution": task.institution,
                "program": task.program,
                "required_slots": list(task.required_slots),
            },
        )

        discovery_queries = build_discovery_queries(task, max_queries=6)
        discovery_payloads = await self._search_batch(discovery_queries)
        discovery_rows = _rows_from_search_payloads(discovery_payloads)
        official_domains = discover_official_domains(
            discovery_rows,
            institution=task.institution,
            limit=4,
        )

        route_queries = build_slot_route_queries(
            task,
            official_domains=official_domains,
            max_queries=24,
        )
        route_payloads = await self._search_batch(route_queries)
        route_rows = _rows_from_search_payloads(route_payloads)
        rows = self._dedupe_rows(discovery_rows + route_rows)
        rows, page_data, sources = await self._sources_from_rows(rows, task=task)
        evidence_rows = extract_german_evidence_rows(
            sources,
            required_slots=task.required_slots,
            institution=task.institution,
        )

        missing = unresolved_slots(evidence_rows)
        rescue_queries: list[str] = []
        if missing:
            rescue_queries = build_slot_route_queries(
                task,
                official_domains=official_domains,
                missing_slots=missing,
                max_queries=18,
            )
            if rescue_queries:
                rescue_payloads = await self._search_batch(rescue_queries)
                rescue_rows = _rows_from_search_payloads(rescue_payloads)
                rows = self._dedupe_rows(rows + rescue_rows)
                rows, page_data, sources = await self._sources_from_rows(
                    rows,
                    task=task,
                    existing_page_data=page_data,
                )
                evidence_rows = extract_german_evidence_rows(
                    sources,
                    required_slots=task.required_slots,
                    institution=task.institution,
                )
                missing = unresolved_slots(evidence_rows)

        coverage = coverage_score(evidence_rows)
        results = _result_rows_from_evidence(evidence_rows)
        source_urls = []
        seen_urls: set[str] = set()
        for row in evidence_rows:
            url = str(row.get("source_url", "")).strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            source_urls.append(url)

        plan = research_plan_for_task(task, official_domains=official_domains)
        plan["route_queries"] = route_queries
        plan["rescue_queries"] = rescue_queries

        emit_trace_event(
            "german_research_completed",
            {
                "coverage": coverage,
                "unresolved_slots": missing,
                "official_domains": official_domains,
                "source_count": len(source_urls),
            },
        )

        return {
            "applicable": True,
            "query": query,
            "research_strategy": "germany_researcher",
            "query_variants": discovery_queries + route_queries + rescue_queries,
            "query_plan": plan,
            "verification": {
                "verified": not missing,
                "required_field_coverage": coverage,
                "required_fields_missing": missing,
                "unresolved_fields": missing,
                "source_policy": "german_official_first",
                "official_domains": official_domains,
                "source_urls": source_urls,
            },
            "coverage_summary": {
                "required_field_coverage": coverage,
                "unresolved_fields": missing,
                "source_policy": "german_official_first",
            },
            "coverage_ledger": evidence_rows,
            "field_evidence": evidence_rows,
            "evidence_ledger": evidence_rows,
            "unresolved_slots": missing,
            "source_routes_attempted": {
                "discovery": discovery_queries,
                "slot_routes": route_queries,
                "rescue": rescue_queries,
            },
            "results": results,
            "timings_ms": {"total": _elapsed_ms(started_at)},
        }

    @staticmethod
    def _dedupe_rows(rows: list[dict], *, limit: int = 48) -> list[dict]:
        output: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url", row.get("link", ""))).strip()
            if not url:
                continue
            key = urldefrag(url)[0].lower()
            if key in seen:
                continue
            seen.add(key)
            output.append(dict(row))
            if len(output) >= limit:
                break
        return output


async def research_german_university(query: str) -> dict:
    return await GermanUniversityResearchOrchestrator().research(query)
