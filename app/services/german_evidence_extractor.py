import re
from datetime import datetime, timezone

from app.services.german_source_policy import classify_german_source
from app.services.german_source_routes import GERMAN_RESEARCH_SLOTS, slot_label

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WHITESPACE_RE = re.compile(r"\s+")
_DATE_RANGE_RE = re.compile(
    r"\b\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s*(?:[–-]|to|until)\s*\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)\b",
    flags=re.IGNORECASE,
)
_MONTH_DATE_RE = re.compile(
    r"\b\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)\b",
    flags=re.IGNORECASE,
)
_PROGRAM_STOPWORDS = {
    "bachelor",
    "bachelors",
    "business",
    "degree",
    "master",
    "masters",
    "msc",
    "program",
    "programme",
    "science",
}

_PROGRAM_OVERVIEW_RE = re.compile(
    r"\b(master|m\.sc|msc|bachelor|degree|standard period|semesters?|ects|program start|"
    r"abschluss|regelstudienzeit|studienbeginn)\b",
    flags=re.IGNORECASE,
)
_LANGUAGE_OF_INSTRUCTION_RE = re.compile(
    r"\b(language of instruction|teaching language|taught in|unterrichtssprache|lehrsprache)\b"
    r"[^.;:\n]{0,80}[:\-]?\s*(english|german|deutsch|englisch|bilingual|english and german)",
    flags=re.IGNORECASE,
)
_LANGUAGE_REQUIREMENT_RE = re.compile(
    r"\b(language requirements?|proof of language|sprachnachweis|sprachkenntnisse|"
    r"englischkenntnisse|deutschkenntnisse|testdaf|dsh|ielts|toefl|cefr|c1|b2)\b",
    flags=re.IGNORECASE,
)
_LANGUAGE_SCORE_RE = re.compile(
    r"\b(ielts|toefl(?:\s*ibt)?|cefr|cambridge|testdaf|dsh|telc)\b"
    r"[^.;\n]{0,100}\b([ABC][12]|\d{1,3}(?:[.,]\d{1,2})?)\b",
    flags=re.IGNORECASE,
)
_GERMAN_LANGUAGE_RE = re.compile(
    r"\b(german language (?:requirement|proficiency|proof|certificate)|"
    r"(?:no|not)\s+german language (?:required|requirement)|"
    r"deutschkenntnisse|testdaf|dsh|telc deutsch|goethe|"
    r"deutsche sprachpruefung|deutsche sprachprüfung)\b",
    flags=re.IGNORECASE,
)
_GPA_RE = re.compile(
    r"\b(mindestnote|minimum grade|grade threshold|abschlussnote|durchschnittsnote|"
    r"gpa|selection grade)\b[^.;\n]{0,100}\b([1-4](?:[,.]\d{1,2})?)\b",
    flags=re.IGNORECASE,
)
_ECTS_RE = re.compile(
    r"\b(\d{1,3})\s*(?:ects|credit points?|credits?|cp|leistungspunkte?)\b",
    flags=re.IGNORECASE,
)
_DEADLINE_RE = re.compile(
    r"\b(application deadline|deadline|application period|bewerbungsfrist|bewerbungszeitraum|frist|"
    r"apply by|winter semester|summer semester|wintersemester|sommersemester)\b"
    r"[^.;\n]{0,120}"
    r"(\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"(?:\s*[–-]\s*\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september|october|november|december))?)",
    flags=re.IGNORECASE,
)
_PORTAL_RE = re.compile(
    r"\b(application portal|online application|apply online|bewerbungsportal|bewerbung|uni-assist|myassist|"
    r"hochschulstart|portal2|hisinone|almaweb|campo)\b",
    flags=re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s)>\]\"']+|www\.[^\s)>\]\"']+", flags=re.IGNORECASE)
_FEE_RE = re.compile(
    r"\b(semester fee|semester contribution|tuition fees?|semesterbeitrag|studiengebuehren|studiengebühren)\b"
    r"[^.;\n]{0,100}(?:eur|euro|€)?\s*(\d{2,5}(?:[,.]\d{1,2})?)?",
    flags=re.IGNORECASE,
)
_SELECTION_RE = re.compile(
    r"\b(selection criteria|selection statute|auswahlsatzung|zulassungssatzung|"
    r"auswahlverfahren|auswahlkriterien|ranking|rangliste|selection points|auswahlpunkte)\b",
    flags=re.IGNORECASE,
)
_ENGLISH_ONLY_SIGNAL_RE = re.compile(
    r"\b(language of instruction:\s*english|language requirements?:\s*english|"
    r"proof of proficiency in english|solid knowledge of english)\b",
    flags=re.IGNORECASE,
)
_POSITIVE_GERMAN_REQUIREMENT_RE = re.compile(
    r"\b(must|have to|required|requirement|proof|proficiency|c1|testdaf|dsh|"
    r"deutschkenntnisse|german language proficiency)\b",
    flags=re.IGNORECASE,
)

_SLOT_PATTERNS: dict[str, tuple[re.Pattern, ...]] = {
    "program_overview": (_PROGRAM_OVERVIEW_RE,),
    "language_of_instruction": (_LANGUAGE_OF_INSTRUCTION_RE,),
    "language_requirements": (_LANGUAGE_REQUIREMENT_RE,),
    "language_test_score_thresholds": (_LANGUAGE_SCORE_RE,),
    "german_language_requirement": (_GERMAN_LANGUAGE_RE,),
    "gpa_or_grade_threshold": (_GPA_RE,),
    "ects_or_subject_credit_requirements": (_ECTS_RE,),
    "application_deadline": (_DEADLINE_RE,),
    "application_portal": (_PORTAL_RE,),
    "tuition_or_semester_fee": (_FEE_RE,),
    "selection_criteria": (_SELECTION_RE,),
    "competitiveness_signal": (_SELECTION_RE, _GPA_RE),
}

_SOURCE_PAGE_TYPE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (
        "ambassador_or_testimonial",
        re.compile(
            r"\b(program[-\s]?ambassadors?|student ambassador|testimonial|experience report|"
            r"i['’]m\s+[a-z]+|i have been enrolled)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ranking_or_directory",
        re.compile(r"\b(che[-\s]?ranking|department-detail|studyportals|mastersportal)\b", re.IGNORECASE),
    ),
    (
        "application_portal",
        re.compile(r"\b(portal2|hisinone|bewerbung\.uni|application portal|online application|bewerbungsportal)\b", re.IGNORECASE),
    ),
    (
        "deadline_table",
        re.compile(r"\b(application deadlines?|application period|bewerbungsfrist|bewerbungszeitraum|dates/application-deadlines)\b", re.IGNORECASE),
    ),
    (
        "selection_statute",
        re.compile(r"\b(auswahlsatzung|zulassungssatzung|selection statute|admission regulations?|mindestnote)\b", re.IGNORECASE),
    ),
    (
        "brochure_or_generic_pdf",
        re.compile(r"\b(masterbroschuere|masterbroschüre|brochure|flyer|factsheet|info sheet)\b|\.pdf(?:$|\?)", re.IGNORECASE),
    ),
    (
        "admission_criteria",
        re.compile(r"\b(admission criteria|admission requirements?|eligibility|selection criteria|zulassungsvoraussetzungen)\b", re.IGNORECASE),
    ),
    (
        "language_requirements_page",
        re.compile(r"\b(foreign language requirements?|language requirements?|sprachnachweis|ielts|toefl|cefr)\b", re.IGNORECASE),
    ),
    (
        "official_program_page",
        re.compile(r"\b(master'?s program|master program|programs/masters-program|degree: master|language of instruction|standard period)\b", re.IGNORECASE),
    ),
)

_SLOT_ALLOWED_PAGE_TYPES: dict[str, set[str]] = {
    "program_overview": {"official_program_page", "admission_criteria"},
    "language_of_instruction": {
        "official_program_page",
        "language_requirements_page",
        "admission_criteria",
        "selection_statute",
    },
    "language_requirements": {
        "language_requirements_page",
        "official_program_page",
        "admission_criteria",
        "selection_statute",
    },
    "language_test_score_thresholds": {
        "language_requirements_page",
        "admission_criteria",
        "selection_statute",
    },
    "german_language_requirement": {
        "language_requirements_page",
        "admission_criteria",
        "selection_statute",
    },
    "gpa_or_grade_threshold": {"selection_statute", "admission_criteria", "official_program_page"},
    "ects_or_subject_credit_requirements": {
        "selection_statute",
        "admission_criteria",
        "official_program_page",
    },
    "application_deadline": {
        "deadline_table",
        "admission_criteria",
        "selection_statute",
        "official_program_page",
    },
    "application_portal": {"application_portal", "admission_criteria", "official_program_page"},
    "tuition_or_semester_fee": {"official_program_page", "admission_criteria"},
    "selection_criteria": {"selection_statute", "admission_criteria", "official_program_page"},
    "competitiveness_signal": {"selection_statute", "admission_criteria", "official_program_page"},
}

_PREREQUISITE_ECTS_CONTEXT_RE = re.compile(
    r"\b(admission|admission requirements?|eligibility|requirements?|prerequisites?|"
    r"applicants?|at least|minimum|subject|informatics|computer science|business|"
    r"mathematics|statistics|programming|voraussetzungen|zulassung)\b",
    flags=re.IGNORECASE,
)
_TOTAL_ECTS_CONTEXT_RE = re.compile(
    r"\b(total|standard period|degree|curriculum|program(?:me)?\s+(?:comprises|has)|"
    r"credits:?|ects credits:?|scope)\b",
    flags=re.IGNORECASE,
)


def normalize_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


def classify_source_page_type(source: dict, *, content: str = "") -> str:
    route_text = normalize_text(
        " ".join(
            str(part or "")
            for part in (
                source.get("url", source.get("link", source.get("source_url", ""))),
                source.get("title", ""),
                source.get("snippet", ""),
            )
        )
    )
    full_text = normalize_text(
        " ".join(
            part
            for part in (
                route_text,
                str(content or source.get("content", "") or source.get("text", "")),
            )
            if part
        )
    )
    if not full_text:
        return "unknown"
    for page_type, pattern in _SOURCE_PAGE_TYPE_PATTERNS:
        if pattern.search(route_text):
            return page_type
    if re.search(r"\b(master'?s program|master program|programs/masters-program)\b", route_text, re.IGNORECASE):
        return "official_program_page"
    for page_type, pattern in _SOURCE_PAGE_TYPE_PATTERNS:
        if pattern.search(full_text):
            return page_type
    return "unknown"


def _sentences(text: str, *, limit: int = 80) -> list[str]:
    compact = normalize_text(text)
    if not compact:
        return []
    output: list[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(compact):
        sentence = normalize_text(raw)
        if len(sentence) < 12:
            continue
        output.append(sentence[:700])
        if len(output) >= limit:
            break
    return output


def _program_terms(program: str) -> list[str]:
    compact = normalize_text(program).lower()
    terms: list[str] = []
    if compact:
        terms.append(compact)
    no_degree = re.sub(
        r"\b(m\.?\s*sc|msc|master(?:'s)?(?:\s+of\s+science)?|b\.?\s*sc|bsc|bachelor(?:'s)?)\b",
        " ",
        compact,
        flags=re.IGNORECASE,
    )
    no_degree = normalize_text(no_degree)
    if no_degree and no_degree not in terms:
        terms.append(no_degree)
    tokens = [
        token
        for token in re.findall(r"[a-z0-9äöüß]+", no_degree)
        if len(token) >= 4 and token not in _PROGRAM_STOPWORDS
    ]
    if len(tokens) >= 2:
        joined = " ".join(tokens[:4])
        if joined not in terms:
            terms.append(joined)
    return terms[:4]


def _program_windows(text: str, *, program: str = "", window_chars: int = 3600) -> list[str]:
    compact = normalize_text(text)
    if not compact:
        return []
    terms = _program_terms(program)
    if not terms:
        return [compact]
    lowered = compact.lower()
    windows: list[str] = []
    seen: set[tuple[int, int]] = set()
    for term in terms:
        if not term:
            continue
        start = 0
        while True:
            index = lowered.find(term.lower(), start)
            if index < 0:
                break
            left = max(0, index - 900)
            right = min(len(compact), index + window_chars)
            key = (left, right)
            if key not in seen:
                seen.add(key)
                windows.append(compact[left:right])
            start = index + max(1, len(term))
            if len(windows) >= 8:
                return windows
    return windows or [compact]


def _source_url(source: dict) -> str:
    return normalize_text(str(source.get("url", source.get("link", source.get("source_url", "")))))


def _source_text(source: dict) -> str:
    parts = [
        str(source.get("title", "")),
        str(source.get("snippet", "")),
        str(source.get("content", "")),
        str(source.get("text", "")),
        _source_url(source),
    ]
    return normalize_text(" ".join(part for part in parts if part))


def _slot_candidates(text: str, *, slot_id: str, program: str = "") -> list[str]:
    windows = _program_windows(text, program=program)
    candidates: list[str] = []
    for window in windows:
        candidates.extend(_sentences(window, limit=180))
        for pattern in _SLOT_PATTERNS.get(str(slot_id), ()):
            for match in pattern.finditer(window):
                left = max(0, match.start() - 220)
                right = min(len(window), match.end() + 360)
                candidates.append(normalize_text(window[left:right]))
                if len(candidates) >= 260:
                    return candidates[:260]
        if slot_id == "application_deadline":
            for line in _deadline_table_like_rows(window, program=program):
                candidates.append(line)
    return candidates[:260]


def _deadline_table_like_rows(text: str, *, program: str = "") -> list[str]:
    terms = _program_terms(program)
    compact = normalize_text(text)
    if not compact or not terms:
        return []
    rows: list[str] = []
    lowered = compact.lower()
    for term in terms:
        start = 0
        while True:
            index = lowered.find(term.lower(), start)
            if index < 0:
                break
            left = max(0, index - 180)
            right = min(len(compact), index + 420)
            row = compact[left:right]
            if _DATE_RANGE_RE.search(row) or len(_MONTH_DATE_RE.findall(row)) >= 2:
                rows.append(row)
            start = index + max(1, len(term))
            if len(rows) >= 4:
                return rows
    return rows


def _value_from_match(slot_id: str, sentence: str, match: re.Match, url: str) -> str:
    if slot_id == "language_of_instruction":
        value = normalize_text(match.group(match.lastindex or 0))
        if value:
            return value
    if slot_id == "language_test_score_thresholds":
        return _language_scores_from_candidate(sentence)
    if slot_id == "gpa_or_grade_threshold":
        return normalize_text(match.group(0))
    if slot_id == "ects_or_subject_credit_requirements":
        return _ects_value_from_candidate(sentence)
    if slot_id == "application_deadline":
        ranges = _DATE_RANGE_RE.findall(sentence)
        if ranges:
            return normalize_text("; ".join(ranges))
        return normalize_text(match.group(0))
    if slot_id == "application_portal":
        for candidate in _URL_RE.findall(sentence):
            return candidate if candidate.startswith("http") else f"https://{candidate}"
        if _PORTAL_RE.search(url):
            return url
        return normalize_text(match.group(0))
    if slot_id == "tuition_or_semester_fee":
        return normalize_text(match.group(0))
    return normalize_text(sentence)


def _language_scores_from_candidate(sentence: str) -> str:
    compact = normalize_text(sentence)
    if not compact:
        return ""
    values: list[str] = []
    seen: set[str] = set()
    for match in _LANGUAGE_SCORE_RE.finditer(compact):
        raw = normalize_text(match.group(0))
        if not raw:
            continue
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        values.append(raw)
    return "; ".join(values[:6])


def _language_requirement_from_candidate(sentence: str) -> str:
    compact = normalize_text(sentence)
    if not compact:
        return ""
    score_summary = _language_scores_from_candidate(compact)
    if score_summary:
        return score_summary
    if _ENGLISH_ONLY_SIGNAL_RE.search(compact):
        return "English"
    if re.search(r"\bsolid knowledge of english\b", compact, flags=re.IGNORECASE):
        return "Solid knowledge of English"
    if re.search(r"\bproof of proficiency in english\b", compact, flags=re.IGNORECASE):
        return "Proof of proficiency in English"
    return compact[:240]


def _ects_value_from_candidate(sentence: str) -> str:
    compact = normalize_text(sentence)
    if not compact:
        return ""
    if not _PREREQUISITE_ECTS_CONTEXT_RE.search(compact):
        return ""
    if _TOTAL_ECTS_CONTEXT_RE.search(compact) and not re.search(
        r"\b(informatics|computer science|business|mathematics|statistics|programming|subject)\b",
        compact,
        flags=re.IGNORECASE,
    ):
        return ""
    matches = _ECTS_RE.findall(compact)
    if not matches:
        return ""
    numeric_values = [int(value) for value in matches if str(value).isdigit()]
    if numeric_values and max(numeric_values) <= 12 and not re.search(
        r"\b(admission|eligibility|applicants?|at least|minimum|prerequisites?|zulassung)\b",
        compact,
        flags=re.IGNORECASE,
    ):
        return ""
    values: list[str] = []
    seen: set[str] = set()
    for value in matches:
        normalized = f"{value} ECTS"
        if normalized in seen:
            continue
        seen.add(normalized)
        values.append(normalized)
    if len(values) >= 2:
        return normalize_text("; ".join(values[:6]))
    return values[0]


def _deadline_value_from_candidate(sentence: str) -> str:
    return _deadline_value_from_candidate_for_program(sentence, program="")


def _deadline_value_from_candidate_for_program(sentence: str, *, program: str = "") -> str:
    compact = normalize_text(sentence)
    terms = _program_terms(program)
    if compact and terms:
        lowered = compact.lower()
        indexes = [
            lowered.find(term.lower())
            for term in terms
            if term and lowered.find(term.lower()) >= 0
        ]
        if indexes:
            compact = compact[min(indexes):]
            program_ranges = _DATE_RANGE_RE.findall(compact)
            if program_ranges:
                return normalize_text("; ".join(program_ranges[:2]))
            program_dates = _MONTH_DATE_RE.findall(compact)
            if len(program_dates) >= 2:
                return normalize_text("; ".join(program_dates[:4]))
    ranges = _DATE_RANGE_RE.findall(sentence)
    if ranges:
        return normalize_text("; ".join(ranges))
    dates = _MONTH_DATE_RE.findall(sentence)
    if len(dates) >= 2:
        return normalize_text("; ".join(dates[:4]))
    return ""


def _program_context_matches(sentence: str, *, program: str) -> bool:
    terms = _program_terms(program)
    if not terms:
        return True
    lowered = normalize_text(sentence).lower()
    return any(term and term.lower() in lowered for term in terms)


def _is_contaminating_german_requirement(
    *,
    sentence: str,
    source_page_type: str,
    program: str,
) -> bool:
    compact = normalize_text(sentence)
    if not compact:
        return False
    if re.search(r"\bgerman language and literature\b", compact, flags=re.IGNORECASE):
        return True
    if _ENGLISH_ONLY_SIGNAL_RE.search(compact) and not _POSITIVE_GERMAN_REQUIREMENT_RE.search(compact):
        return True
    if program and not _program_context_matches(compact, program=program):
        # Generic multi-program pages often contain German requirements for neighboring programs.
        return source_page_type in {"language_requirements_page", "admission_criteria", "deadline_table"}
    return False


def _slot_rejection_reason(
    *,
    slot_id: str,
    source_page_type: str,
    value: str,
    sentence: str,
    program: str = "",
) -> str:
    if source_page_type == "ranking_or_directory":
        return "source_page_type_ranking_or_directory"
    if source_page_type == "ambassador_or_testimonial":
        return "source_page_type_ambassador_or_testimonial"
    allowed_types = _SLOT_ALLOWED_PAGE_TYPES.get(slot_id)
    if allowed_types and source_page_type not in allowed_types and source_page_type != "unknown":
        return f"source_page_type_mismatch:{source_page_type}"
    if slot_id == "ects_or_subject_credit_requirements" and not _ects_value_from_candidate(sentence):
        return "ects_not_prerequisite_context"
    if (
        slot_id == "application_deadline"
        and program
        and source_page_type in {"selection_statute", "deadline_table"}
        and not _program_context_matches(sentence, program=program)
    ):
        return "deadline_not_in_program_context"
    if slot_id == "german_language_requirement" and re.search(
        r"\bgerman language and literature\b",
        value + " " + sentence,
        flags=re.IGNORECASE,
    ):
        return "discipline_list_not_language_requirement"
    if slot_id == "german_language_requirement" and _is_contaminating_german_requirement(
        sentence=sentence,
        source_page_type=source_page_type,
        program=program,
    ):
        return "german_requirement_not_in_program_context"
    return ""


def _slot_score(
    slot_id: str,
    sentence: str,
    url: str,
    source_tier: str,
    *,
    source_page_type: str = "",
) -> float:
    score = 0.35
    if source_tier == "tier0_official":
        score += 0.35
    elif source_tier == "tier1_corroboration":
        score += 0.2
    lowered = f"{sentence} {url}".lower()
    if source_page_type == "official_program_page":
        if slot_id in {
            "program_overview",
            "language_of_instruction",
            "language_requirements",
            "ects_or_subject_credit_requirements",
            "application_deadline",
            "application_portal",
            "selection_criteria",
            "competitiveness_signal",
        }:
            score += 0.18
    if source_page_type == "admission_criteria":
        if slot_id in {
            "language_requirements",
            "language_test_score_thresholds",
            "ects_or_subject_credit_requirements",
            "gpa_or_grade_threshold",
            "selection_criteria",
            "competitiveness_signal",
        }:
            score += 0.2
    if source_page_type == "language_requirements_page" and slot_id in {
        "language_requirements",
        "language_test_score_thresholds",
        "german_language_requirement",
    }:
        score += 0.2
    if source_page_type == "deadline_table" and slot_id == "application_deadline":
        score += 0.2
    if source_page_type == "selection_statute":
        if slot_id in {"gpa_or_grade_threshold", "selection_criteria", "competitiveness_signal"}:
            score += 0.12
        elif slot_id in {"ects_or_subject_credit_requirements", "application_deadline"}:
            score -= 0.08
    if slot_id == "application_deadline" and any(
        marker in lowered for marker in ("bewerbungsfrist", "deadline", "fristen")
    ):
        score += 0.15
    if slot_id == "application_portal" and any(
        marker in lowered for marker in ("portal", "apply", "bewerbung", "uni-assist")
    ):
        score += 0.15
    if re.search(r"\d", sentence):
        score += 0.08
    if slot_id == "ects_or_subject_credit_requirements":
        value_count = len(_ECTS_RE.findall(sentence))
        if value_count >= 3:
            score += 0.12
    return round(max(0.0, min(1.0, score)), 4)


def extract_german_evidence_rows(
    sources: list[dict],
    *,
    required_slots: tuple[str, ...] | list[str] = GERMAN_RESEARCH_SLOTS,
    institution: str = "",
    program: str = "",
) -> list[dict]:
    best_by_slot: dict[str, dict] = {}
    now = datetime.now(timezone.utc).isoformat()
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = _source_url(source)
        title = normalize_text(str(source.get("title", "")))
        snippet = normalize_text(str(source.get("snippet", "")))
        classification = classify_german_source(
            url,
            title=title,
            snippet=snippet,
            institution=institution,
        )
        source_tier = str(classification.get("source_tier", "tier2_discovery"))
        source_type = str(classification.get("source_type", "discovery"))
        text = _source_text(source)
        if not text:
            continue
        source_page_type = classify_source_page_type(source, content=text)
        for slot_id in required_slots:
            patterns = _SLOT_PATTERNS.get(str(slot_id), ())
            for sentence in _slot_candidates(text, slot_id=str(slot_id), program=program):
                if str(slot_id) == "application_deadline":
                    value = _deadline_value_from_candidate_for_program(sentence, program=program)
                    rejection_reason = _slot_rejection_reason(
                        slot_id=str(slot_id),
                        source_page_type=source_page_type,
                        value=value,
                        sentence=sentence,
                        program=program,
                    )
                    if rejection_reason:
                        continue
                    if value:
                        confidence = _slot_score(
                            str(slot_id),
                            sentence,
                            url,
                            source_tier,
                            source_page_type=source_page_type,
                        )
                        row = {
                            "field": str(slot_id),
                            "id": str(slot_id),
                            "label": slot_label(str(slot_id)),
                            "status": "found",
                            "value": value,
                            "source_url": url,
                            "source_type": source_type,
                            "source_tier": source_tier,
                            "source_page_type": source_page_type,
                            "evidence_snippet": sentence[:420],
                            "evidence_text": sentence[:420],
                            "confidence": confidence,
                            "retrieved_at": now,
                        }
                        existing = best_by_slot.get(str(slot_id))
                        if existing is None or confidence > float(existing.get("confidence", 0.0) or 0.0):
                            best_by_slot[str(slot_id)] = row
                for pattern in patterns:
                    match = pattern.search(sentence)
                    if not match:
                        continue
                    if str(slot_id) == "language_requirements":
                        value = _language_requirement_from_candidate(sentence)
                    else:
                        value = _value_from_match(str(slot_id), sentence, match, url)
                    if not value:
                        continue
                    rejection_reason = _slot_rejection_reason(
                        slot_id=str(slot_id),
                        source_page_type=source_page_type,
                        value=value,
                        sentence=sentence,
                        program=program,
                    )
                    if rejection_reason:
                        continue
                    confidence = _slot_score(
                        str(slot_id),
                        sentence,
                        url,
                        source_tier,
                        source_page_type=source_page_type,
                    )
                    row = {
                        "field": str(slot_id),
                        "id": str(slot_id),
                        "label": slot_label(str(slot_id)),
                        "status": "found",
                        "value": value,
                        "source_url": url,
                        "source_type": source_type,
                        "source_tier": source_tier,
                        "source_page_type": source_page_type,
                        "evidence_snippet": sentence[:420],
                        "evidence_text": sentence[:420],
                        "confidence": confidence,
                        "retrieved_at": now,
                    }
                    existing = best_by_slot.get(str(slot_id))
                    if existing is None or confidence > float(existing.get("confidence", 0.0) or 0.0):
                        best_by_slot[str(slot_id)] = row
    rows: list[dict] = []
    for slot_id in required_slots:
        key = str(slot_id)
        if key in best_by_slot:
            rows.append(best_by_slot[key])
        else:
            rows.append(
                {
                    "field": key,
                    "id": key,
                    "label": slot_label(key),
                    "status": "missing",
                    "value": "Not verified from official sources.",
                    "source_url": "",
                    "source_type": "official",
                    "source_tier": "tier0_official",
                    "evidence_snippet": "",
                    "evidence_text": "",
                    "confidence": 0.0,
                    "retrieved_at": now,
                }
            )
    return rows


def unresolved_slots(evidence_rows: list[dict]) -> list[str]:
    output: list[str] = []
    for row in evidence_rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status", "")).strip().lower() == "found":
            continue
        slot_id = normalize_text(str(row.get("id", row.get("field", ""))))
        if slot_id:
            output.append(slot_id)
    return output


def coverage_score(evidence_rows: list[dict]) -> float:
    if not evidence_rows:
        return 1.0
    found = sum(
        1
        for row in evidence_rows
        if isinstance(row, dict) and str(row.get("status", "")).strip().lower() == "found"
    )
    return round(found / max(1, len(evidence_rows)), 4)
