import re

_ENTITY_TOKEN_RE = re.compile(r"[A-Za-z0-9äöüÄÖÜß-]+")

_KNOWN_ALIASES = {
    "tum": "Technical University of Munich",
    "lmu": "Ludwig Maximilian University of Munich",
    "rwth": "RWTH Aachen University",
    "fau": "Friedrich-Alexander-Universität Erlangen-Nürnberg",
    "uni mannheim": "University of Mannheim",
}


def _normalize(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _extract_university_mentions(query: str) -> list[str]:
    compact = _normalize(query)
    if not compact:
        return []
    mentions: list[str] = []
    seen: set[str] = set()
    patterns = [
        re.compile(
            r"\b(?:university|universitat|universitaet|hochschule|college)\s+(?:of\s+)?"
            r"([A-Za-z0-9äöüÄÖÜß .'-]{2,80})",
            re.IGNORECASE,
        ),
        re.compile(r"\b(?:TU|TH|FH)\s+([A-Za-z0-9äöüÄÖÜß .'-]{2,80})", re.IGNORECASE),
    ]
    for pattern in patterns:
        for match in pattern.finditer(compact):
            raw = _normalize(str(match.group(0)))
            if not raw:
                continue
            key = raw.lower()
            if key in seen:
                continue
            seen.add(key)
            mentions.append(raw[:120])
    lowered = compact.lower()
    for alias, canonical in _KNOWN_ALIASES.items():
        if alias not in lowered:
            continue
        key = canonical.lower()
        if key in seen:
            continue
        seen.add(key)
        mentions.append(canonical)
    return mentions[:5]


def _extract_program_mentions(query: str) -> list[str]:
    compact = _normalize(query)
    if not compact:
        return []
    mentions: list[str] = []
    seen: set[str] = set()
    patterns = [
        re.compile(
            r"\b(?:m\.?sc|msc|master(?:'s)?(?:\s+of\s+science)?|b\.?sc|bsc|bachelor(?:'s)?)\s+"
            r"(?:in\s+)?([A-Za-z0-9&/ .'-]{2,90})",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(program|programme|course)\s+(?:in\s+)?([A-Za-z0-9&/ .'-]{2,90})",
            re.IGNORECASE,
        ),
    ]
    for pattern in patterns:
        for match in pattern.finditer(compact):
            group_value = match.group(0)
            value = _normalize(str(group_value))
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            mentions.append(value[:120])
    return mentions[:5]


def _degree_level(query: str) -> str:
    lowered = _normalize(query).lower()
    if re.search(r"\b(m\.?sc|msc|master|postgraduate)\b", lowered):
        return "master"
    if re.search(r"\b(b\.?sc|bsc|bachelor|undergraduate)\b", lowered):
        return "bachelor"
    if re.search(r"\b(phd|doctorate|doctoral)\b", lowered):
        return "phd"
    return "unknown"


def _subject_focus(query: str) -> list[str]:
    lowered = _normalize(query).lower()
    if not lowered:
        return []
    stopwords = {
        "about",
        "admission",
        "application",
        "deadline",
        "requirements",
        "university",
        "program",
        "programme",
        "course",
        "where",
        "apply",
        "students",
        "international",
        "language",
    }
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _ENTITY_TOKEN_RE.findall(lowered):
        compact = token.strip("-").lower()
        if len(compact) < 3 or compact in stopwords:
            continue
        if compact in seen:
            continue
        seen.add(compact)
        tokens.append(compact)
        if len(tokens) >= 8:
            break
    return tokens


def resolve_student_entities(query: str) -> dict:
    compact = _normalize(query)
    universities = _extract_university_mentions(compact)
    programs = _extract_program_mentions(compact)
    return {
        "university_mentions": universities,
        "program_mentions": programs,
        "degree_level": _degree_level(compact),
        "subject_focus_tokens": _subject_focus(compact),
        "scope": "germany_eu",
        "normalized_query": compact,
    }
