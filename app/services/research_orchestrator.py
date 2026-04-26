import re
from typing import Iterable

_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


_OBJECTIVE_CATALOG: tuple[dict, ...] = (
    {
        "id": "university_overview",
        "label": "University overview",
        "subquestion": "official university and program overview details",
        "query_focus": "official university program overview",
        "coverage_keywords": ("university", "program", "department", "faculty"),
        "trigger_keywords": (),
        "min_keyword_hits": 1,
    },
    {
        "id": "admission_requirements",
        "label": "Admission requirements",
        "subquestion": "eligibility criteria and required documents",
        "query_focus": "admission requirements eligibility required documents",
        "coverage_keywords": ("admission", "requirements", "eligibility", "documents"),
        "trigger_keywords": ("admission", "eligibility", "requirement", "documents", "apply"),
        "min_keyword_hits": 1,
    },
    {
        "id": "application_deadline",
        "label": "Application deadline",
        "subquestion": "application deadline and intake timeline with exact dates",
        "query_focus": "application deadline intake timeline exact date",
        "coverage_keywords": ("deadline", "intake", "apply by", "closing date"),
        "trigger_keywords": ("deadline", "intake", "apply by", "closing", "last date"),
        "min_keyword_hits": 1,
    },
    {
        "id": "tuition_and_funding",
        "label": "Tuition and funding",
        "subquestion": "tuition fees, semester contribution, and scholarship options",
        "query_focus": "tuition fees semester contribution scholarship funding",
        "coverage_keywords": ("tuition", "fees", "scholarship", "funding"),
        "trigger_keywords": ("tuition", "fees", "cost", "scholarship", "funding"),
        "min_keyword_hits": 1,
    },
    {
        "id": "curriculum_and_modules",
        "label": "Curriculum and modules",
        "subquestion": "curriculum structure, modules, and regulations",
        "query_focus": "curriculum modules study plan module handbook regulations",
        "coverage_keywords": ("curriculum", "modules", "study plan", "module handbook"),
        "trigger_keywords": ("curriculum", "module", "syllabus", "course structure", "handbook"),
        "min_keyword_hits": 1,
    },
    {
        "id": "professors_and_supervision",
        "label": "Professors and supervision",
        "subquestion": "professor list, supervisor options, and contact points",
        "query_focus": "faculty professors supervisor advisor contact email",
        "coverage_keywords": ("professor", "faculty", "supervisor", "advisor"),
        "trigger_keywords": (
            "professor",
            "professors",
            "faculty",
            "faculties",
            "supervisor",
            "supervisors",
            "advisor",
            "advisors",
            "lecturer",
            "lecturers",
            "teacher",
        ),
        "min_keyword_hits": 1,
    },
    {
        "id": "labs_and_research",
        "label": "Labs and research groups",
        "subquestion": "research labs, groups, focus areas, and projects",
        "query_focus": "research lab group institute chair projects focus",
        "coverage_keywords": ("research", "lab", "group", "institute"),
        "trigger_keywords": (
            "research",
            "lab",
            "labs",
            "group",
            "groups",
            "institute",
            "chair",
            "projects",
        ),
        "min_keyword_hits": 1,
    },
    {
        "id": "application_portal_and_contact",
        "label": "Application portal and contact",
        "subquestion": "official portal links and admissions contact information",
        "query_focus": "application portal contact admissions office email phone",
        "coverage_keywords": ("portal", "contact", "email", "phone"),
        "trigger_keywords": ("portal", "contact", "email", "phone", "office", "where to apply"),
        "min_keyword_hits": 1,
    },
)


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _query_tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in _QUERY_TOKEN_RE.findall(_normalize_text(value).lower())
        if len(token) >= 3
    }


def _contains_keyword(query_tokens: set[str], keywords: Iterable[str]) -> bool:
    if not query_tokens:
        return False
    for keyword in keywords:
        compact = _normalize_text(str(keyword)).lower()
        if not compact:
            continue
        pieces = [item for item in compact.split() if len(item) >= 3]
        if not pieces:
            continue
        if all(piece in query_tokens for piece in pieces):
            return True
    return False


def _normalize_query_list(items: Iterable[str], *, limit: int) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        compact = _normalize_text(str(item))
        if not compact:
            continue
        key = compact.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(compact)
        if len(normalized) >= max(1, limit):
            break
    return normalized


def _objective_templates(base_query: str, focus: str) -> list[str]:
    return [
        f"{base_query} {focus} official source",
        f"{base_query} {focus} official website",
        f"{base_query} {focus} official pdf",
        f"{base_query} {focus} site:daad.de",
    ]


def build_research_plan(
    query: str,
    *,
    max_objectives: int = 8,
    max_queries: int = 18,
) -> dict:
    compact_query = _normalize_text(query)
    lower_tokens = _query_tokens(compact_query)
    selected: list[dict] = []
    seen_ids: set[str] = set()

    for objective in _OBJECTIVE_CATALOG:
        objective_id = str(objective.get("id", "")).strip()
        if not objective_id or objective_id in seen_ids:
            continue
        trigger_keywords = objective.get("trigger_keywords", ())
        if trigger_keywords and not _contains_keyword(lower_tokens, trigger_keywords):
            continue
        seen_ids.add(objective_id)
        selected.append(
            {
                "id": objective_id,
                "label": str(objective.get("label", objective_id)).strip(),
                "subquestion": _normalize_text(str(objective.get("subquestion", ""))),
                "query_focus": _normalize_text(str(objective.get("query_focus", ""))),
                "coverage_keywords": tuple(objective.get("coverage_keywords", ())),
                "min_keyword_hits": max(1, int(objective.get("min_keyword_hits", 1))),
            }
        )

    if not selected:
        fallback = _OBJECTIVE_CATALOG[0]
        selected.append(
            {
                "id": str(fallback.get("id", "university_overview")),
                "label": str(fallback.get("label", "University overview")),
                "subquestion": _normalize_text(str(fallback.get("subquestion", ""))),
                "query_focus": _normalize_text(str(fallback.get("query_focus", ""))),
                "coverage_keywords": tuple(fallback.get("coverage_keywords", ())),
                "min_keyword_hits": max(1, int(fallback.get("min_keyword_hits", 1))),
            }
        )

    selected = selected[: max(1, max_objectives)]
    subquestions = _normalize_query_list(
        [str(item.get("subquestion", "")) for item in selected],
        limit=max_objectives,
    )

    query_candidates: list[str] = []
    for objective in selected:
        focus = _normalize_text(str(objective.get("query_focus", "")))
        if not focus:
            continue
        query_candidates.extend(_objective_templates(compact_query, focus))

    queries = _normalize_query_list(query_candidates, limit=max_queries)
    return {
        "planner": "research_orchestrator",
        "objectives": selected,
        "subquestions": subquestions,
        "queries": queries,
    }


def build_queries_for_missing_objectives(
    query: str,
    *,
    missing_objectives: list[dict],
    official_domains: list[str],
    max_queries: int = 12,
) -> list[str]:
    if not missing_objectives:
        return []

    compact_query = _normalize_text(query)
    domains: list[str] = []
    seen_domains: set[str] = set()
    for domain in official_domains:
        compact = _normalize_text(str(domain)).lower()
        if not compact or compact in seen_domains:
            continue
        seen_domains.add(compact)
        domains.append(compact)

    candidates: list[str] = []
    for objective in missing_objectives:
        focus = _normalize_text(str(objective.get("query_focus", "")))
        if not focus:
            continue
        candidates.extend(_objective_templates(compact_query, focus))
        for domain in domains[:4]:
            candidates.append(f"{compact_query} {focus} site:{domain}")

    return _normalize_query_list(candidates, limit=max_queries)


def research_objective_coverage(
    objectives: list[dict],
    candidates: list[dict],
) -> dict:
    if not objectives:
        return {
            "fields": [],
            "missing_ids": [],
            "missing_labels": [],
            "missing_subquestions": [],
            "coverage": 1.0,
        }

    evidence_texts: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        metadata = candidate.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        text = _normalize_text(
            " ".join(
                [
                    str(candidate.get("content", "")),
                    str(metadata.get("title", "")),
                    str(metadata.get("url", "")),
                ]
            )
        ).lower()
        if text:
            evidence_texts.append(text)

    total = len(objectives)
    covered_count = 0
    statuses: list[dict] = []
    missing_ids: list[str] = []
    missing_labels: list[str] = []
    missing_subquestions: list[str] = []

    for objective in objectives:
        objective_id = _normalize_text(str(objective.get("id", "")))
        label = _normalize_text(str(objective.get("label", objective_id))) or objective_id
        subquestion = _normalize_text(str(objective.get("subquestion", "")))
        keywords = [
            _normalize_text(str(item)).lower()
            for item in objective.get("coverage_keywords", ())
            if _normalize_text(str(item))
        ]
        threshold = max(1, int(objective.get("min_keyword_hits", 1) or 1))

        covered = False
        if keywords and evidence_texts:
            for text in evidence_texts:
                hits = 0
                seen_hits: set[str] = set()
                for keyword in keywords:
                    if keyword in seen_hits:
                        continue
                    if keyword in text:
                        seen_hits.add(keyword)
                        hits += 1
                    if hits >= threshold:
                        covered = True
                        break
                if covered:
                    break

        statuses.append({"id": objective_id, "label": label, "covered": covered})
        if covered:
            covered_count += 1
            continue
        if objective_id:
            missing_ids.append(objective_id)
        if label:
            missing_labels.append(label)
        if subquestion:
            missing_subquestions.append(subquestion)

    coverage = 1.0 if total <= 0 else (covered_count / total)
    return {
        "fields": statuses,
        "missing_ids": missing_ids,
        "missing_labels": missing_labels,
        "missing_subquestions": missing_subquestions,
        "coverage": max(0.0, min(1.0, coverage)),
    }
