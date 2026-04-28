"""
UniGraph Phase 1: fast official-source university research.

This module deliberately avoids deep research, vector storage, persistent cache,
multi-agent orchestration, and verification loops. It plans once, fans out to a
small number of targeted searches, extracts bounded evidence from official pages
and PDFs, then answers only from selected evidence chunks.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import pdfplumber
except Exception:  # pragma: no cover - optional dependency in minimal test envs
    pdfplumber = None

from app.core.config import get_settings
from app.services.tavily_search_service import aextract_urls, asearch_google

settings = get_settings()
logger = logging.getLogger(__name__)

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
}
LOW_QUALITY_DOMAINS = (
    "reddit.",
    "quora.",
    "facebook.",
    "instagram.",
    "youtube.",
    "medium.com",
    "blogspot.",
)
GERMAN_UNIVERSITY_FOCUS = True
AMBIGUOUS_GERMAN_UNIVERSITIES = {
    "fau": {
        "preferred_name": "Friedrich-Alexander-Universität Erlangen-Nürnberg",
        "preferred_domain": "fau.de",
        "secondary_name": "Florida Atlantic University",
        "secondary_domains": ("fau.edu",),
    }
}
LANGUAGE_TERMS = (
    "ielts",
    "toefl",
    "duolingo",
    "english",
    "language",
    "proficiency",
    "b2",
    "c1",
    "cefr",
    "sprachnachweis",
    "sprachkenntnisse",
    "englisch",
)
LANGUAGE_UNRELATED_TERMS = (
    "tuition",
    "fee",
    "semesterbeitrag",
    "studiengebühren",
    "gpa",
    "grade",
    "duration",
    "semester",
    "transcript",
    "document",
    "documents",
    "gre",
    "curriculum",
    "module",
)
OFFICIAL_KEYWORDS = (
    "admission",
    "admissions",
    "application",
    "apply",
    "deadline",
    "requirements",
    "bewerbung",
    "bewerbungsfrist",
    "zulassung",
    "zulassungsvoraussetzungen",
    "sprachnachweis",
    "unterlagen",
    "modulhandbuch",
    "pruefungsordnung",
    "prüfungsordnung",
    "semesterbeitrag",
    "studiengebuehren",
    "studiengebühren",
)
GERMAN_SEARCH_TERMS = [
    "Zulassungsvoraussetzungen",
    "Bewerbungsfrist",
    "Sprachnachweis",
    "erforderliche Unterlagen",
    "Pruefungsordnung",
    "Prüfungsordnung",
    "Modulhandbuch",
    "Semesterbeitrag",
    "Studiengebühren",
]

FIELD_KEYWORDS: dict[str, list[str]] = {
    "english_language_requirement": [
        "english",
        "language",
        "language proof",
        "proficiency",
        "cefr",
        "b2",
        "c1",
        "sprachnachweis",
        "sprachkenntnisse",
        "englisch",
    ],
    "ielts_score": ["ielts", "band", "overall", "minimum score"],
    "toefl_score": ["toefl", "internet-based", "ibt"],
    "duolingo_score": ["duolingo", "det"],
    "german_language_requirement": ["german language", "deutsch", "dsh", "testdaf"],
    "application_deadline": [
        "deadline",
        "application period",
        "apply by",
        "bewerbungsfrist",
        "bewerbungszeitraum",
        "winter semester",
        "summer semester",
    ],
    "intake_or_semester": ["intake", "winter semester", "summer semester", "semester"],
    "applicant_category": ["international", "eu", "non-eu", "applicant", "bewerber"],
    "academic_eligibility": [
        "eligibility",
        "admission requirement",
        "academic requirement",
        "zulassungsvoraussetzungen",
        "qualification",
    ],
    "gpa_requirement": ["gpa", "grade", "minimum grade", "final grade", "average grade", "note"],
    "required_degree_background": [
        "bachelor",
        "degree",
        "subject",
        "background",
        "credits",
        "ects",
    ],
    "admission_restrictions": ["restricted admission", "selection", "aptitude", "nc"],
    "required_application_documents": [
        "documents",
        "checklist",
        "required documents",
        "application documents",
        "unterlagen",
        "certificate",
        "transcript",
    ],
    "international_applicant_documents": ["international", "visa", "passport", "foreign"],
    "language_proof": ["language proof", "ielts", "toefl", "duolingo", "sprachnachweis"],
    "degree_transcript_requirements": ["degree certificate", "transcript", "diploma"],
    "aps_requirement": ["aps", "academic evaluation centre", "akademische prüfstelle"],
    "vpd_requirement": ["vpd", "preliminary review documentation", "vorprüfungsdokumentation"],
    "uni_assist_requirement": ["uni-assist", "uni assist"],
    "tuition_fee": ["tuition", "fees", "studiengebühren", "tuition fee"],
    "semester_contribution": ["semester contribution", "semesterbeitrag", "student services fee"],
    "gre_gmat_requirement": ["gre", "gmat"],
    "teaching_language": [
        "language of instruction",
        "teaching language",
        "english-taught",
        "german-taught",
    ],
    "program_duration": ["duration", "standard period", "semesters", "regelstudienzeit"],
    "curriculum_modules": ["curriculum", "module", "module handbook", "modulhandbuch", "courses"],
    "scholarship_funding": ["scholarship", "funding", "financial aid", "daad scholarship"],
    "application_process": [
        "how to apply",
        "application portal",
        "apply online",
        "application process",
    ],
    "general_information": ["program", "degree", "study", "university"],
}

INTENT_PROFILES: dict[str, dict[str, list[str]]] = {
    "language_requirement_lookup": {
        "required": ["english_language_requirement"],
        "optional": ["ielts_score", "toefl_score", "duolingo_score", "german_language_requirement"],
        "excluded": [
            "tuition_fee",
            "semester_contribution",
            "gpa_requirement",
            "required_application_documents",
            "application_deadline",
            "program_duration",
            "curriculum_modules",
            "gre_gmat_requirement",
        ],
    },
    "deadline_lookup": {
        "required": ["application_deadline", "intake_or_semester"],
        "optional": ["applicant_category", "application_process"],
        "excluded": [
            "english_language_requirement",
            "ielts_score",
            "gpa_requirement",
            "tuition_fee",
            "required_application_documents",
        ],
    },
    "eligibility_check": {
        "required": [
            "english_language_requirement",
            "academic_eligibility",
            "gpa_requirement",
            "required_degree_background",
        ],
        "optional": ["admission_restrictions", "application_deadline", "ielts_score"],
        "excluded": ["tuition_fee", "curriculum_modules", "scholarship_funding"],
    },
    "document_requirement_lookup": {
        "required": [
            "required_application_documents",
            "international_applicant_documents",
            "language_proof",
            "degree_transcript_requirements",
        ],
        "optional": ["aps_requirement", "vpd_requirement", "uni_assist_requirement"],
        "excluded": ["tuition_fee", "curriculum_modules", "scholarship_funding"],
    },
    "tuition_fee_lookup": {
        "required": ["tuition_fee"],
        "optional": ["semester_contribution", "applicant_category"],
        "excluded": ["ielts_score", "gpa_requirement", "curriculum_modules"],
    },
    "admission_requirement_lookup": {
        "required": ["academic_eligibility", "required_degree_background"],
        "optional": ["gpa_requirement", "english_language_requirement", "admission_restrictions"],
        "excluded": ["tuition_fee", "curriculum_modules", "scholarship_funding"],
    },
    "program_overview_lookup": {
        "required": ["teaching_language", "program_duration"],
        "optional": ["tuition_fee", "application_deadline"],
        "excluded": ["required_application_documents"],
    },
    "curriculum_lookup": {
        "required": ["curriculum_modules"],
        "optional": ["program_duration", "teaching_language"],
        "excluded": ["tuition_fee", "application_deadline", "ielts_score"],
    },
    "application_process_lookup": {
        "required": ["application_process"],
        "optional": [
            "application_deadline",
            "uni_assist_requirement",
            "aps_requirement",
            "vpd_requirement",
        ],
        "excluded": ["tuition_fee", "curriculum_modules"],
    },
    "scholarship_funding_lookup": {
        "required": ["scholarship_funding"],
        "optional": ["tuition_fee"],
        "excluded": ["ielts_score", "gpa_requirement", "curriculum_modules"],
    },
    "general_university_question": {
        "required": ["general_information"],
        "optional": [],
        "excluded": [],
    },
}


def _cfg_int(name: str, default: int, *, minimum: int = 1, maximum: int = 100) -> int:
    value = getattr(settings.web_search, name, default)
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


MAX_QUERIES = _cfg_int("phase1_max_queries", 5, maximum=8)
MAX_RESULTS_PER_QUERY = _cfg_int("phase1_max_results_per_query", 3, maximum=8)
MAX_TOTAL_URLS_TO_FETCH = _cfg_int("phase1_max_total_urls_to_fetch", 8, maximum=16)
MAX_PDFS_TO_READ = _cfg_int("phase1_max_pdfs_to_read", 3, maximum=6)
MAX_PDF_SIZE_MB = _cfg_int("phase1_max_pdf_size_mb", 15, maximum=50)
MAX_PDF_PAGES = _cfg_int("phase1_max_pdf_pages", 40, maximum=100)
MAX_EVIDENCE_CHUNKS = _cfg_int("phase1_max_evidence_chunks", 12, maximum=30)
CHUNK_CHARS = _cfg_int("page_chunk_chars", 850, minimum=250, maximum=4000)
CHUNK_OVERLAP = _cfg_int("page_chunk_overlap_chars", 120, minimum=0, maximum=1000)


@dataclass
class QueryPlan:
    university: str = ""
    university_short: str = ""
    program: str = ""
    country: str = ""
    degree_level: str = ""
    user_intent: str = ""
    intent: str = "general_university_question"
    required_info: list[str] = field(default_factory=list)
    required_fields: list[str] = field(default_factory=list)
    optional_fields: list[str] = field(default_factory=list)
    excluded_fields: list[str] = field(default_factory=list)
    user_profile_details: dict[str, Any] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    german_keywords: list[str] = field(default_factory=list)
    search_queries: list[dict[str, Any]] = field(default_factory=list)
    priority_sources: list[str] = field(default_factory=list)
    ambiguity_note: str = ""


@dataclass
class ExtractedPage:
    text: str
    page_number: int | None = None


@dataclass
class ExtractedContent:
    url: str
    title: str
    domain: str
    source_type: str
    document_type: str
    source_quality: float
    retrieved_at: str
    query: str
    pages: list[ExtractedPage]


@dataclass
class EvidenceChunk:
    text: str
    url: str
    title: str
    domain: str
    source_type: str
    document_type: str
    retrieved_at: str
    query: str
    score: float
    section: str
    page_number: int | None = None
    scoring: dict[str, float] = field(default_factory=dict)
    field: str = ""
    support_level: str = "weak"
    selection_reason: str = ""


@dataclass
class ResearchResult:
    query: str
    answer: str
    evidence_chunks: list[EvidenceChunk]
    query_plan: QueryPlan
    debug_info: dict[str, Any]


def canonicalize_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return str(url or "").strip()
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            urlencode(query, doseq=True),
            "",
        )
    )


def _compact(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _is_pdf_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.path.lower().endswith(".pdf")


def _safe_json_loads(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", str(text or ""), flags=re.DOTALL)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_germany_plan(plan: QueryPlan) -> bool:
    haystack = " ".join([plan.country, plan.university, plan.program]).lower()
    return "germany" in haystack or "german" in haystack or ".de" in " ".join(plan.priority_sources)


def _query_mentions_language_requirement(query: str) -> bool:
    lowered = str(query or "").lower()
    return any(term in lowered for term in LANGUAGE_TERMS)


def _normalize_field_name(value: Any) -> str:
    return _compact(value).lower().replace("/", "_").replace("-", "_").replace(" ", "_")


def _field_terms(field_name: str, plan: QueryPlan | None = None) -> list[str]:
    normalized = field_name.replace("_", " ").lower()
    terms = [normalized, field_name.lower(), *FIELD_KEYWORDS.get(field_name, [])]
    if plan is not None:
        terms += plan.keywords + plan.german_keywords
    return list(dict.fromkeys([term.lower() for term in terms if _compact(term)]))


def _infer_intent(query: str) -> str:
    lowered = query.lower()
    if re.search(r"\b(can i apply|eligible|eligibility|am i eligible|profile|gpa)\b", lowered):
        return "eligibility_check"
    if re.search(r"\b(deadline|application period|intake|bewerbungsfrist)\b", lowered):
        return "deadline_lookup"
    if re.search(r"\b(documents?|checklist|unterlagen|transcript|certificate)\b", lowered):
        return "document_requirement_lookup"
    if re.search(r"\b(tuition|fees?|semester contribution|semesterbeitrag|studiengeb)", lowered):
        return "tuition_fee_lookup"
    if re.search(r"\b(curriculum|modules?|module handbook|modulhandbuch|courses?)\b", lowered):
        return "curriculum_lookup"
    if re.search(r"\b(scholarship|funding|financial aid)\b", lowered):
        return "scholarship_funding_lookup"
    if re.search(
        r"\b(how (?:do|to) apply|application process|portal|uni-assist|vpd|aps)\b", lowered
    ):
        return "application_process_lookup"
    if _query_mentions_language_requirement(query) or re.search(
        r"\b(english-taught|german-taught|teaching language|language of instruction)\b", lowered
    ):
        return "language_requirement_lookup"
    if re.search(r"\b(admission requirements?|requirements?|zulassungsvoraussetzungen)\b", lowered):
        return "admission_requirement_lookup"
    if re.search(r"\b(duration|overview|how long|semesters?)\b", lowered):
        return "program_overview_lookup"
    return "general_university_question"


def _intent_profile(query: str) -> dict[str, Any]:
    intent = _infer_intent(query)
    profile = INTENT_PROFILES.get(intent, INTENT_PROFILES["general_university_question"])
    required = list(profile["required"])
    optional = list(profile["optional"])
    lowered = query.lower()
    if "ielts" in lowered and "ielts_score" not in required:
        required.append("ielts_score")
    if "toefl" in lowered and "toefl_score" not in optional:
        optional.append("toefl_score")
    if "duolingo" in lowered and "duolingo_score" not in optional:
        optional.append("duolingo_score")
    if "aps" in lowered and "aps_requirement" not in required + optional:
        required.append("aps_requirement")
    if "uni-assist" in lowered and "uni_assist_requirement" not in required + optional:
        required.append("uni_assist_requirement")
    return {
        "intent": intent,
        "required_fields": list(dict.fromkeys(required)),
        "optional_fields": list(dict.fromkeys(optional)),
        "excluded_fields": list(dict.fromkeys(profile["excluded"])),
    }


def _explicit_non_german_fau(query: str) -> bool:
    lowered = str(query or "").lower()
    return bool(re.search(r"\b(florida|atlantic|usa|united states|america|boca raton)\b", lowered))


def _prefers_german_fau(query: str, plan: QueryPlan | None = None) -> bool:
    haystack = f"{query} {getattr(plan, 'university', '')} {getattr(plan, 'country', '')}".lower()
    return bool(re.search(r"\bfau\b", haystack)) and not _explicit_non_german_fau(query)


def _requested_sections_from_query(query: str, fallback: list[str]) -> list[str]:
    profile = _intent_profile(query)
    return profile["required_fields"] or fallback or ["general_information"]


def _with_german_fau_focus(plan: QueryPlan, query: str) -> QueryPlan:
    if not (GERMAN_UNIVERSITY_FOCUS and _prefers_german_fau(query, plan)):
        return plan
    fau = AMBIGUOUS_GERMAN_UNIVERSITIES["fau"]
    plan.university = plan.university or str(fau["preferred_name"])
    plan.university_short = plan.university_short or "FAU"
    plan.country = plan.country or "Germany"
    plan.priority_sources = list(
        dict.fromkeys([str(fau["preferred_domain"]), *plan.priority_sources, "daad.de"])
    )
    if _query_mentions_language_requirement(query):
        profile = _intent_profile(query)
        plan.required_info = profile["required_fields"]
        plan.required_fields = profile["required_fields"]
        plan.optional_fields = profile["optional_fields"]
        plan.excluded_fields = profile["excluded_fields"]
        plan.intent = profile["intent"]
        plan.keywords = list(dict.fromkeys([*plan.keywords, "IELTS", "English proficiency"]))
    plan.ambiguity_note = (
        "FAU is ambiguous; German university focus prefers Friedrich-Alexander-Universität "
        "Erlangen-Nürnberg (fau.de) over Florida Atlantic University unless Florida/USA is explicit."
    )
    field_terms = " ".join(
        term.replace("_", " ")
        for term in (plan.required_fields or plan.required_info or ["admission requirements"])[:3]
    )
    focused_queries = [
        {
            "query": f"FAU Erlangen Nürnberg {plan.program or query} {field_terms} site:fau.de",
            "type": "official_page",
            "priority": 1.0,
        },
        {
            "query": f"Friedrich-Alexander-Universität Erlangen-Nürnberg {plan.program or query} {field_terms} site:fau.de",
            "type": "official_page",
            "priority": 0.98,
        },
        {
            "query": f"FAU {plan.program or query} {field_terms} filetype:pdf site:fau.de",
            "type": "pdf",
            "priority": 0.92,
        },
        {
            "query": f"FAU Erlangen Nürnberg {plan.program or query} {field_terms} DAAD",
            "type": "daad",
            "priority": 0.72,
        },
    ]
    existing = [item for item in plan.search_queries if isinstance(item, dict)]
    plan.search_queries = [*focused_queries, *existing][:MAX_QUERIES]
    return plan


def _fallback_plan(query: str) -> QueryPlan:
    keywords = [token for token in re.findall(r"[A-Za-zÄÖÜäöüß0-9][\wÄÖÜäöüß-]{2,}", query)[:12]]
    profile = _intent_profile(query)
    required_info = profile["required_fields"]
    field_phrase = " ".join(field.replace("_", " ") for field in required_info[:3])
    queries = [
        {
            "query": f"{query} {field_phrase} official university",
            "type": "official_page",
            "priority": 1.0,
        },
        {
            "query": f"{query} official admissions {field_phrase}",
            "type": profile["intent"],
            "priority": 0.9,
        },
        {"query": f"{query} {field_phrase} filetype:pdf", "type": "pdf", "priority": 0.85},
        {"query": f"{query} DAAD", "type": "daad", "priority": 0.7},
    ]
    plan = QueryPlan(
        country="Germany" if re.search(r"\b(germany|german|deutschland)\b", query, re.I) else "",
        user_intent=query,
        intent=profile["intent"],
        required_info=required_info,
        required_fields=required_info,
        optional_fields=profile["optional_fields"],
        excluded_fields=profile["excluded_fields"],
        keywords=keywords,
        german_keywords=(
            GERMAN_SEARCH_TERMS[:4]
            if re.search(r"\b(germany|german|deutschland)\b", query, re.I)
            else []
        ),
        search_queries=queries[:MAX_QUERIES],
        priority_sources=["daad.de"],
    )
    return _with_german_fau_focus(plan, query)


def _normalize_plan(payload: dict[str, Any], query: str) -> QueryPlan:
    fallback = _fallback_plan(query)
    profile = _intent_profile(query)
    raw_queries = payload.get("search_queries")
    queries: list[dict[str, Any]] = []
    if isinstance(raw_queries, list):
        for item in raw_queries:
            if not isinstance(item, dict):
                continue
            text = _compact(item.get("query"))
            if not text:
                continue
            queries.append(
                {
                    "query": text,
                    "type": _compact(item.get("type")) or "official_page",
                    "priority": float(item.get("priority") or 0.8),
                }
            )
            if len(queries) >= MAX_QUERIES:
                break
    if not queries:
        queries = fallback.search_queries

    required_fields = [
        _normalize_field_name(item)
        for item in payload.get(
            "required_fields", payload.get("required_info", fallback.required_fields)
        )
        if _compact(item)
    ] or profile["required_fields"]
    optional_fields = [
        _normalize_field_name(item)
        for item in payload.get("optional_fields", fallback.optional_fields)
        if _compact(item)
    ] or profile["optional_fields"]
    excluded_fields = [
        _normalize_field_name(item)
        for item in payload.get("excluded_fields", fallback.excluded_fields)
        if _compact(item)
    ] or profile["excluded_fields"]
    deterministic_intent = profile["intent"]
    llm_intent = _compact(payload.get("intent") or payload.get("user_intent")).lower()
    intent = (
        deterministic_intent
        if deterministic_intent != "general_university_question"
        else llm_intent
    )
    if intent not in INTENT_PROFILES:
        intent = deterministic_intent

    plan = QueryPlan(
        university=_compact(payload.get("university")) or fallback.university,
        university_short=_compact(payload.get("university_short")),
        program=_compact(payload.get("program")) or fallback.program,
        country=_compact(payload.get("country")) or fallback.country,
        degree_level=_compact(payload.get("degree_level")) or fallback.degree_level,
        user_intent=_compact(payload.get("user_intent")) or query,
        intent=intent,
        required_info=required_fields,
        required_fields=required_fields,
        optional_fields=optional_fields,
        excluded_fields=excluded_fields,
        user_profile_details=(
            payload.get("user_profile_details", {})
            if isinstance(payload.get("user_profile_details"), dict)
            else {}
        ),
        keywords=[
            _compact(item) for item in payload.get("keywords", fallback.keywords) if _compact(item)
        ],
        german_keywords=[
            _compact(item)
            for item in payload.get("german_keywords", fallback.german_keywords)
            if _compact(item)
        ],
        search_queries=queries,
        priority_sources=[
            _compact(item).lower().removeprefix("www.")
            for item in payload.get("priority_sources", fallback.priority_sources)
            if _compact(item)
        ],
    )
    if _is_germany_plan(plan):
        plan.german_keywords = list(dict.fromkeys([*plan.german_keywords, *GERMAN_SEARCH_TERMS]))[
            :12
        ]
    return _with_german_fau_focus(plan, query)


async def analyze_query(query: str) -> QueryPlan:
    from app.infra.bedrock_chat_client import client as bedrock_client

    system_prompt = """
You plan fast official-source university research. Return only JSON.
Identify: university, university_short, program, country, degree_level, user_intent,
intent, required_fields, optional_fields, excluded_fields, user_profile_details,
keywords, german_keywords, search_queries, priority_sources.

Classify intent as one of: language_requirement_lookup, deadline_lookup,
eligibility_check, document_requirement_lookup, tuition_fee_lookup,
admission_requirement_lookup, program_overview_lookup, curriculum_lookup,
application_process_lookup, general_university_question.

Required fields are facts that must be answered. Optional fields are useful only
when directly relevant. Excluded fields must not appear in the final answer.
For a narrow IELTS question, exclude tuition, GPA, documents, deadlines, GRE, and
curriculum. For a deadline question, exclude IELTS, GPA, tuition, and documents
unless the deadline explicitly depends on them.

Create at most 5 targeted search queries. Include official program/admission pages,
deadlines, language requirements, documents, tuition/fees if relevant, PDFs, DAAD,
and German terms for German universities. Prefer official university domains, DAAD,
.de/.eu, official PDFs, admissions, faculty/program, international office, and
uni-assist where relevant. Avoid blogs, forums, consultants, and unsourced portals.
"""
    user_prompt = f"Question: {query}"
    logger.info("UniGraph query decomposition started | question=%s", query)
    try:
        response = await bedrock_client.chat.completions.create(
            model=settings.bedrock.primary_model_id,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=1800,
        )
        content = response.choices[0].message.content
        plan = _normalize_plan(_safe_json_loads(content), query)
    except Exception as exc:
        logger.warning("UniGraph query decomposition failed; using fallback. error=%s", exc)
        plan = _fallback_plan(query)
    logger.info(
        "UniGraph query decomposition complete | university=%s | program=%s | sections=%s",
        plan.university,
        plan.program,
        plan.required_fields,
    )
    return plan


async def execute_search_queries(plan: QueryPlan) -> tuple[list[dict[str, Any]], int]:
    logger.info("UniGraph generated search queries | queries=%s", plan.search_queries)

    async def _search(query_obj: dict[str, Any]) -> dict[str, Any]:
        query_text = str(query_obj.get("query", "")).strip()
        try:
            payload = await asearch_google(
                query=query_text,
                num=MAX_RESULTS_PER_QUERY,
                search_depth="advanced",
                include_raw_content=False,
                include_answer=False,
            )
            rows = payload.get("organic_results", [])
            logger.info("UniGraph fan-out results | query=%s | count=%s", query_text, len(rows))
            return {
                "query": query_text,
                "type": query_obj.get("type", "official_page"),
                "priority": float(query_obj.get("priority") or 0.8),
                "results": rows if isinstance(rows, list) else [],
            }
        except Exception as exc:
            logger.warning("UniGraph search failed | query=%s | error=%s", query_text, exc)
            return {
                "query": query_text,
                "type": query_obj.get("type", "official_page"),
                "priority": float(query_obj.get("priority") or 0.8),
                "results": [],
                "error": str(exc),
            }

    queries = plan.search_queries[:MAX_QUERIES]
    return list(await asyncio.gather(*[_search(item) for item in queries])), len(queries)


def calculate_source_quality(url: str, *, document_type: str = "html") -> tuple[float, str]:
    domain = _domain(url)
    known_official_university_domains = {
        "fau.de",
        "tum.de",
        "lmu.de",
        "rwth-aachen.de",
        "kit.edu",
        "uni-mannheim.de",
    }
    university_like = (
        domain in known_official_university_domains
        or any(domain.endswith("." + item) for item in known_official_university_domains)
        or domain.endswith(".edu")
        or domain.endswith(".de")
        and any(part in domain for part in ("uni-", "tu-", "tum.", "lmu.", "rwth-", "kit.", "fu-"))
        or any(part in domain for part in ("university", "hochschule"))
    )
    if university_like:
        return 0.95, (
            "official_university_pdf" if document_type == "pdf" else "official_university_page"
        )
    if domain.endswith("daad.de") or domain == "daad.de" or domain.endswith("study-in-germany.de"):
        return 0.85, "daad"
    if "uni-assist" in domain:
        return 0.75, "uni_assist"
    if domain.endswith(".eu") or ".gov" in domain or domain.endswith(".bund.de"):
        return 0.75, "government_or_eu"
    if any(item in domain for item in ("education", "study", "studieren", "mastersportal")):
        return 0.40, "third_party_education_site"
    if any(item in domain for item in LOW_QUALITY_DOMAINS) or "forum" in domain or "blog" in domain:
        return 0.20, "blog_or_forum"
    return 0.50, "other"


def _accepted_search_result(url: str, title: str, snippet: str, plan: QueryPlan) -> bool:
    domain = _domain(url)
    combined = f"{url} {title} {snippet}".lower()
    if any(bad in domain for bad in LOW_QUALITY_DOMAINS):
        return False
    if any(
        source and (domain == source or domain.endswith("." + source))
        for source in plan.priority_sources
    ):
        return True
    quality, source_type = calculate_source_quality(
        url, document_type="pdf" if _is_pdf_url(url) else "html"
    )
    if quality >= 0.75:
        return True
    if (domain.endswith(".de") or domain.endswith(".eu")) and any(
        term in combined for term in OFFICIAL_KEYWORDS
    ):
        return True
    return source_type not in {"blog_or_forum", "third_party_education_site"} and quality >= 0.50


def _skip_reason_for_search_result(url: str, title: str, snippet: str, plan: QueryPlan) -> str:
    domain = _domain(url)
    combined = f"{url} {title} {snippet}".lower()
    if plan.ambiguity_note and "fau.edu" in domain:
        return "ambiguous_secondary_institution_florida_atlantic"
    if any(bad in domain for bad in LOW_QUALITY_DOMAINS):
        return "low_quality_domain"
    if any(term in combined for term in ("consultant", "forum", "reddit", "quora")):
        return "low_quality_or_untrusted_source"
    if not _accepted_search_result(url, title, snippet, plan):
        return "low_quality_or_irrelevant_source"
    return ""


def select_and_deduplicate_urls(
    search_results: list[dict[str, Any]],
    plan: QueryPlan,
    *,
    debug_collector: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped: list[dict[str, str]] = []
    for search_result in search_results:
        query = str(search_result.get("query", ""))
        query_priority = float(search_result.get("priority") or 0.8)
        query_type = str(search_result.get("type", "official_page"))
        for row in search_result.get("results", []):
            if not isinstance(row, dict):
                continue
            raw_url = row.get("link") or row.get("url") or ""
            url = canonicalize_url(str(raw_url))
            if not url or url.lower() in seen:
                if url:
                    skipped.append({"url": url, "reason": "duplicate_url", "query": query})
                continue
            title = _compact(row.get("title"))
            snippet = _compact(row.get("snippet") or row.get("content"))
            skip_reason = _skip_reason_for_search_result(url, title, snippet, plan)
            if skip_reason:
                logger.info("UniGraph filtered URL | url=%s | query=%s", url, query)
                skipped.append({"url": url, "reason": skip_reason, "query": query})
                continue
            seen.add(url.lower())
            document_type = "pdf" if _is_pdf_url(url) or query_type == "pdf" else "html"
            source_quality, source_type = calculate_source_quality(url, document_type=document_type)
            official_boost = 0.25 if source_quality >= 0.75 else 0.0
            pdf_boost = 0.10 if document_type == "pdf" else 0.0
            keyword_boost = (
                0.10
                if any(term in f"{url} {title} {snippet}".lower() for term in OFFICIAL_KEYWORDS)
                else 0.0
            )
            candidates.append(
                {
                    "url": url,
                    "title": title,
                    "snippet": snippet,
                    "query": query,
                    "query_type": query_type,
                    "document_type": document_type,
                    "source_quality": source_quality,
                    "source_type": source_type,
                    "score": (query_priority * 0.55) + official_boost + pdf_boost + keyword_boost,
                }
            )
    candidates.sort(
        key=lambda item: (float(item["score"]), float(item["source_quality"])), reverse=True
    )
    selected = candidates[:MAX_TOTAL_URLS_TO_FETCH]
    logger.info(
        "UniGraph deduplicated URLs | candidates=%s | selected=%s", len(candidates), selected
    )
    if debug_collector is not None:
        debug_collector["skipped_urls"] = skipped
        debug_collector["source_scores"] = [
            {
                "url": item["url"],
                "domain": _domain(str(item["url"])),
                "source_type": item["source_type"],
                "source_quality": item["source_quality"],
                "selection_score": item["score"],
                "query": item["query"],
            }
            for item in candidates
        ]
    return selected


def _html_to_text(raw_html: str) -> str:
    text = re.sub(
        r"<(script|style|nav|header|footer|aside)\b[^>]*>.*?</\1>", " ", raw_html, flags=re.I | re.S
    )
    text = re.sub(
        r"</?(p|br|li|tr|td|th|div|section|article|h[1-6])\b[^>]*>", "\n", text, flags=re.I
    )
    text = re.sub(r"<[^>]+>", " ", text)
    return _compact(unescape(text))


async def extract_html_content(url_info: dict[str, Any]) -> ExtractedContent | None:
    url = str(url_info["url"])
    try:
        payload = await aextract_urls(
            [url], extract_depth="advanced", query=str(url_info.get("query", ""))
        )
    except Exception as exc:
        logger.warning("UniGraph Tavily extract failed | url=%s | error=%s", url, exc)
        return None
    results = payload.get("results", []) if isinstance(payload, dict) else []
    if not results:
        return None
    row = results[0] if isinstance(results[0], dict) else {}
    text = _compact(row.get("raw_content") or row.get("content") or "")
    if "<" in text and ">" in text:
        text = _html_to_text(text)
    if len(text) < 120:
        return None
    return ExtractedContent(
        url=url,
        title=str(url_info.get("title", "")),
        domain=_domain(url),
        source_type=str(url_info.get("source_type", "other")),
        document_type="html",
        source_quality=float(url_info.get("source_quality") or 0.5),
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        query=str(url_info.get("query", "")),
        pages=[ExtractedPage(text=text)],
    )


def _download_pdf_to_temp(url: str) -> str | None:
    max_bytes = MAX_PDF_SIZE_MB * 1024 * 1024
    request = urllib.request.Request(url, headers={"User-Agent": "unigraph-phase1-research/1.0"})
    with urllib.request.urlopen(
        request, timeout=float(settings.web_search.page_fetch_timeout_seconds)
    ) as response:
        content_type = str(response.headers.get("content-type", "")).lower()
        content_length = int(response.headers.get("content-length") or 0)
        if content_length and content_length > max_bytes:
            logger.info("UniGraph skipped oversized PDF | url=%s | size=%s", url, content_length)
            return None
        if "pdf" not in content_type and not _is_pdf_url(url):
            return None
        fd, path = tempfile.mkstemp(prefix="unigraph_pdf_", suffix=".pdf")
        read_total = 0
        with os.fdopen(fd, "wb") as handle:
            while True:
                chunk = response.read(1024 * 512)
                if not chunk:
                    break
                read_total += len(chunk)
                if read_total > max_bytes:
                    handle.close()
                    os.unlink(path)
                    logger.info("UniGraph skipped oversized PDF while reading | url=%s", url)
                    return None
                handle.write(chunk)
    return path


def _extract_pdf_pages(path: str, url: str) -> list[ExtractedPage]:
    if pdfplumber is None:
        logger.warning("UniGraph PDF extraction skipped; pdfplumber is unavailable.")
        return []
    pages: list[ExtractedPage] = []
    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages[:MAX_PDF_PAGES], start=1):
            parts: list[str] = []
            text = page.extract_text() or ""
            if text:
                parts.append(text)
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []
            for table_index, table in enumerate(tables, start=1):
                if not table:
                    continue
                parts.append(f"[Table {table_index}]")
                for row in table:
                    parts.append(" | ".join(_compact(cell) for cell in (row or [])))
            page_text = _compact("\n".join(parts))
            if page_text:
                pages.append(ExtractedPage(text=page_text, page_number=index))
    logger.info("UniGraph PDF read | url=%s | pages=%s", url, len(pages))
    return pages


async def extract_pdf_content(url_info: dict[str, Any]) -> ExtractedContent | None:
    url = str(url_info["url"])

    def _read_pdf() -> list[ExtractedPage]:
        path = _download_pdf_to_temp(url)
        if not path:
            return []
        try:
            return _extract_pdf_pages(path, url)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    try:
        pages = await asyncio.to_thread(_read_pdf)
    except Exception as exc:
        logger.warning("UniGraph PDF extraction failed | url=%s | error=%s", url, exc)
        return None
    if not pages:
        return None
    return ExtractedContent(
        url=url,
        title=str(url_info.get("title", "")),
        domain=_domain(url),
        source_type=str(url_info.get("source_type", "other")),
        document_type="pdf",
        source_quality=float(url_info.get("source_quality") or 0.5),
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        query=str(url_info.get("query", "")),
        pages=pages,
    )


async def extract_all_contents(selected_urls: list[dict[str, Any]]) -> list[ExtractedContent]:
    pdf_seen = 0
    bounded: list[dict[str, Any]] = []
    for item in selected_urls:
        if item.get("document_type") == "pdf":
            if pdf_seen >= MAX_PDFS_TO_READ:
                continue
            pdf_seen += 1
        bounded.append(item)
    logger.info("UniGraph URLs fetched | urls=%s", [item["url"] for item in bounded])
    tasks = [
        (
            extract_pdf_content(item)
            if item.get("document_type") == "pdf"
            else extract_html_content(item)
        )
        for item in bounded
    ]
    results = await asyncio.gather(*tasks)
    extracted = [item for item in results if item is not None]
    logger.info(
        "UniGraph extraction complete | docs=%s | pdfs=%s | html=%s",
        len(extracted),
        sum(1 for item in extracted if item.document_type == "pdf"),
        sum(1 for item in extracted if item.document_type == "html"),
    )
    return extracted


def _chunk_debug_payload(chunk: EvidenceChunk) -> dict[str, Any]:
    return {
        "section": chunk.section,
        "field": chunk.field or chunk.section,
        "support_level": chunk.support_level,
        "selection_reason": chunk.selection_reason,
        "url": chunk.url,
        "title": chunk.title,
        "domain": chunk.domain,
        "source_type": chunk.source_type,
        "document_type": chunk.document_type,
        "page_number": chunk.page_number,
        "retrieved_at": chunk.retrieved_at,
        "query": chunk.query,
        "score": round(float(chunk.score), 4),
        "scoring": {key: round(float(value), 4) for key, value in chunk.scoring.items()},
        "text": chunk.text[:1200],
    }


def chunk_text(
    text: str, *, chunk_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    text = _compact(text)
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        chunk = text[start:end]
        if end < len(text):
            split_at = max(chunk.rfind(". "), chunk.rfind("; "), chunk.rfind(" "))
            if split_at > int(chunk_chars * 0.65):
                end = start + split_at + 1
                chunk = text[start:end]
        chunks.append(chunk.strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _section_keywords(section: str, plan: QueryPlan) -> list[str]:
    return _field_terms(section, plan)


def _keyword_match(text: str, keywords: list[str]) -> float:
    if not keywords:
        return 0.30
    haystack = text.lower()
    matches = sum(1 for keyword in keywords if keyword and keyword.lower() in haystack)
    return min(1.0, matches / max(4, min(len(keywords), 12)))


def _is_language_section(section: str) -> bool:
    lowered = section.replace("_", " ").lower()
    return "language" in lowered or "ielts" in lowered or "english" in lowered or "spra" in lowered


def _chunk_matches_requested_section(chunk: str, section: str, plan: QueryPlan) -> bool:
    lowered = chunk.lower()
    if _is_language_section(section):
        if not any(term in lowered for term in LANGUAGE_TERMS):
            return False
        language_hits = sum(1 for term in LANGUAGE_TERMS if term in lowered)
        unrelated_hits = sum(1 for term in LANGUAGE_UNRELATED_TERMS if term in lowered)
        if unrelated_hits > language_hits + 1:
            return False
    return _keyword_match(chunk, _section_keywords(section, plan)) >= 0.18


def _field_support_level(text: str, field_name: str, score: float) -> str:
    lowered = text.lower()
    direct_patterns = {
        "application_deadline": r"\b\d{1,2}\.?\s*(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)|\b\d{1,2}\.\d{1,2}\.",
        "ielts_score": r"\bielts\b.{0,90}\b[4-9](?:\.\d)?\b",
        "toefl_score": r"\btoefl\b.{0,90}\b\d{2,3}\b",
        "duolingo_score": r"\bduolingo\b.{0,90}\b\d{2,3}\b",
        "gpa_requirement": r"\b(gpa|grade|note)\b.{0,90}\b\d(?:\.\d+)?\b",
        "tuition_fee": r"\b(tuition|fee|studiengeb).{0,90}(€|eur|euro|usd|\d)",
        "semester_contribution": r"\b(semester contribution|semesterbeitrag).{0,90}(€|eur|euro|\d)",
        "program_duration": r"\b(duration|semesters|regelstudienzeit).{0,90}\b\d+\b",
    }
    if re.search(direct_patterns.get(field_name, r"$^"), lowered, re.I):
        return "direct"
    if score >= 0.42:
        return "direct"
    if score >= 0.22:
        return "indirect"
    return "weak"


def _map_chunk_to_fields(chunk: str, plan: QueryPlan) -> list[dict[str, Any]]:
    candidate_fields = list(dict.fromkeys([*plan.required_fields, *plan.optional_fields]))
    excluded = set(plan.excluded_fields)
    mappings: list[dict[str, Any]] = []
    for field_name in candidate_fields:
        if field_name in excluded:
            continue
        if not _chunk_matches_requested_section(chunk, field_name, plan):
            continue
        keyword_score = _keyword_match(chunk, _field_terms(field_name, plan))
        mappings.append(
            {
                "field": field_name,
                "keyword_match": keyword_score,
                "support_level": _field_support_level(chunk, field_name, keyword_score),
                "reason": f"matched keywords for {field_name}",
            }
        )
    mappings.sort(key=lambda item: item["keyword_match"], reverse=True)
    return mappings[:2]


def _excluded_chunk_reason(chunk: str, plan: QueryPlan) -> str:
    lowered = chunk.lower()
    matched_excluded = [
        field_name
        for field_name in plan.excluded_fields
        if _keyword_match(lowered, _field_terms(field_name, None)) >= 0.18
    ]
    if matched_excluded:
        return "matched_excluded_field:" + ",".join(matched_excluded[:3])
    return "no_required_or_optional_field_match"


def _official_rows_or_all(rows: list[EvidenceChunk]) -> list[EvidenceChunk]:
    official = [row for row in rows if float(row.scoring.get("source_quality", 0.0) or 0.0) >= 0.75]
    return official or rows


def _query_relevance(query: str, plan: QueryPlan) -> float:
    for item in plan.search_queries:
        if str(item.get("query")) == query:
            return max(0.0, min(1.0, float(item.get("priority") or 0.5)))
    return 0.5


def group_and_rank_evidence(
    extracted: list[ExtractedContent],
    plan: QueryPlan,
    *,
    debug_collector: dict[str, Any] | None = None,
) -> dict[str, list[EvidenceChunk]]:
    if not plan.required_fields:
        plan.required_fields = plan.required_info or ["general_information"]
    sections = list(dict.fromkeys([*plan.required_fields, *plan.optional_fields]))
    if not sections:
        sections = ["general_information"]
    grouped: dict[str, list[EvidenceChunk]] = {section: [] for section in sections}
    total_chunks = 0
    excluded_chunks: list[dict[str, Any]] = []
    for source in extracted:
        for page in source.pages:
            for chunk in chunk_text(page.text):
                total_chunks += 1
                mappings = _map_chunk_to_fields(chunk, plan)
                if not mappings:
                    excluded_chunks.append(
                        {
                            "url": source.url,
                            "title": source.title,
                            "domain": source.domain,
                            "page_number": page.page_number,
                            "reason": _excluded_chunk_reason(chunk, plan),
                            "text": chunk[:700],
                        }
                    )
                    continue
                for mapping in mappings:
                    section = str(mapping["field"])
                    keyword_match = float(mapping["keyword_match"])
                    source_quality = source.source_quality
                    query_relevance = _query_relevance(source.query, plan)
                    final_score = (
                        (0.50 * keyword_match) + (0.30 * source_quality) + (0.20 * query_relevance)
                    )
                    grouped[section].append(
                        EvidenceChunk(
                            text=chunk,
                            url=source.url,
                            title=source.title,
                            domain=source.domain,
                            source_type=source.source_type,
                            document_type=source.document_type,
                            page_number=page.page_number,
                            retrieved_at=source.retrieved_at,
                            query=source.query,
                            section=section,
                            score=final_score,
                            scoring={
                                "keyword_match": keyword_match,
                                "source_quality": source_quality,
                                "query_relevance": query_relevance,
                            },
                            field=section,
                            support_level=str(mapping["support_level"]),
                            selection_reason=str(mapping["reason"]),
                        )
                    )
    for section, rows in list(grouped.items()):
        rows.sort(key=lambda item: item.score, reverse=True)
        grouped[section] = _official_rows_or_all(rows)
    logger.info(
        "UniGraph chunks created and grouped | chunks=%s | grouped=%s",
        total_chunks,
        {section: len(rows) for section, rows in grouped.items()},
    )
    if debug_collector is not None:
        debug_collector["excluded_evidence_chunks"] = excluded_chunks[:80]
    return grouped


def fan_in_evidence(grouped: dict[str, list[EvidenceChunk]]) -> list[EvidenceChunk]:
    selected: list[EvidenceChunk] = []
    seen: set[tuple[str, str]] = set()
    per_section = max(1, MAX_EVIDENCE_CHUNKS // max(1, len(grouped)))
    for section, rows in grouped.items():
        used = 0
        for chunk in rows:
            key = (chunk.url, chunk.text[:180].lower())
            if key in seen:
                continue
            seen.add(key)
            selected.append(chunk)
            used += 1
            if used >= per_section:
                break
    if len(selected) < MAX_EVIDENCE_CHUNKS:
        leftovers = [chunk for rows in grouped.values() for chunk in rows]
        leftovers.sort(key=lambda item: item.score, reverse=True)
        for chunk in leftovers:
            key = (chunk.url, chunk.text[:180].lower())
            if key in seen:
                continue
            selected.append(chunk)
            seen.add(key)
            if len(selected) >= MAX_EVIDENCE_CHUNKS:
                break
    selected.sort(key=lambda item: item.score, reverse=True)
    logger.info("UniGraph selected evidence chunks | selected=%s", [c.__dict__ for c in selected])
    return selected[:MAX_EVIDENCE_CHUNKS]


def _evidence_context(chunks: list[EvidenceChunk]) -> str:
    lines: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        page = f", page {chunk.page_number}" if chunk.page_number else ""
        lines.append(
            f"[E{index}] field={chunk.field or chunk.section}; support_level={chunk.support_level}; "
            f"source_type={chunk.source_type}; "
            f"document_type={chunk.document_type}{page}; title={chunk.title or chunk.url}; "
            f"url={chunk.url}; retrieved_at={chunk.retrieved_at}"
        )
        lines.append(chunk.text[:1200])
        lines.append("")
    return "\n".join(lines)


async def generate_answer(
    query: str,
    evidence_chunks: list[EvidenceChunk],
    plan: QueryPlan,
    field_confidence: dict[str, str] | None = None,
    field_missing_reasons: dict[str, str] | None = None,
) -> str:
    from app.infra.bedrock_chat_client import client as bedrock_client

    if not evidence_chunks:
        missing = field_missing_reasons or {
            field: _missing_reason_for_field(field, {})
            for field in (plan.required_fields or plan.required_info)
        }
        return (
            " ".join(missing.values())
            or "No retrieved evidence directly answers the requested field."
        )

    system_prompt = """
You are UniGraph. Answer university questions only from the provided extracted
evidence. Do not use memory or guesses.

Rules:
- Answer the user's exact question first and cite factual claims with [E#].
- Use only evidence mapped to required_fields or relevant optional_fields.
- Never include excluded_fields, even if retrieved evidence contains them.
- Do not use memory or unsupported claims.
- Explain missing evidence with the exact field-specific reason provided; avoid
  generic uncertainty wording.
- Prefer official university evidence over DAAD, DAAD over uni-assist, and
  uni-assist over third-party sources. Do not cite third-party evidence when
  official evidence supports the same claim.
- Label any third-party source as non-official if it must be used.
- If the university abbreviation is ambiguous, briefly mention the ambiguity and
  answer the most likely German target first. Do not give a full answer for the
  secondary institution unless the user asked for it.
- If trusted sources conflict, state the conflict and apply the source priority.
- Adapt the format to the intent: brief for language lookups, clear date/intake
  for deadlines, checklist for documents, cautious profile comparison for
  eligibility, and concise overview for program questions.
"""
    user_prompt = (
        f"Question: {query}\n\n"
        f"Detected intent: {plan.intent}\n"
        f"Required fields: {json.dumps(plan.required_fields or plan.required_info, ensure_ascii=False)}\n"
        f"Optional fields: {json.dumps(plan.optional_fields, ensure_ascii=False)}\n"
        f"Excluded fields: {json.dumps(plan.excluded_fields, ensure_ascii=False)}\n"
        f"Query analysis: {json.dumps(plan.__dict__, ensure_ascii=False)[:2500]}\n\n"
        f"Field-level confidence: {json.dumps(field_confidence or {}, ensure_ascii=False)}\n\n"
        f"Field-specific missing reasons: {json.dumps(field_missing_reasons or {}, ensure_ascii=False)}\n\n"
        f"Extracted evidence:\n{_evidence_context(evidence_chunks)}"
    )
    try:
        response = await bedrock_client.chat.completions.create(
            model=settings.bedrock.primary_model_id,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2200,
        )
        return str(response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("UniGraph answer generation failed | error=%s", exc)
        return "I could not generate a grounded answer from the retrieved evidence."


def _fields_not_verified(plan: QueryPlan, grouped: dict[str, list[EvidenceChunk]]) -> list[str]:
    missing: list[str] = []
    for section in plan.required_fields or plan.required_info or ["general_information"]:
        rows = grouped.get(section, [])
        if not rows or rows[0].score < 0.48:
            missing.append(section)
    return missing


def _missing_reason_for_field(field_name: str, grouped: dict[str, list[EvidenceChunk]]) -> str:
    rows = grouped.get(field_name, [])
    if field_name == "ielts_score":
        language_text = " ".join(
            row.text for row in grouped.get("english_language_requirement", [])[:5]
        )
        if language_text:
            return "Retrieved evidence discusses English proficiency, but does not state a specific IELTS band score for this program."
        return "No retrieved evidence states a specific IELTS band score for this program."
    if field_name == "application_deadline":
        return "The retrieved evidence does not clearly state the application deadline for the requested program."
    if field_name == "gpa_requirement":
        return "The retrieved official sources do not state a fixed minimum GPA; eligibility may depend on formal academic assessment."
    if field_name == "required_application_documents":
        return "I found no retrieved program-specific application document checklist."
    if field_name == "tuition_fee":
        return "The retrieved evidence does not clearly state the tuition fee for the requested applicant context."
    if field_name in {"aps_requirement", "vpd_requirement", "uni_assist_requirement"}:
        return f"The retrieved evidence does not clearly verify the {field_name.replace('_', ' ')}."
    if rows:
        return f"Retrieved evidence for {field_name.replace('_', ' ')} is indirect or weak."
    return f"No retrieved evidence directly verifies {field_name.replace('_', ' ')}."


def _field_statuses(
    plan: QueryPlan, grouped: dict[str, list[EvidenceChunk]]
) -> tuple[list[str], list[str], dict[str, str]]:
    answered: list[str] = []
    partial: list[str] = []
    missing: dict[str, str] = {}
    for field_name in plan.required_fields or plan.required_info or ["general_information"]:
        rows = grouped.get(field_name, [])
        if not rows:
            missing[field_name] = _missing_reason_for_field(field_name, grouped)
            continue
        best = rows[0]
        if best.score >= 0.58 and best.support_level in {"direct", "indirect"}:
            answered.append(field_name)
        elif best.score >= 0.40:
            partial.append(field_name)
            missing[field_name] = _missing_reason_for_field(field_name, grouped)
        else:
            missing[field_name] = _missing_reason_for_field(field_name, grouped)
    return answered, partial, missing


def _confidence(chunks: list[EvidenceChunk], fields_not_verified: list[str]) -> float:
    if not chunks:
        return 0.0
    avg = sum(chunk.score for chunk in chunks[:5]) / min(5, len(chunks))
    penalty = min(0.35, len(fields_not_verified) * 0.08)
    return max(0.0, min(1.0, avg - penalty))


def _confidence_label(score: float) -> str:
    if score >= 0.72:
        return "high"
    if score >= 0.52:
        return "medium"
    if score > 0.0:
        return "low"
    return "not verified"


def _third_party_usage(
    evidence: list[EvidenceChunk], grouped: dict[str, list[EvidenceChunk]]
) -> list[dict[str, Any]]:
    usage: list[dict[str, Any]] = []
    for chunk in evidence:
        if chunk.source_type not in {"third_party_education_site", "blog_or_forum", "other"}:
            continue
        field_name = chunk.field or chunk.section
        official_available = any(
            row.source_type
            in {
                "official_university_page",
                "official_university_pdf",
                "daad",
                "uni_assist",
                "government_or_eu",
            }
            for row in grouped.get(field_name, [])
        )
        usage.append(
            {
                "field": field_name,
                "url": chunk.url,
                "source_type": chunk.source_type,
                "reason": (
                    "used because no official or trusted source was selected for this field"
                    if not official_available
                    else "selected as supplementary evidence despite trusted evidence being available"
                ),
            }
        )
    return usage


def _field_level_confidence(
    query: str,
    plan: QueryPlan,
    grouped: dict[str, list[EvidenceChunk]],
) -> dict[str, str]:
    confidence: dict[str, str] = {}
    for section in plan.required_fields or plan.required_info or ["general_information"]:
        rows = grouped.get(section, [])
        best_score = rows[0].score if rows else 0.0
        confidence[section] = _confidence_label(best_score)
    if _query_mentions_language_requirement(query):
        language_rows = grouped.get("english_language_requirement", [])
        language_text = " ".join(row.text for row in language_rows[:6]).lower()
        official_language = any(
            float(row.scoring.get("source_quality", 0.0) or 0.0) >= 0.75 for row in language_rows
        )
        confidence["English B2 requirement"] = (
            "high" if official_language and re.search(r"\bb2\b", language_text) else "not verified"
        )
        confidence["numeric IELTS score"] = (
            "high"
            if official_language and re.search(r"\bielts\b.{0,80}\b[4-9](?:\.\d)?\b", language_text)
            else "not verified"
        )
        confidence["per-section IELTS score"] = (
            "high"
            if official_language
            and re.search(
                r"\b(reading|writing|speaking|listening)\b.{0,80}\b[4-9](?:\.\d)?\b", language_text
            )
            else "not verified"
        )
    return confidence


async def research_university_question(query: str) -> ResearchResult:
    started = time.perf_counter()
    logger.info("UniGraph Phase 1 started | user_question=%s", query)
    debug_collector: dict[str, Any] = {}
    plan = await analyze_query(query)
    search_results, tavily_calls = await execute_search_queries(plan)
    selected_urls = select_and_deduplicate_urls(
        search_results,
        plan,
        debug_collector=debug_collector,
    )
    extracted = await extract_all_contents(selected_urls)
    grouped = group_and_rank_evidence(extracted, plan, debug_collector=debug_collector)
    evidence = fan_in_evidence(grouped)
    fields_not_verified = _fields_not_verified(plan, grouped)
    fields_answered, fields_partially_answered, fields_missing_with_reason = _field_statuses(
        plan, grouped
    )
    confidence = _confidence(evidence, fields_not_verified)
    field_confidence = _field_level_confidence(query, plan, grouped)
    answer = await generate_answer(
        query, evidence, plan, field_confidence, fields_missing_with_reason
    )
    duration = time.perf_counter() - started
    chunks_created_detail = [
        {
            "url": item.url,
            "title": item.title,
            "domain": item.domain,
            "source_type": item.source_type,
            "document_type": item.document_type,
            "page_number": page.page_number,
            "retrieved_at": item.retrieved_at,
            "query": item.query,
            "chunks": chunk_text(page.text),
        }
        for item in extracted
        for page in item.pages
    ]
    debug_info = {
        "user_question": query,
        "query_decomposition": plan.__dict__,
        "detected_intent": plan.intent,
        "required_fields": plan.required_fields or plan.required_info,
        "optional_fields": plan.optional_fields,
        "excluded_fields": plan.excluded_fields,
        "generated_search_strategy": {
            "intent": plan.intent,
            "required_information_sections": plan.required_fields or plan.required_info,
            "optional_fields": plan.optional_fields,
            "excluded_fields": plan.excluded_fields,
            "priority_sources": plan.priority_sources,
            "keywords": plan.keywords,
            "german_keywords": plan.german_keywords,
            "limits": {
                "max_queries": MAX_QUERIES,
                "max_results_per_query": MAX_RESULTS_PER_QUERY,
                "max_total_urls_to_fetch": MAX_TOTAL_URLS_TO_FETCH,
                "max_pdfs_to_read": MAX_PDFS_TO_READ,
                "max_pdf_size_mb": MAX_PDF_SIZE_MB,
                "max_pdf_pages": MAX_PDF_PAGES,
                "max_evidence_chunks": MAX_EVIDENCE_CHUNKS,
            },
        },
        "generated_search_queries": plan.search_queries,
        "tavily_calls_used": tavily_calls
        + sum(1 for item in extracted if item.document_type == "html"),
        "fan_out_search_results": search_results,
        "raw_search_results": search_results,
        "filtered_urls": selected_urls,
        "deduplicated_urls": [item["url"] for item in selected_urls],
        "skipped_urls": debug_collector.get("skipped_urls", []),
        "urls_fetched": [item.url for item in extracted],
        "fetched_urls": [item.url for item in extracted],
        "pdfs_read": [item.url for item in extracted if item.document_type == "pdf"],
        "pdf_pages_extracted": {
            item.url: [page.page_number for page in item.pages if page.page_number is not None]
            for item in extracted
            if item.document_type == "pdf"
        },
        "chunks_created": sum(
            len(chunk_text(page.text)) for item in extracted for page in item.pages
        ),
        "chunks_created_detail": chunks_created_detail,
        "grouped_evidence_by_requested_section": {
            section: {
                "chunk_count": len(rows),
                "top_chunks": [_chunk_debug_payload(chunk) for chunk in rows[:5]],
            }
            for section, rows in grouped.items()
        },
        "selected_evidence_chunks": [_chunk_debug_payload(chunk) for chunk in evidence],
        "field_mapped_evidence": [_chunk_debug_payload(chunk) for chunk in evidence],
        "excluded_evidence_chunks": debug_collector.get("excluded_evidence_chunks", []),
        "source_scores": debug_collector.get("source_scores", []),
        "fan_in_evidence": [
            {"section": chunk.section, "url": chunk.url, "score": chunk.score} for chunk in evidence
        ],
        "total_tavily_calls": tavily_calls
        + sum(1 for item in extracted if item.document_type == "html"),
        "final_confidence": confidence,
        "field_level_confidence": field_confidence,
        "fields_not_verified": fields_not_verified,
        "fields_answered": fields_answered,
        "fields_partially_answered": fields_partially_answered,
        "fields_missing_with_reason": fields_missing_with_reason,
        "third_party_sources_used": _third_party_usage(evidence, grouped),
        "duration_seconds": duration,
    }
    logger.info(
        "UniGraph Phase 1 complete | confidence=%.3f | fields_not_verified=%s | tavily_calls=%s",
        confidence,
        fields_not_verified,
        debug_info["total_tavily_calls"],
    )
    return ResearchResult(
        query=query,
        answer=answer,
        evidence_chunks=evidence,
        query_plan=plan,
        debug_info=debug_info,
    )


def _chunk_to_retrieval_row(index: int, chunk: EvidenceChunk) -> dict[str, Any]:
    return {
        "chunk_id": f"unigraph:phase1:evidence:{index}",
        "content": chunk.text,
        "distance": max(0.0, 1.0 - chunk.score),
        "metadata": {
            "url": chunk.url,
            "title": chunk.title,
            "domain": chunk.domain,
            "source_type": chunk.source_type,
            "document_type": chunk.document_type,
            "page_number": chunk.page_number,
            "retrieved_at": chunk.retrieved_at,
            "query": chunk.query,
            "section": chunk.section,
            "field": chunk.field or chunk.section,
            "support_level": chunk.support_level,
            "selection_reason": chunk.selection_reason,
            "score": chunk.score,
            **chunk.scoring,
        },
    }


async def aretrieve_web_chunks(query: str, **kwargs) -> dict[str, Any]:
    debug_enabled = bool(kwargs.get("debug", False))
    result = await research_university_question(query)
    rows = [
        _chunk_to_retrieval_row(index, chunk)
        for index, chunk in enumerate(result.evidence_chunks, start=1)
    ]
    payload = {
        "query": query,
        "results": rows,
        "answer": result.answer,
        "retrieval_strategy": "unigraph_phase1_official_source_research",
        "web_retrieval_verified": bool(result.evidence_chunks)
        and not result.debug_info.get("fields_not_verified"),
        "confidence": result.debug_info.get("final_confidence", 0.0),
        "field_level_confidence": result.debug_info.get("field_level_confidence", {}),
        "fields_not_verified": result.debug_info.get("fields_not_verified", []),
        "coverage_ledger": [
            {
                "field": section,
                "label": section.replace("_", " ").title(),
                "status": (
                    "not_verified"
                    if section in result.debug_info.get("fields_not_verified", [])
                    else "found"
                ),
                "value": (
                    ""
                    if section in result.debug_info.get("fields_not_verified", [])
                    else "Verified from selected evidence."
                ),
                "source_url": next(
                    (
                        row["metadata"]["url"]
                        for row in rows
                        if row["metadata"]["section"] == section
                    ),
                    "",
                ),
                "source_type": next(
                    (
                        row["metadata"]["source_type"]
                        for row in rows
                        if row["metadata"]["section"] == section
                    ),
                    "",
                ),
                "confidence": result.debug_info.get("final_confidence", 0.0),
                "confidence_label": result.debug_info.get("field_level_confidence", {}).get(
                    section, ""
                ),
            }
            for section in (
                result.query_plan.required_fields
                or result.query_plan.required_info
                or ["general_information"]
            )
        ],
    }
    if debug_enabled:
        payload["debug"] = result.debug_info
    return payload
