from urllib.parse import urlparse
from app.core.config import get_settings

TIER0_OFFICIAL = "tier0_official"
TIER1_CORROBORATION = "tier1_corroboration"
TIER2_SECONDARY = "tier2_secondary"

_TIER_RANK = {
    TIER0_OFFICIAL: 3,
    TIER1_CORROBORATION: 2,
    TIER2_SECONDARY: 1,
}

_TIER1_HOST_MARKERS = (
    "daad",
    "europa.eu",
    ".gov",
    "anabin",
    "kmk.org",
    "study-in-germany",
)
_OFFICIAL_HOST_MARKERS = (
    "uni",
    "university",
    "universit",
    "hochschule",
    "tu-",
    "th-",
    "fh-",
)


def _settings():
    return get_settings()


def _normalize_host(url: str) -> str:
    host = str(urlparse(str(url or "")).hostname or "").strip().lower()
    if host.startswith("www."):
        return host[4:]
    return host


def _host_matches(host: str, domain: str) -> bool:
    normalized_host = str(host or "").strip().lower()
    normalized_domain = str(domain or "").strip().lower()
    if normalized_domain.startswith("www."):
        normalized_domain = normalized_domain[4:]
    if not normalized_host or not normalized_domain:
        return False
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def classify_source_tier(url: str, *, title: str = "", snippet: str = "") -> dict:
    host = _normalize_host(url)
    settings = _settings()
    allowlist = [
        str(item).strip().lower()
        for item in getattr(settings.web_search, "official_source_allowlist", [])
        if str(item).strip()
    ]
    if any(_host_matches(host, domain) for domain in allowlist):
        return {
            "url": str(url).strip(),
            "host": host,
            "source_tier": TIER0_OFFICIAL,
            "tier_rank": _TIER_RANK[TIER0_OFFICIAL],
            "reason": "allowlisted_official_domain",
        }
    if host and any(marker in host for marker in _TIER1_HOST_MARKERS):
        return {
            "url": str(url).strip(),
            "host": host,
            "source_tier": TIER1_CORROBORATION,
            "tier_rank": _TIER_RANK[TIER1_CORROBORATION],
            "reason": "corroboration_domain",
        }
    host_text = host.replace("-", " ")
    if host and any(marker in host_text for marker in _OFFICIAL_HOST_MARKERS):
        return {
            "url": str(url).strip(),
            "host": host,
            "source_tier": TIER0_OFFICIAL,
            "tier_rank": _TIER_RANK[TIER0_OFFICIAL],
            "reason": "institutional_domain_pattern",
        }
    evidence_text = " ".join(str(value) for value in (title, snippet)).lower()
    if any(marker in evidence_text for marker in ("university", "faculty", "department", "admission")):
        return {
            "url": str(url).strip(),
            "host": host,
            "source_tier": TIER1_CORROBORATION,
            "tier_rank": _TIER_RANK[TIER1_CORROBORATION],
            "reason": "institutional_context_in_snippet",
        }
    return {
        "url": str(url).strip(),
        "host": host,
        "source_tier": TIER2_SECONDARY,
        "tier_rank": _TIER_RANK[TIER2_SECONDARY],
        "reason": "secondary_source",
    }


def slot_allows_tier(slot: dict, source_tier: str) -> bool:
    required = str(slot.get("source_tier_requirement", TIER0_OFFICIAL)).strip().lower()
    tier = str(source_tier or "").strip().lower()
    if required == TIER0_OFFICIAL:
        return tier == TIER0_OFFICIAL
    if required == TIER1_CORROBORATION:
        return tier in {TIER0_OFFICIAL, TIER1_CORROBORATION}
    return tier in {TIER0_OFFICIAL, TIER1_CORROBORATION, TIER2_SECONDARY}


def build_source_policy_decisions(
    *,
    required_slots: list[dict],
    candidates: list[dict],
    canonical_facts: list[dict] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    seen_urls: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        metadata = candidate.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        url = str(metadata.get("url", "")).strip()
        if not url:
            continue
        key = url.lower()
        if key in seen_urls:
            continue
        seen_urls.add(key)
        classification = classify_source_tier(
            url,
            title=str(metadata.get("title", "")),
            snippet=str(metadata.get("snippet", "")),
        )
        allowance = {
            str(slot.get("slot_id", "")).strip(): slot_allows_tier(slot, classification["source_tier"])
            for slot in required_slots
            if str(slot.get("slot_id", "")).strip()
        }
        rows.append(
            {
                "url": url,
                "host": classification.get("host", ""),
                "source_tier": classification.get("source_tier", TIER2_SECONDARY),
                "reason": classification.get("reason", ""),
                "slot_allowance": allowance,
            }
        )
    for fact in canonical_facts or []:
        if not isinstance(fact, dict):
            continue
        url = str(fact.get("source_url", "")).strip()
        if not url:
            continue
        key = url.lower()
        if key in seen_urls:
            continue
        seen_urls.add(key)
        rows.append(
            {
                "url": url,
                "host": _normalize_host(url),
                "source_tier": str(fact.get("source_tier", TIER0_OFFICIAL)).strip() or TIER0_OFFICIAL,
                "reason": "canonical_knowledge_layer",
                "slot_allowance": {},
            }
        )
    return rows[:20]
