import re
from datetime import datetime, timezone

from app.services.german_source_policy import classify_german_source
from app.services.german_source_routes import GERMAN_RESEARCH_SLOTS, slot_label

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WHITESPACE_RE = re.compile(r"\s+")

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
    r"\b(german language|deutschkenntnisse|testdaf|dsh|telc deutsch|goethe|"
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
    r"\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september|october|november|december))",
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


def normalize_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


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


def _value_from_match(slot_id: str, sentence: str, match: re.Match, url: str) -> str:
    if slot_id == "language_of_instruction":
        value = normalize_text(match.group(match.lastindex or 0))
        if value:
            return value
    if slot_id == "language_test_score_thresholds":
        return normalize_text(match.group(0))
    if slot_id == "gpa_or_grade_threshold":
        return normalize_text(match.group(0))
    if slot_id == "ects_or_subject_credit_requirements":
        return normalize_text(match.group(0))
    if slot_id == "application_deadline":
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


def _slot_score(slot_id: str, sentence: str, url: str, source_tier: str) -> float:
    score = 0.35
    if source_tier == "tier0_official":
        score += 0.35
    elif source_tier == "tier1_corroboration":
        score += 0.2
    lowered = f"{sentence} {url}".lower()
    if slot_id in {"gpa_or_grade_threshold", "ects_or_subject_credit_requirements", "selection_criteria"}:
        if any(marker in lowered for marker in ("auswahlsatzung", "zulassungssatzung", ".pdf")):
            score += 0.2
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
    return round(max(0.0, min(1.0, score)), 4)


def extract_german_evidence_rows(
    sources: list[dict],
    *,
    required_slots: tuple[str, ...] | list[str] = GERMAN_RESEARCH_SLOTS,
    institution: str = "",
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
        for sentence in _sentences(text):
            for slot_id in required_slots:
                patterns = _SLOT_PATTERNS.get(str(slot_id), ())
                for pattern in patterns:
                    match = pattern.search(sentence)
                    if not match:
                        continue
                    value = _value_from_match(str(slot_id), sentence, match, url)
                    if not value:
                        continue
                    confidence = _slot_score(str(slot_id), sentence, url, source_tier)
                    row = {
                        "field": str(slot_id),
                        "id": str(slot_id),
                        "label": slot_label(str(slot_id)),
                        "status": "found",
                        "value": value,
                        "source_url": url,
                        "source_type": source_type,
                        "source_tier": source_tier,
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

