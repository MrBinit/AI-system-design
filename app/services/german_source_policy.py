import re
from urllib.parse import urlparse

TIER0_OFFICIAL = "tier0_official"
TIER1_CORROBORATION = "tier1_corroboration"
TIER2_DISCOVERY = "tier2_discovery"

_GERMAN_HIGHER_ED_HOST_RE = re.compile(
    r"(^|[-.])(uni|tu|th|fh|hs|rwth|kit|hwr|htw|haw|hhu|tum)([-.]|$)|"
    r"(universitaet|universitat|university|hochschule|technische-universitaet)",
    flags=re.IGNORECASE,
)
_DISCOVERY_BLOCKLIST_RE = re.compile(
    r"(studyportals|mastersportal|bachelorsportal|edu-link|mystipendium|"
    r"reddit|quora|linkedin|wikipedia|ranking|consult|blog|forum)",
    flags=re.IGNORECASE,
)
_TIER1_DOMAINS = (
    "daad.de",
    "www2.daad.de",
    "hochschulkompass.de",
    "uni-assist.de",
    "anabin.kmk.org",
    "kmk.org",
    "study-in-germany.de",
)
_PROGRAM_STOPWORDS = {
    "bachelor",
    "bachelors",
    "business",
    "degree",
    "in",
    "master",
    "masters",
    "msc",
    "m",
    "of",
    "program",
    "programme",
    "science",
    "sc",
}
_MASTER_RE = re.compile(r"\b(master|masters|m\.?\s*sc|msc|m\.a\.|ma)\b", flags=re.IGNORECASE)
_BACHELOR_RE = re.compile(r"\b(bachelor|bachelors|b\.?\s*sc|bsc|b\.a\.|ba)\b", flags=re.IGNORECASE)
_EXCHANGE_RE = re.compile(
    r"\b(exchange[-\s]?students?|incoming[-\s]?students?|erasmus|study abroad|visiting students?)\b",
    flags=re.IGNORECASE,
)
_PROGRAM_SPECIFIC_RE = re.compile(
    r"\b(master|masters|msc|m\.sc|bachelor|bsc|programs?|programme|studiengang|"
    r"auswahlsatzung|zulassungssatzung|selection statute|pruefungsordnung|prüfungsordnung|"
    r"modulhandbuch|po_msc|po_bsc|degree)\b|\.pdf(\b|$|\?)",
    flags=re.IGNORECASE,
)


def normalize_host(url_or_host: str) -> str:
    value = str(url_or_host or "").strip().lower()
    if not value:
        return ""
    if "://" in value:
        value = str(urlparse(value).hostname or "").strip().lower()
    value = value.removeprefix("www.")
    return value


def _scope_text(*values: str) -> str:
    return " ".join(str(value or "").replace("_", " ").replace("-", " ").lower() for value in values)


def _program_tokens(program: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9äöüß]+", str(program or "").lower())
        if len(token) >= 4 and token not in _PROGRAM_STOPWORDS
    }
    return tokens


def domain_group(host: str) -> str:
    normalized = normalize_host(host)
    if not normalized:
        return ""
    parts = [part for part in normalized.split(".") if part]
    if len(parts) <= 2:
        return normalized
    return ".".join(parts[-2:])


def host_matches_domain(host: str, domain: str) -> bool:
    normalized_host = normalize_host(host)
    normalized_domain = normalize_host(domain)
    if not normalized_host or not normalized_domain:
        return False
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def is_tier1_german_education_source(url_or_host: str) -> bool:
    host = normalize_host(url_or_host)
    return any(host_matches_domain(host, domain) for domain in _TIER1_DOMAINS)


def looks_like_official_german_university_source(
    url: str,
    *,
    title: str = "",
    snippet: str = "",
    institution: str = "",
) -> bool:
    host = normalize_host(url)
    if not host or not host.endswith(".de"):
        return False
    if _DISCOVERY_BLOCKLIST_RE.search(host):
        return False
    if is_tier1_german_education_source(host):
        return False
    if _GERMAN_HIGHER_ED_HOST_RE.search(host):
        return True
    evidence_text = f"{title} {snippet}".lower()
    if institution:
        institution_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", institution.lower())
            if len(token) >= 4 and token not in {"university", "universitat", "universitaet"}
        }
        if institution_tokens and institution_tokens & set(re.findall(r"[a-z0-9]+", evidence_text)):
            return True
    return bool(
        re.search(
            r"\b(university|universitaet|universitat|hochschule|faculty|department|study program)\b",
            evidence_text,
        )
    )


def classify_german_source(
    url: str,
    *,
    title: str = "",
    snippet: str = "",
    institution: str = "",
) -> dict:
    host = normalize_host(url)
    if looks_like_official_german_university_source(
        url,
        title=title,
        snippet=snippet,
        institution=institution,
    ):
        return {
            "source_tier": TIER0_OFFICIAL,
            "source_type": "official",
            "host": host,
            "reason": "official_german_university_domain",
        }
    if is_tier1_german_education_source(url):
        return {
            "source_tier": TIER1_CORROBORATION,
            "source_type": "corroboration",
            "host": host,
            "reason": "recognized_german_education_source",
        }
    return {
        "source_tier": TIER2_DISCOVERY,
        "source_type": "discovery",
        "host": host,
        "reason": "discovery_source",
    }


def validate_german_program_scope(
    url: str,
    *,
    title: str = "",
    snippet: str = "",
    content: str = "",
    program: str = "",
    degree_level: str = "",
) -> dict:
    """Reject official-looking pages that clearly belong to the wrong program/audience."""
    short_text = _scope_text(url, title, snippet)
    full_text = _scope_text(url, title, snippet, str(content or "")[:2000])
    normalized_degree = str(degree_level or "").strip().lower()

    if _EXCHANGE_RE.search(short_text):
        return {
            "accepted": False,
            "reason": "audience_mismatch_exchange_student_source",
        }

    if normalized_degree == "master" and _BACHELOR_RE.search(short_text) and not _MASTER_RE.search(short_text):
        return {
            "accepted": False,
            "reason": "degree_level_mismatch_bachelor_source",
        }
    if normalized_degree == "bachelor" and _MASTER_RE.search(short_text) and not _BACHELOR_RE.search(short_text):
        return {
            "accepted": False,
            "reason": "degree_level_mismatch_master_source",
        }

    subject_tokens = _program_tokens(program)
    if subject_tokens:
        matched_tokens = subject_tokens & set(re.findall(r"[a-z0-9äöüß]+", full_text))
        if _PROGRAM_SPECIFIC_RE.search(short_text) and not matched_tokens:
            return {
                "accepted": False,
                "reason": "program_mismatch_program_specific_source",
            }

    return {
        "accepted": True,
        "reason": "scope_accepted",
    }


def discover_official_domains(rows: list[dict], *, institution: str = "", limit: int = 4) -> list[str]:
    domains: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url", row.get("link", ""))).strip()
        classification = classify_german_source(
            url,
            title=str(row.get("title", "")),
            snippet=str(row.get("snippet", "")),
            institution=institution,
        )
        if classification.get("source_tier") != TIER0_OFFICIAL:
            continue
        grouped = domain_group(str(classification.get("host", "")))
        if not grouped or grouped in seen:
            continue
        seen.add(grouped)
        domains.append(grouped)
        if len(domains) >= max(1, limit):
            break
    return domains
