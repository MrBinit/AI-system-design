import re
from dataclasses import dataclass, field

_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9äöüÄÖÜß&/-]+")

GERMAN_RESEARCH_SLOTS: tuple[str, ...] = (
    "program_overview",
    "language_of_instruction",
    "language_requirements",
    "language_test_score_thresholds",
    "german_language_requirement",
    "gpa_or_grade_threshold",
    "ects_or_subject_credit_requirements",
    "application_deadline",
    "application_portal",
    "tuition_or_semester_fee",
    "selection_criteria",
    "competitiveness_signal",
)

_SLOT_LABELS = {
    "program_overview": "Program overview",
    "language_of_instruction": "Language of instruction",
    "language_requirements": "Language requirements",
    "language_test_score_thresholds": "Language test score thresholds",
    "german_language_requirement": "German language requirement",
    "gpa_or_grade_threshold": "GPA/grade threshold",
    "ects_or_subject_credit_requirements": "ECTS/subject credit requirements",
    "application_deadline": "Application deadline",
    "application_portal": "Application portal",
    "tuition_or_semester_fee": "Tuition or semester fee",
    "selection_criteria": "Selection criteria",
    "competitiveness_signal": "Competitiveness signal",
}

_SLOT_ROUTE_TERMS = {
    "program_overview": (
        "official program page",
        "Master program facts",
        "Studiengang Master",
    ),
    "language_of_instruction": (
        "language of instruction",
        "teaching language",
        "Unterrichtssprache",
        "Lehrsprache",
    ),
    "language_requirements": (
        "language requirements",
        "Sprachnachweis",
        "Sprachkenntnisse",
        "Englischkenntnisse",
        "Deutschkenntnisse",
    ),
    "language_test_score_thresholds": (
        "IELTS TOEFL CEFR minimum score",
        "TOEFL IELTS Mindestpunktzahl",
        "C1 B2 Sprachnachweis",
    ),
    "german_language_requirement": (
        "German language requirement",
        "Deutschkenntnisse TestDaF DSH",
    ),
    "gpa_or_grade_threshold": (
        "Mindestnote",
        "minimum grade",
        "Durchschnittsnote",
        "selection statute grade",
    ),
    "ects_or_subject_credit_requirements": (
        "ECTS Leistungspunkte fachliche Voraussetzungen",
        "required credits prerequisite modules",
        "subject credit requirements",
    ),
    "application_deadline": (
        "Bewerbungsfrist",
        "Bewerbungszeitraum",
        "application deadline",
        "Wintersemester Sommersemester deadline",
    ),
    "application_portal": (
        "Bewerbungsportal",
        "online application",
        "apply online",
        "application portal",
        "uni-assist",
    ),
    "tuition_or_semester_fee": (
        "semester fee tuition fees",
        "Semesterbeitrag Studiengebuehren",
    ),
    "selection_criteria": (
        "Auswahlsatzung",
        "Zulassungssatzung",
        "selection criteria admission",
        "Auswahlverfahren",
    ),
    "competitiveness_signal": (
        "selection criteria admission score ranking",
        "Auswahlpunkte Auswahlverfahren Mindestnote",
    ),
}

_PDF_ROUTE_TERMS = (
    "Auswahlsatzung filetype:pdf",
    "Zulassungssatzung filetype:pdf",
    "Pruefungsordnung filetype:pdf",
    "Modulhandbuch filetype:pdf",
)

_SLOT_ROUTE_PRIORITY = {
    "application_deadline": 10,
    "application_portal": 9,
    "gpa_or_grade_threshold": 8,
    "ects_or_subject_credit_requirements": 8,
    "language_test_score_thresholds": 7,
    "language_requirements": 7,
    "language_of_instruction": 6,
    "selection_criteria": 6,
    "german_language_requirement": 5,
    "program_overview": 4,
    "tuition_or_semester_fee": 3,
    "competitiveness_signal": 2,
}

_STOPWORDS = {
    "about",
    "also",
    "and",
    "application",
    "apply",
    "course",
    "deadline",
    "degree",
    "for",
    "from",
    "german",
    "germany",
    "gpa",
    "ielts",
    "international",
    "language",
    "master",
    "masters",
    "msc",
    "of",
    "portal",
    "program",
    "programme",
    "requirements",
    "safe",
    "student",
    "tell",
    "the",
    "toefl",
    "university",
    "where",
    "with",
}


@dataclass(frozen=True)
class GermanResearchTask:
    query: str
    institution: str = ""
    program: str = ""
    degree_level: str = ""
    subject_terms: tuple[str, ...] = field(default_factory=tuple)
    required_slots: tuple[str, ...] = field(default_factory=tuple)
    country: str = "Germany"


def normalize_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _tokens(value: str) -> list[str]:
    return [token.lower() for token in _QUERY_TOKEN_RE.findall(normalize_text(value))]


def _dedupe(values: list[str], *, limit: int) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        compact = normalize_text(value)
        if not compact:
            continue
        key = compact.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(compact)
        if len(output) >= max(1, limit):
            break
    return output


def is_likely_german_university_query(query: str) -> bool:
    text = normalize_text(query).lower()
    if not text:
        return False
    has_university = bool(
        re.search(r"\b(university|universitaet|universitat|hochschule|tu |fh |rwth|tum|lmu)\b", text)
    )
    has_student_topic = bool(
        re.search(
            r"\b(master|msc|m\.sc|program|programme|admission|deadline|bewerbung|"
            r"bewerbungsfrist|gpa|ects|ielts|toefl|sprachnachweis|portal|daad)\b",
            text,
        )
    )
    country_hint = bool(re.search(r"\b(germany|german|deutschland|daad|uni-assist)\b", text))
    return has_university and (has_student_topic or country_hint)


def _extract_institution(query: str) -> str:
    compact = normalize_text(query)
    patterns = [
        r"\b(University of [A-Za-zÄÖÜäöüß .'-]{2,80})",
        r"\b(Technical University of [A-Za-zÄÖÜäöüß .'-]{2,80})",
        r"\b([A-ZÄÖÜ][A-Za-zÄÖÜäöüß .'-]{2,80} University)",
        r"\b(TU|TH|FH|HS)\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß .'-]{2,80})",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact)
        if not match:
            continue
        value = normalize_text(match.group(0))
        value = re.split(
            r"\s+(?:MSc|M\.Sc|Master|Bachelor|BSc|PhD|deadline|admission|language|GPA|ECTS)\b",
            value,
            flags=re.IGNORECASE,
        )[0]
        return normalize_text(value)
    aliases = {
        "tum": "Technical University of Munich",
        "lmu": "Ludwig Maximilian University of Munich",
        "rwth": "RWTH Aachen University",
    }
    lowered = compact.lower()
    for alias, canonical in aliases.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return canonical
    return ""


def _extract_degree_level(query: str) -> str:
    lowered = normalize_text(query).lower()
    if re.search(r"\b(m\.?sc|msc|master|masters)\b", lowered):
        return "master"
    if re.search(r"\b(b\.?sc|bsc|bachelor|bachelors)\b", lowered):
        return "bachelor"
    if re.search(r"\b(phd|doctoral|doctorate)\b", lowered):
        return "phd"
    return ""


def _extract_program(query: str, institution: str) -> str:
    compact = normalize_text(query)
    degree_re = r"(?:M\.?Sc|MSc|Master(?:'s)?(?:\s+of\s+Science)?|B\.?Sc|BSc|Bachelor(?:'s)?)"
    match = re.search(
        rf"\b({degree_re}\s+(?:in\s+)?[A-Za-z0-9&/ .'-]{{2,90}})",
        compact,
        flags=re.IGNORECASE,
    )
    if match:
        value = normalize_text(match.group(1))
        value = re.split(
            r"\s*[:;\n-]\s*|\s+(?:language|ielts|toefl|gpa|ects|deadline|where|portal)\b",
            value,
            flags=re.IGNORECASE,
        )[0]
        return normalize_text(value)
    if institution:
        remainder = normalize_text(compact.replace(institution, " "))
    else:
        remainder = compact
    tokens = [
        token
        for token in _tokens(remainder)
        if len(token) >= 3 and token not in _STOPWORDS
    ][:8]
    return normalize_text(" ".join(tokens))


def _required_slots_from_query(query: str) -> tuple[str, ...]:
    text = normalize_text(query).lower()
    slots: list[str] = ["program_overview"]
    if re.search(r"\b(language of instruction|teaching language|taught in|unterrichtssprache)\b", text):
        slots.append("language_of_instruction")
    if re.search(r"\b(language|ielts|toefl|cefr|testdaf|dsh|sprachnachweis|german)\b", text):
        slots.extend(
            [
                "language_of_instruction",
                "language_requirements",
                "language_test_score_thresholds",
                "german_language_requirement",
            ]
        )
    if re.search(r"\b(gpa|grade|mindestnote|ects|credit|credits|prerequisite)\b", text):
        slots.extend(["gpa_or_grade_threshold", "ects_or_subject_credit_requirements"])
    if re.search(r"\b(deadline|bewerbungsfrist|application period|last date|fristen)\b", text):
        slots.append("application_deadline")
    if re.search(r"\b(portal|apply online|where to apply|bewerbungsportal|uni-assist)\b", text):
        slots.append("application_portal")
    if re.search(r"\b(fee|fees|tuition|semester contribution|semesterbeitrag)\b", text):
        slots.append("tuition_or_semester_fee")
    if re.search(r"\b(competitive|safe|chance|chances|selection|auswahlsatzung)\b", text):
        slots.extend(["selection_criteria", "competitiveness_signal"])
    if len(slots) == 1 and re.search(r"\b(admission|requirements|eligibility|zulassung)\b", text):
        slots.extend(
            [
                "language_requirements",
                "gpa_or_grade_threshold",
                "ects_or_subject_credit_requirements",
                "application_deadline",
                "application_portal",
            ]
        )
    return tuple(_dedupe(slots, limit=len(GERMAN_RESEARCH_SLOTS)))


def resolve_german_research_task(query: str) -> GermanResearchTask:
    institution = _extract_institution(query)
    program = _extract_program(query, institution)
    degree_level = _extract_degree_level(query)
    subject_terms = tuple(
        token
        for token in _tokens(program)
        if token not in _STOPWORDS and token not in {"m", "sc"}
    )
    return GermanResearchTask(
        query=normalize_text(query),
        institution=institution,
        program=program,
        degree_level=degree_level,
        subject_terms=subject_terms,
        required_slots=_required_slots_from_query(query),
    )


def slot_label(slot_id: str) -> str:
    return _SLOT_LABELS.get(str(slot_id), str(slot_id).replace("_", " ").title())


def build_discovery_queries(task: GermanResearchTask, *, max_queries: int = 6) -> list[str]:
    base = normalize_text(" ".join(item for item in (task.institution, task.program) if item))
    if not base:
        base = task.query
    degree = task.degree_level or "master"
    candidates = [
        f"{base} official {degree} program page Germany",
        f"{base} admission requirements official university",
        f"{base} DAAD",
        f"{base} site:daad.de",
        f"{base} Auswahlsatzung",
        f"{base} Bewerbungsfrist",
    ]
    return _dedupe(candidates, limit=max_queries)


def build_slot_route_queries(
    task: GermanResearchTask,
    *,
    official_domains: list[str] | tuple[str, ...] = (),
    missing_slots: list[str] | tuple[str, ...] | None = None,
    max_queries: int = 24,
) -> list[str]:
    base = normalize_text(" ".join(item for item in (task.institution, task.program) if item))
    if not base:
        base = task.query
    slots = tuple(
        sorted(
            tuple(missing_slots or task.required_slots or GERMAN_RESEARCH_SLOTS),
            key=lambda slot: (-_SLOT_ROUTE_PRIORITY.get(str(slot), 0), str(slot)),
        )
    )
    domains = [normalize_text(domain).lower() for domain in official_domains if normalize_text(domain)]
    candidates: list[str] = []
    domain_scopes = [f"site:{domain}" for domain in domains[:3]]

    for domain_scope in domain_scopes or [""]:
        suffix = f" {domain_scope}" if domain_scope else ""
        candidates.append(f"{base} official program page{suffix}")
        for pdf_term in _PDF_ROUTE_TERMS:
            candidates.append(f"{base} {pdf_term}{suffix}")

    for slot in slots:
        for term in _SLOT_ROUTE_TERMS.get(slot, (slot.replace("_", " "),)):
            if domain_scopes:
                for domain_scope in domain_scopes[:2]:
                    candidates.append(f"{base} {term} {domain_scope}")
            else:
                candidates.append(f"{base} {term} official source")

    return _dedupe(candidates, limit=max_queries)


def research_plan_for_task(task: GermanResearchTask, *, official_domains: list[str] | None = None) -> dict:
    domains = official_domains or []
    return {
        "planner": "germany_researcher",
        "country": "Germany",
        "institution": task.institution,
        "program": task.program,
        "degree_level": task.degree_level,
        "required_slots": list(task.required_slots),
        "official_domains": domains,
        "discovery_queries": build_discovery_queries(task),
        "route_queries": build_slot_route_queries(task, official_domains=domains, max_queries=18),
    }
