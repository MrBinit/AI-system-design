import asyncio
import contextvars
import hashlib
import io
import inspect
import json
import logging
import re
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urldefrag, urljoin, urlparse

from app.core.config import get_settings
from app.infra.io_limiters import DependencyBackpressureError, dependency_limiter
from app.infra.redis_client import app_scoped_key, async_redis_client
from app.services.chat_trace_service import emit_trace_event
from app.services.research_orchestrator import (
    build_queries_for_missing_objectives,
    build_research_plan,
    research_objective_coverage,
)
from app.services.student_qa_retrieval_orchestrator import (
    augment_retrieval_result_with_student_contract,
)
from app.services.student_qa_schema_registry import resolve_question_schema
from app.services.tavily_search_service import aextract_urls, asearch_google, asearch_google_batch
from redis.exceptions import RedisError

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover - optional dependency guard
    PdfReader = None

settings = get_settings()
logger = logging.getLogger(__name__)


def _with_student_contract(query: str, result: dict) -> dict:
    if not isinstance(result, dict):
        return result
    try:
        return augment_retrieval_result_with_student_contract(query, result)
    except Exception as exc:
        logger.warning("Student-QA contract augmentation failed; returning raw retrieval result. %s", exc)
        return result

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", flags=re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", flags=re.DOTALL)
_BOILERPLATE_BLOCK_RE = re.compile(
    r"<(nav|footer|header|aside|form|noscript|svg)\b[^>]*>.*?</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)
_BLOCK_BREAK_RE = re.compile(
    r"</?(article|section|main|div|p|li|ul|ol|h[1-6]|table|tr|td|th|br)\b[^>]*>",
    flags=re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_ANCHOR_TAG_RE = re.compile(
    r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    flags=re.IGNORECASE | re.DOTALL,
)
_WHITESPACE_RE = re.compile(r"\s+")
_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_FIELD_BOUNDARY_LABEL_RE = re.compile(
    r"\s+(?=("
    r"eligibility requirements?|admission requirements?|language of instruction|"
    r"language requirements?|program start|academic calendar|school|semester fee|"
    r"tuition fees?|application deadlines?|application deadline|application portal|"
    r"gpa|grade threshold|minimum grade|ects|prerequisite credits?"
    r")\s*:)",
    flags=re.IGNORECASE,
)
_NEXT_FIELD_LABEL_RE = re.compile(
    r"\s+(?="
    r"(?:language of instruction|language requirements?|program start|academic calendar|"
    r"school|semester fee|tuition fees?|application deadlines?|application deadline|"
    r"application portal|gpa|grade threshold|minimum grade|ects|prerequisite credits?)"
    r"\s*:)",
    flags=re.IGNORECASE,
)
_DEADLINE_HINT_RE = re.compile(
    r"\b(deadline|apply by|last date|closing date|intake)\b",
    flags=re.IGNORECASE,
)
_REQUIREMENTS_HINT_RE = re.compile(
    r"\b(requirements?|eligibility|admission requirements?|documents?)\b",
    flags=re.IGNORECASE,
)
_LANGUAGE_HINT_RE = re.compile(
    r"\b(language|ielts|toefl|english|german|international students?)\b",
    flags=re.IGNORECASE,
)
_CURRICULUM_HINT_RE = re.compile(
    r"\b(curriculum|module|course structure|syllabus)\b",
    flags=re.IGNORECASE,
)
_TUITION_HINT_RE = re.compile(
    r"\b(tuition|fees|semester contribution|cost)\b",
    flags=re.IGNORECASE,
)
_PORTAL_HINT_RE = re.compile(
    r"\b(portal|application portal|online application|apply online|bewerbungsportal|"
    r"where (?:can i|to) apply|how to apply|where can i apply)\b",
    flags=re.IGNORECASE,
)
_NEWS_HINT_RE = re.compile(
    r"\b(news|latest|recent|today|this week|this month|update|updated)\b",
    flags=re.IGNORECASE,
)
_META_PUBLISHED_RE = [
    re.compile(
        r"<meta[^>]+(?:property|name)\s*=\s*[\"']"
        r"(?:article:published_time|publishdate|pubdate|date|dc\.date|og:updated_time)"
        r"[\"'][^>]+content\s*=\s*[\"']([^\"']+)[\"']",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"<meta[^>]+content\s*=\s*[\"']([^\"']+)[\"'][^>]+(?:property|name)\s*=\s*[\"']"
        r"(?:article:published_time|publishdate|pubdate|date|dc\.date|og:updated_time)"
        r"[\"']",
        flags=re.IGNORECASE,
    ),
]
_TIME_TAG_RE = re.compile(r"<time[^>]+datetime\s*=\s*[\"']([^\"']+)[\"']", flags=re.IGNORECASE)
_DATE_LIKE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    flags=re.IGNORECASE,
)
_NUMERIC_TOKEN_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")
_DEADLINE_CONTENT_RE = re.compile(
    r"\b(application deadline|deadline|application period|apply by|closing date|"
    r"bewerbungsfrist|bewerbungszeitraum|frist|wintersemester|sommersemester)\b",
    flags=re.IGNORECASE,
)
_DATE_VALUE_RE = re.compile(
    r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b|"
    r"\b(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|"
    r"sep|sept|september|oct|october|nov|november|dec|december)\b",
    flags=re.IGNORECASE,
)
_LANGUAGE_CONTENT_RE = re.compile(
    r"\b(language requirement|english requirement|german requirement|english proficiency|proof of english|proof of german|"
    r"sprachnachweis|sprachkenntnisse|sprachvoraussetzungen|ielts|toefl|cefr|"
    r"testdaf|dsh|cambridge|german)\b",
    flags=re.IGNORECASE,
)
_INSTRUCTION_LANGUAGE_CONTENT_RE = re.compile(
    r"\b(language of instruction|teaching language|taught in|unterrichtssprache|"
    r"lehrsprache|sprache der lehre)\b",
    flags=re.IGNORECASE,
)
_INSTRUCTION_LANGUAGE_VALUE_RE = re.compile(
    r"\b(english|german|deutsch|englisch|bilingual)\b",
    flags=re.IGNORECASE,
)
_LANGUAGE_SCORE_RE = re.compile(
    r"\b(ielts|toefl|cefr|unicert|cambridge)\b.{0,25}\b\d",
    flags=re.IGNORECASE,
)
_LANGUAGE_SCORE_VALUE_RE = re.compile(
    r"\b(ielts|toefl(?:\s*ibt)?|testdaf|dsh|telc|cefr|uni-?cert|cambridge)\b[^.;,\n]{0,120}\b(\d+(?:[.,]\d+)?)\b",
    flags=re.IGNORECASE,
)
_LANGUAGE_LEVEL_VALUE_RE = re.compile(
    r"\b(?:cefr\s*)?([ABC][12](?:[./][12])?)\b|"
    r"\b(?:dsh[-\s]?[123]|testdaf(?:\s*(?:tdn|level)?\s*[345]))\b",
    flags=re.IGNORECASE,
)
_LANGUAGE_NAME_VALUE_RE = re.compile(
    r"\b(english|englisch|german|deutsch)\b",
    flags=re.IGNORECASE,
)
_ADMISSION_CONTENT_RE = re.compile(
    r"\b(admission requirements?|eligibility|entry requirements?|bachelor|qualifying degree|"
    r"documents?|zulassung|zulassungsvoraussetzungen|auswahlsatzung)\b",
    flags=re.IGNORECASE,
)
_GPA_CONTENT_RE = re.compile(
    r"\b(gpa|grade point|minimum grade|cgpa|grade average|grade threshold|"
    r"mindestnote|abschlussnote|notendurchschnitt)\b",
    flags=re.IGNORECASE,
)
_GPA_VALUE_RE = re.compile(
    r"\b(?:mindestnote|minimum grade|grade threshold|grade average|gpa|cgpa)\b[^.;,\n]{0,35}\b([1-4](?:[.,]\d{1,2})?)\b",
    flags=re.IGNORECASE,
)
_GPA_FALLBACK_VALUE_RE = re.compile(
    r"\b(?:note|abschlussnote|notendurchschnitt)\b[^.;,\n]{0,60}\b([1-4](?:[.,]\d{1,2})?)\b",
    flags=re.IGNORECASE,
)
_DURATION_ECTS_CONTENT_RE = re.compile(
    r"\b(ects|credit points?|cp|semester|semesters|duration|years?)\b",
    flags=re.IGNORECASE,
)
_ECTS_VALUE_RE = re.compile(
    r"\b(\d{1,3})\s*(?:ects|credit points?|credits?|cp|leistungspunkte?)\b",
    flags=re.IGNORECASE,
)
_CURRICULUM_CONTENT_RE = re.compile(
    r"\b(curriculum|course structure|study plan|modules?|regulations?|pruefungsordnung)\b",
    flags=re.IGNORECASE,
)
_TUITION_CONTENT_RE = re.compile(
    r"\b(tuition|fees|semester contribution|costs?)\b",
    flags=re.IGNORECASE,
)
_PORTAL_CONTENT_RE = re.compile(
    r"\b(application portal|online application|apply online|bewerbungsportal|application system|"
    r"where to apply|how to apply|apply via)\b",
    flags=re.IGNORECASE,
)
_PORTAL_URL_RE = re.compile(
    r"(portal2?|bewerbungs?-?portal|application-?portal|apply(?:-?online)?|online-?application|"
    r"uni-?assist|myassist|campo|campus|hisinone|almaweb|bewerbung)",
    flags=re.IGNORECASE,
)
_PORTAL_SOURCE_URL_RE = re.compile(
    r"(portal2?|bewerbungs?-?portal|application-?portal|apply-?online|online-?application|"
    r"uni-?assist|myassist|campo|hisinone|almaweb|campus\.)",
    flags=re.IGNORECASE,
)
_PORTAL_APPLY_SOURCE_URL_RE = re.compile(
    r"(apply|application-?portal|online-?application|bewerbung|bewerbungsportal|zulassung)",
    flags=re.IGNORECASE,
)
_GERMAN_DEADLINE_ENHANCED_RE = re.compile(
    r"\b(bewerbungsfrist|bewerbungszeitraum|bis zum|frist.{0,20}(?:winter|sommer)semester)\b[^.;]{0,60}(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})",
    flags=re.IGNORECASE,
)
_GERMAN_GPA_ENHANCED_RE = re.compile(
    r"\b(mindestnote|notendurchschnitt|abschlussnote|durchschnittsnote)\b[^.;]{0,40}\b([1-4][,\.]\d{1,2})\b",
    flags=re.IGNORECASE,
)
_GERMAN_SEMESTER_INTAKE_RE = re.compile(
    r"\b(wintersemester|sommersemester|winter\s*semester|summer\s*semester)\b[^.;]{0,40}\b(20\d{2}(?:/\d{2,4})?)\b",
    flags=re.IGNORECASE,
)
_MODULHANDBUCH_SECTION_RE = re.compile(
    r"(Modul\s+\d+|Module\s+\d+|Pflichtmodul|Wahlpflichtmodul)[:,\s]+([^\n]{10,120})",
    flags=re.IGNORECASE,
)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)
_URL_VALUE_RE = re.compile(r"(?:https?://|www\.)[^\s)]+", flags=re.IGNORECASE)
_DEADLINE_VALUE_RE = re.compile(
    r"\b(?:\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?|\d{4}-\d{2}-\d{2}|"
    r"(?:\d{1,2}\s+)?(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|"
    r"jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)"
    r"(?:\s+\d{4})?)\b",
    flags=re.IGNORECASE,
)
_DEADLINE_RANGE_VALUE_RE = re.compile(
    r"(\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\s*(?:-|–|to)\s*\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)",
    flags=re.IGNORECASE,
)
_DEADLINE_URL_HINT_RE = re.compile(
    r"(deadline|deadlines|application-period|application-deadline|bewerbungsfrist|fristen|apply)",
    flags=re.IGNORECASE,
)
_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "for",
    "from",
    "how",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
}
_BOILERPLATE_LINE_MARKERS = {
    "cookie",
    "privacy policy",
    "terms of use",
    "all rights reserved",
    "sign in",
    "log in",
    "subscribe",
    "newsletter",
    "javascript is disabled",
    "enable javascript",
    "accept all",
}
_HIGH_AUTHORITY_SUFFIXES = (
    ".gov",
    ".edu",
    ".ac.uk",
    ".europa.eu",
)
_GERMAN_UNIVERSITY_DOMAIN_PATTERNS = (
    r"^uni-[a-z-]+\.de$",
    r"^tu-[a-z-]+\.de$",
    r"^fh-[a-z-]+\.de$",
    r"^hs-[a-z-]+\.de$",
    r"^hochschule-[a-z-]+\.de$",
)
_GERMAN_OFFICIAL_EDUCATION_DOMAINS = (
    "daad.de",
    "study-in-germany.de",
    "study-in.de",
    "bmbf.de",
    "kmk.org",
    "hochschulkompass.de",
    "studieren.de",
)
_GERMAN_UNIVERSITY_PAGE_PATTERNS = (
    "/admission",
    "/bewerbung",
    "/zulassung",
    "/international",
    "/master",
    "/studium",
    "/studiengaenge",
    "/requirements",
    "/voraussetzungen",
    "/pruefungsordnung",
    "/modulhandbuch",
    "/auswahlsatzung",
)
_OFFICIAL_SOURCE_HOST_MARKERS = (
    "uni",
    "university",
    "universit",
    "universitaet",
    "hochschule",
    "college",
)
_OFFICIAL_SOURCE_HOST_PREFIXES = ("uni", "tu", "th", "fh", "hs")
_OFFICIAL_SOURCE_TEXT_MARKERS = (
    "university",
    "universität",
    "universitaet",
    "hochschule",
    "faculty",
    "department",
    "school of",
    "institute",
)
_ACADEMIC_PAGE_MARKERS = (
    "master",
    "m.sc",
    "msc",
    "admission",
    "requirements",
    "application",
    "deadline",
    "study program",
    "programme",
    "studium",
    "bewerbung",
)
_NON_OFFICIAL_HOST_MARKERS = (
    "blog",
    "forum",
    "wiki",
    "guide",
    "ranking",
    "rankings",
    "portal",
    "directory",
    "listing",
    "review",
    "consult",
    "news",
    "magazine",
    "substack",
    "medium",
    "reddit",
    "quora",
    "linkedin",
    "wikipedia",
    "newsroom",
)
_ACRONYM_LIKE_HOST_BLOCKLIST = {"dfg", "dlr"}  # Removed "daad" to enable DAAD.de recognition
_DOMAIN_INFERENCE_STOPWORDS = {
    "msc",
    "m.sc",
    "master",
    "masters",
    "program",
    "programme",
    "course",
    "requirements",
    "admission",
    "deadline",
    "application",
    "language",
    "ielts",
    "toefl",
}
_DOMAIN_SLUG_TOKEN_ALIASES = {
    "tubingen": "tuebingen",
    "munchen": "muenchen",
    "koln": "koeln",
    "dusseldorf": "duesseldorf",
    "wurzburg": "wuerzburg",
    "nurnberg": "nuernberg",
}
_KNOWN_QUERY_PHRASE_DOMAIN_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("university of mannheim", ("uni-mannheim.de", "portal2.uni-mannheim.de")),
    ("mannheim msc business informatics", ("uni-mannheim.de", "portal2.uni-mannheim.de")),
    ("technical university of munich", ("tum.de",)),
    ("technische universitat munchen", ("tum.de",)),
    ("technische universitaet muenchen", ("tum.de",)),
    ("tum munich", ("tum.de",)),
)
_ADMISSIONS_HIGH_PRECISION_FIELD_IDS = {
    "admission_requirements",
    "gpa_threshold",
    "ects_breakdown",
    "instruction_language",
    "language_requirements",
    "language_score_thresholds",
    "application_deadline",
    "application_portal",
}
_STUDENT_SLOT_TO_RETRIEVAL_FIELD_ID = {
    "program_overview": "program_overview",
    "eligibility_requirements": "admission_requirements",
    "gpa_or_grade_threshold": "gpa_threshold",
    "gpa_threshold": "gpa_threshold",
    "ects_or_prerequisite_credit_breakdown": "ects_breakdown",
    "ects_prerequisites": "ects_breakdown",
    "instruction_language": "instruction_language",
    "language_requirements": "language_requirements",
    "language_test_score_thresholds": "language_score_thresholds",
    "language_test_thresholds": "language_score_thresholds",
    "application_deadline": "application_deadline",
    "international_deadline": "application_deadline",
    "application_portal": "application_portal",
    "duration_ects": "duration_ects",
    "tuition_or_fees": "tuition_fees",
    "curriculum_focus": "curriculum_modules",
    "professors_or_supervisors": "professors_or_supervisors",
    "labs_or_research_groups": "labs_or_research_groups",
    "contact_information": "contact_information",
    "visa_or_work_rights": "visa_or_work_rights",
    "funding_or_scholarship": "funding_or_scholarship",
    "admission_decision_signal": "admission_decision_signal",
}
_RETRIEVAL_FIELD_CATALOG: dict[str, dict] = {
    "program_overview": {
        "id": "program_overview",
        "label": "program overview",
        "subquestion": "program overview, degree type, department, and teaching language",
        "query_focus": "official program overview degree language",
    },
    "admission_requirements": {
        "id": "admission_requirements",
        "label": "course requirements",
        "subquestion": "course requirements, eligibility criteria, and required documents",
        "query_focus": "admission requirements eligibility required documents",
    },
    "gpa_threshold": {
        "id": "gpa_threshold",
        "label": "GPA/grade threshold",
        "subquestion": "minimum GPA/grade threshold and grading scale details",
        "query_focus": "minimum GPA grade threshold admission score requirement",
    },
    "ects_breakdown": {
        "id": "ects_breakdown",
        "label": "ECTS/prerequisite credits",
        "subquestion": "required ECTS or prerequisite credit breakdown by subject area",
        "query_focus": "required ECTS prerequisite credits mathematics computer science",
    },
    "language_requirements": {
        "id": "language_requirements",
        "label": "language requirements",
        "subquestion": "language requirements with accepted tests and minimum scores",
        "query_focus": "language requirements IELTS TOEFL minimum score",
    },
    "instruction_language": {
        "id": "instruction_language",
        "label": "language of instruction",
        "subquestion": "official language of instruction for this specific degree program",
        "query_focus": "language of instruction teaching language taught in",
    },
    "language_score_thresholds": {
        "id": "language_score_thresholds",
        "label": "language score thresholds",
        "subquestion": "exact IELTS/TOEFL/CEFR minimum score thresholds",
        "query_focus": "IELTS TOEFL CEFR minimum score thresholds exact values",
    },
    "application_deadline": {
        "id": "application_deadline",
        "label": "application deadlines",
        "subquestion": "application deadline and intake timeline with exact dates",
        "query_focus": "application deadline exact dates intake timeline",
    },
    "application_portal": {
        "id": "application_portal",
        "label": "application portal",
        "subquestion": "official application portal URL and where to apply",
        "query_focus": "official application portal URL where to apply",
    },
    "duration_ects": {
        "id": "duration_ects",
        "label": "duration and ECTS",
        "subquestion": "program duration in semesters/years and total ECTS credits",
        "query_focus": "program duration semesters years total ECTS credits",
    },
    "curriculum_modules": {
        "id": "curriculum_modules",
        "label": "curriculum and modules",
        "subquestion": "curriculum structure and core modules from official regulations",
        "query_focus": "curriculum structure core modules regulations",
    },
    "tuition_fees": {
        "id": "tuition_fees",
        "label": "tuition and fees",
        "subquestion": "tuition fees and semester contribution amounts",
        "query_focus": "tuition fees semester contribution costs",
    },
    "professors_or_supervisors": {
        "id": "professors_or_supervisors",
        "label": "professors or supervisors",
        "subquestion": "official professors, faculty, or supervisors for the requested program",
        "query_focus": "official faculty professors supervisors research group",
    },
    "labs_or_research_groups": {
        "id": "labs_or_research_groups",
        "label": "labs or research groups",
        "subquestion": "official labs, chairs, institutes, or research groups",
        "query_focus": "official labs research groups chairs institutes",
    },
    "contact_information": {
        "id": "contact_information",
        "label": "contact information",
        "subquestion": "official contact information for the program or admissions office",
        "query_focus": "official contact email admissions office program coordinator",
    },
    "visa_or_work_rights": {
        "id": "visa_or_work_rights",
        "label": "visa or work rights",
        "subquestion": "student visa or work rights information from official sources",
        "query_focus": "official student visa work rights residence permit",
    },
    "funding_or_scholarship": {
        "id": "funding_or_scholarship",
        "label": "funding or scholarship",
        "subquestion": "official funding, grants, or scholarship options",
        "query_focus": "official scholarships funding grants international students",
    },
    "admission_decision_signal": {
        "id": "admission_decision_signal",
        "label": "admission competitiveness signal",
        "subquestion": "official selection criteria needed to reason about competitiveness",
        "query_focus": "official selection criteria admission score ranking threshold",
    },
}
_MASTER_LEVEL_TOKENS = {"master", "masters", "msc", "postgraduate", "graduate"}
_BACHELOR_LEVEL_TOKENS = {"bachelor", "bachelors", "bsc", "undergraduate"}
_PROGRAM_FOCUS_STOPWORDS = {
    "about",
    "admission",
    "admissions",
    "and",
    "application",
    "apply",
    "course",
    "deadline",
    "degree",
    "ects",
    "for",
    "gpa",
    "ielts",
    "in",
    "international",
    "language",
    "master",
    "masters",
    "minimum",
    "msc",
    "of",
    "portal",
    "program",
    "programme",
    "requirements",
    "score",
    "students",
    "tell",
    "the",
    "toefl",
    "university",
    "where",
}
_CRAWL_PRIORITY_MARKERS = (
    "admission",
    "apply",
    "application",
    "deadline",
    "eligibility",
    "requirements",
    "language",
    "ielts",
    "toefl",
    "portal",
    "regulation",
    "regulations",
    "statute",
    "statutes",
    "selection",
    "foreign language requirements",
    "module",
    "curriculum",
    "tuition",
    "fees",
    "bewerbung",
    "zulassung",
    "frist",
    "pruefungsordnung",
    "studienordnung",
    "auswahlsatzung",
)
_REQUIRED_FIELD_CRAWL_HINTS: dict[str, tuple[str, ...]] = {
    "admission_requirements": ("admission", "requirements", "eligibility", "prerequisite"),
    "gpa_threshold": ("gpa", "grade", "minimum grade", "score"),
    "ects_breakdown": ("ects", "credits", "credit points", "prerequisite"),
    "instruction_language": (
        "language of instruction",
        "teaching language",
        "unterrichtssprache",
        "taught in",
    ),
    "language_requirements": ("language", "english", "german", "ielts", "toefl"),
    "language_score_thresholds": ("ielts", "toefl", "cefr", "minimum score"),
    "application_deadline": ("deadline", "application period", "frist", "apply by"),
    "application_portal": ("apply online", "application portal", "bewerbungsportal", "portal"),
    "duration_ects": ("duration", "semesters", "ects"),
    "curriculum_modules": ("curriculum", "modules", "study plan", "regulations"),
    "tuition_fees": ("fees", "tuition", "semester contribution"),
    "professors_or_supervisors": ("professor", "faculty", "supervisor", "chair"),
    "labs_or_research_groups": ("lab", "research group", "institute", "chair"),
    "contact_information": ("contact", "email", "admissions office", "coordinator"),
    "visa_or_work_rights": ("visa", "residence permit", "work rights"),
    "funding_or_scholarship": ("scholarship", "funding", "grant"),
    "admission_decision_signal": ("selection criteria", "ranking", "minimum grade"),
}
_REQUIRED_FIELD_SOURCE_ROUTE_HINTS: dict[str, tuple[str, ...]] = {
    "admission_requirements": (
        "official admission requirements eligibility criteria",
        "official selection criteria admission regulations",
        "zulassungsvoraussetzungen master",
    ),
    "gpa_threshold": (
        "selection statute minimum grade admission",
        "admission regulations minimum grade required",
        "auswahlsatzung mindestnote",
    ),
    "ects_breakdown": (
        "selection statute prerequisite ECTS credits",
        "admission regulations required ECTS by subject",
        "auswahlsatzung ects voraussetzungen",
    ),
    "instruction_language": (
        "language of instruction taught in english german",
        "teaching language of the programme",
        "unterrichtssprache master",
    ),
    "language_requirements": (
        "masters foreign language requirements",
        "official english language requirements admissions",
        "sprachnachweis master zulassung",
    ),
    "language_score_thresholds": (
        "IELTS TOEFL minimum score official",
        "CEFR minimum score admissions",
        "language requirements minimum score pdf",
    ),
    "application_deadline": (
        "application deadlines international students official",
        "application period winter semester summer semester",
        "bewerbungsfrist internationale",
    ),
    "application_portal": (
        "apply online application portal official",
        "where to apply admissions portal",
        "bewerbungsportal online antrag",
    ),
    "duration_ects": (
        "program duration semesters total ECTS official",
    ),
    "curriculum_modules": (
        "module handbook study regulations official pdf",
        "study and examination regulations modules",
    ),
    "tuition_fees": (
        "tuition fees semester contribution official",
    ),
    "professors_or_supervisors": (
        "official professors faculty supervisors chairs",
        "official research profile professor chair",
    ),
    "labs_or_research_groups": (
        "official labs research groups chairs institutes",
        "official research group laboratory institute",
    ),
    "contact_information": (
        "official contact program coordinator admissions office email",
        "official student advisory contact email",
    ),
    "visa_or_work_rights": (
        "official student visa work rights residence permit",
        "international students visa work permit official",
    ),
    "funding_or_scholarship": (
        "official scholarships funding grants international students",
        "official financial aid scholarship application",
    ),
    "admission_decision_signal": (
        "official selection criteria ranking admission score",
        "official admission statistics selection threshold",
    ),
}
_REQUIRED_FIELD_PDF_PRIORITY_IDS = {
    "admission_requirements",
    "gpa_threshold",
    "ects_breakdown",
    "language_score_thresholds",
    "application_deadline",
    "curriculum_modules",
}
_REQUIRED_FIELD_QUERY_PRIORITY: dict[str, int] = {
    "application_deadline": 100,
    "application_portal": 95,
    "instruction_language": 94,
    "language_score_thresholds": 92,
    "language_requirements": 90,
    "gpa_threshold": 88,
    "ects_breakdown": 86,
    "admission_requirements": 84,
    "program_overview": 70,
    "duration_ects": 60,
    "curriculum_modules": 58,
    "tuition_fees": 56,
    "professors_or_supervisors": 54,
    "labs_or_research_groups": 53,
    "contact_information": 52,
    "visa_or_work_rights": 51,
    "funding_or_scholarship": 50,
    "admission_decision_signal": 49,
}
_PROGRAM_SCOPE_GENERAL_ROUTE_FIELDS = {
    "program_overview",
    "instruction_language",
    "language_requirements",
    "language_score_thresholds",
    "application_deadline",
    "application_portal",
    "duration_ects",
    "contact_information",
    "visa_or_work_rights",
    "funding_or_scholarship",
}
_PROGRAM_SCOPE_FIELD_ROUTE_MARKERS: dict[str, tuple[str, ...]] = {
    "program_overview": (
        "program overview",
        "master's program in",
        "masters program in",
        "business informatics",
        "wirtschaftsinformatik",
    ),
    "admission_requirements": (
        "admission requirements",
        "eligibility",
        "selection statute",
        "auswahlsatzung",
    ),
    "gpa_threshold": ("minimum grade", "minimum gpa", "grade threshold", "mindestnote"),
    "ects_breakdown": ("ects", "credits", "credit points", "voraussetzungen"),
    "instruction_language": (
        "language of instruction",
        "teaching language",
        "unterrichtssprache",
        "taught in",
    ),
    "language_requirements": (
        "language requirements",
        "foreign language requirements",
        "sprachnachweis",
    ),
    "language_score_thresholds": ("ielts", "toefl", "cefr", "minimum score"),
    "application_deadline": ("application deadline", "deadline", "bewerbungsfrist", "apply by"),
    "application_portal": ("application portal", "apply online", "portal", "bewerbungsportal"),
    "duration_ects": ("4 semester", "4 semesters", "120 ects", "duration", "semesters"),
    "curriculum_modules": ("curriculum", "module handbook", "study regulations", "modules"),
    "tuition_fees": ("tuition", "semester contribution", "fees", "cost"),
    "professors_or_supervisors": ("professor", "faculty", "supervisor", "chair"),
    "labs_or_research_groups": ("lab", "research group", "institute", "chair"),
    "contact_information": ("contact", "email", "admissions office", "coordinator"),
    "visa_or_work_rights": ("visa", "residence permit", "work rights"),
    "funding_or_scholarship": ("scholarship", "funding", "grant"),
    "admission_decision_signal": ("selection criteria", "ranking", "minimum grade"),
}
_PROGRAM_SCOPE_GENERIC_FIELD_IDS = {
    "instruction_language",
    "language_requirements",
    "language_score_thresholds",
    "application_deadline",
    "application_portal",
}
_CONFLICT_SENSITIVE_REQUIRED_FIELDS = {
    "gpa_threshold",
    "application_deadline",
    "tuition_fees",
    "duration_ects",
}
_ADMISSIONS_NOISE_MARKERS = (
    "/going-abroad/",
    "studying-abroad",
    "proof-of-language-proficiency",
    "cooperative-study-program",
    "vocational-training",
    "information-about-the-start-of-the-semester",
    "start-of-the-semester",
    "orientation week",
    "exchange program",
    "/coming-to-mannheim/",
    "exchange-students",
    "engageeu-study-offers",
    "course-catalog",
    "university-wide-electives",
    "elective catalogue",
    "erasmus",
    "incoming students",
    "informationen-fuer-incomings",
    "information-for-incomings",
    "portal2-and-ilias",
    "ilias",
    "outgoing students",
    "welcome week",
    "campus tour",
    "news",
    "mmm_study_organization",
    "mmm study organization",
    "master in management",
    "master thesis registration",
    "thesis registration",
    "course documents such as lecture scripts",
)
_ADMISSIONS_HARD_NOISE_MARKERS = (
    "/going-abroad/",
    "studying-abroad",
    "incoming students",
    "informationen-fuer-incomings",
    "information-for-incomings",
    "portal2-and-ilias",
    "ilias",
    "exchange-students",
    "exchange students",
    "erasmus",
    "mmm_study_organization",
    "mmm study organization",
    "master in management",
    "master thesis registration",
    "thesis registration",
    "course documents such as lecture scripts",
)
_MANDATORY_ADMISSIONS_ROUTE_MARKERS = (
    "selection statute",
    "selection-statutes",
    "auswahlsatzung",
    "zulassungssatzung",
    "admission requirements",
    "language requirements",
    "language of instruction",
    "unterrichtssprache",
    "bewerbungsfrist",
    "application deadline",
    "apply",
    "application portal",
    "bewerbungsportal",
    "portal2",
)
_RESEARCHER_HINT_RE = re.compile(
    r"\b(professor|faculty|supervisor|advisor|lab|research group|department|contact|email|phone|"
    r"scholarship|funding|visa|housing)\b",
    flags=re.IGNORECASE,
)
_RESEARCH_OBJECTIVE_CONTEXT_RE = re.compile(
    r"\b(university|program|course|department|faculty|professor|lab|research|curriculum|"
    r"scholarship|funding|visa|housing|contact)\b",
    flags=re.IGNORECASE,
)
_SEARCH_QUERY_MAX_CHARS = 380
_PLANNER_CACHE_VERSION = "v2"
_RETRIEVAL_MODE_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "web_retrieval_mode",
    default="deep",
)
_RETRIEVAL_QUERY_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "web_retrieval_query",
    default="",
)
_RETRIEVAL_STRICT_OFFICIAL_CTX: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "web_retrieval_strict_official",
    default=False,
)
_RETRIEVAL_TARGET_DOMAINS_CTX: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "web_retrieval_target_domains",
    default=(),
)
_RETRIEVAL_REQUIRED_FIELD_IDS_CTX: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "web_retrieval_required_field_ids",
    default=(),
)
_STANDARD_SEARCH_MODES = {"fast", "standard"}


def _normalized_search_mode(search_mode: str | None) -> str:
    candidate = str(search_mode or "").strip().lower()
    if candidate in {"deep", "fast", "standard"}:
        return candidate
    return "deep"


def _current_search_mode() -> str:
    return _normalized_search_mode(_RETRIEVAL_MODE_CTX.get())


def _current_retrieval_query() -> str:
    return " ".join(str(_RETRIEVAL_QUERY_CTX.get("") or "").split()).strip()


def _is_deep_search_mode(search_mode: str | None = None) -> bool:
    mode = _normalized_search_mode(search_mode) if search_mode is not None else _current_search_mode()
    return mode not in _STANDARD_SEARCH_MODES


def _search_depth_for_mode(search_mode: str | None = None) -> str:
    return "advanced" if _is_deep_search_mode(search_mode) else "basic"


def _deep_mode_int_override(
    attr_name: str,
    configured: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if not _is_deep_search_mode():
        return configured
    override = getattr(settings.web_search, attr_name, configured)
    try:
        candidate = int(override)
    except (TypeError, ValueError):
        candidate = configured
    candidate = max(minimum, min(maximum, candidate))
    return max(configured, candidate)


def _default_num_for_mode(top_k: int) -> int:
    configured = max(1, int(settings.web_search.default_num))
    base = max(top_k, configured)
    return _deep_mode_int_override("deep_default_num", base, minimum=1, maximum=100)


def _max_query_variants_for_mode() -> int:
    configured = max(1, int(settings.web_search.max_query_variants))
    if _is_deep_search_mode():
        return _deep_mode_int_override("deep_max_query_variants", configured, minimum=1, maximum=8)
    # Standard/Fast mode stays lightweight for lower API credit burn.
    return min(2, configured)


def _max_context_results_for_mode() -> int:
    configured = max(1, int(settings.web_search.max_context_results))
    return _deep_mode_int_override("deep_max_context_results", configured, minimum=1, maximum=20)


def _max_pages_to_fetch_for_mode() -> int:
    configured = max(0, int(settings.web_search.max_pages_to_fetch))
    if configured <= 0:
        return 0
    return _deep_mode_int_override("deep_max_pages_to_fetch", configured, minimum=0, maximum=20)


def _max_chunks_per_page_for_mode() -> int:
    configured = max(1, int(settings.web_search.max_chunks_per_page))
    return _deep_mode_int_override("deep_max_chunks_per_page", configured, minimum=1, maximum=20)


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))


async def _redis_call(method, *args, **kwargs):
    """Execute one Redis operation behind the shared Redis dependency limiter."""
    async with dependency_limiter("redis"):
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


def _planner_cache_key(*, model_id: str, query: str, allowed_suffixes: list[str]) -> str:
    payload = {
        "version": _PLANNER_CACHE_VERSION,
        "model_id": model_id,
        "query": " ".join(str(query).split()).strip(),
        "allowed_suffixes": list(allowed_suffixes),
        "max_queries": _max_planner_queries(),
        "max_subquestions": _max_planner_subquestions(),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return app_scoped_key("cache", "web_search", "query_planner", f"sha256:{digest}")


def _gap_planner_cache_key(
    *,
    model_id: str,
    query: str,
    subquestions: list[str],
    facts: list[dict],
    fallback_missing: list[str],
) -> str:
    compact_facts: list[dict[str, str]] = []
    for item in facts[:12]:
        if not isinstance(item, dict):
            continue
        compact_facts.append(
            {
                "fact": " ".join(str(item.get("fact", "")).split())[:180],
                "url": str(item.get("url", "")).strip()[:180],
            }
        )
    payload = {
        "version": _PLANNER_CACHE_VERSION,
        "model_id": model_id,
        "query": " ".join(str(query).split()).strip(),
        "subquestions": _normalize_subquestion_list(
            subquestions,
            limit=max(1, _max_planner_subquestions() or 1),
        ),
        "fallback_missing": _normalize_subquestion_list(
            fallback_missing,
            limit=max(1, _max_planner_subquestions() or 1),
        ),
        "facts": compact_facts,
        "max_gap_queries": max(
            1,
            int(getattr(settings.web_search, "retrieval_loop_max_gap_queries", 2)),
        ),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return app_scoped_key("cache", "web_search", "gap_planner", f"sha256:{digest}")


def _official_domains_for_query(query: str) -> list[str]:
    text = " ".join(str(query).split()).strip().lower()
    if not text:
        return []
    matches = re.findall(r"\b(?:[a-z0-9-]+\.)+(?:de|eu)\b", text)
    domains: list[str] = []
    seen: set[str] = set()
    for domain in matches:
        normalized = str(domain).strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        domains.append(normalized)
    inferred = _inferred_official_domains_from_query(text)
    for domain in inferred:
        if domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)
    return domains


def _official_source_route_domains(query: str, *, max_domains: int = 6) -> list[str]:
    """Return trusted domains worth targeting directly with site: queries."""
    domains: list[str] = []
    seen: set[str] = set()

    def _push(domain: str) -> None:
        normalized = str(domain or "").strip().lower()
        if normalized.startswith("www."):
            normalized = normalized[4:]
        if not normalized or normalized in seen:
            return
        if not re.fullmatch(r"(?:[a-z0-9-]+\.)+(?:de|eu)", normalized):
            return
        seen.add(normalized)
        domains.append(normalized)

    for domain in _official_domains_for_query(query):
        _push(domain)
    for domain in _normalized_official_source_allowlist():
        _push(domain)
    for domain in ("daad.de", "uni-assist.de"):
        _push(domain)
    domains.sort(
        key=lambda domain: (
            1
            if _host_matches_domain(domain, "daad.de")
            or _host_matches_domain(domain, "uni-assist.de")
            else 0,
            len(domain),
            domain,
        )
    )
    return domains[:max(1, max_domains)]


def _official_source_focus_terms(required_fields: list[dict] | None = None) -> list[str]:
    required_ids = {
        str(field.get("id", "")).strip()
        for field in (required_fields or [])
        if isinstance(field, dict) and str(field.get("id", "")).strip()
    }
    focus: list[str] = []
    focus.append("admission requirements eligibility required documents")
    if not required_ids or required_ids & {"admission_requirements", "gpa_threshold", "ects_breakdown"}:
        focus.append("selection statute admission requirements minimum grade ECTS")
    if not required_ids or required_ids & {"application_deadline", "application_portal"}:
        focus.append("application deadline application portal apply online")
    if not required_ids or required_ids & {
        "instruction_language",
        "language_requirements",
        "language_score_thresholds",
    }:
        focus.append("language of instruction language requirements IELTS TOEFL")
    if required_ids & {"duration_ects", "curriculum_modules"}:
        focus.append("module handbook curriculum study regulations")
    if required_ids & {"tuition_fees"}:
        focus.append("tuition fees semester contribution")
    return _normalize_query_list(focus, limit=8)


def _build_official_source_route_queries(
    query: str,
    required_fields: list[dict] | None = None,
    *,
    max_queries: int = 12,
) -> list[str]:
    """Build high-signal Tavily queries for the trusted German higher-ed source set."""
    query_base = _compact_query_keywords(query) or " ".join(str(query).split()).strip()
    if not query_base:
        return []

    domains = _official_source_route_domains(query, max_domains=6)
    primary_domain_count = len(
        [
            domain
            for domain in domains
            if not _host_matches_domain(domain, "daad.de")
            and not _host_matches_domain(domain, "uni-assist.de")
        ]
    )
    if primary_domain_count <= 0 and not _is_university_program_query(query):
        return []
    focus_terms = _official_source_focus_terms(required_fields)
    candidates: list[str] = []
    primary_domains = [
        domain
        for domain in domains
        if not _host_matches_domain(domain, "daad.de")
        and not _host_matches_domain(domain, "uni-assist.de")
    ]
    aggregator_domains = [
        domain
        for domain in domains
        if _host_matches_domain(domain, "daad.de")
        or _host_matches_domain(domain, "uni-assist.de")
    ]

    for domain in primary_domains[:3]:
        candidates.append(f"{query_base} official program page site:{domain}")
        for focus in focus_terms[:4]:
            candidates.append(f"{query_base} official {focus} site:{domain}")
        candidates.append(f"{query_base} auswahlsatzung zulassung filetype:pdf site:{domain}")
        candidates.append(f"{query_base} modulhandbuch prüfungsordnung filetype:pdf site:{domain}")

    for domain in aggregator_domains[:3]:
        if _host_matches_domain(domain, "daad.de"):
            candidates.append(f"{query_base} DAAD international programme site:{domain}")
            candidates.append(f"{query_base} admission requirements DAAD site:{domain}")
        elif _host_matches_domain(domain, "uni-assist.de"):
            candidates.append(f"{query_base} uni-assist application admission site:{domain}")
            candidates.append(f"{query_base} application portal requirements site:{domain}")
        else:
            candidates.append(f"{query_base} official information site:{domain}")

    return _normalize_query_list(candidates, limit=max(1, max_queries))


def _comparison_entities_from_query(query: str) -> list[str]:
    text = " ".join(str(query or "").split()).strip()
    if not text:
        return []
    match = re.search(
        r"(?:compare\s+)?(.+?)\s+(?:vs|versus)\s+(.+?)(?:,| for | including |$)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return []
    candidates = [match.group(1).strip(), match.group(2).strip()]
    entities: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        compact = " ".join(str(candidate).split()).strip()
        key = compact.lower()
        if not compact or key in seen:
            continue
        seen.add(key)
        entities.append(compact[:120])
    return entities[:2]


def _entity_focus_query(entity: str) -> str:
    compact_entity = " ".join(str(entity).split()).strip()
    if not compact_entity:
        return ""
    return (
        f"{compact_entity} data science master's program "
        "admission requirements application deadline"
    )


def _domain_group_key(host: str) -> str:
    normalized = str(host or "").strip().lower()
    if normalized.startswith("www."):
        normalized = normalized[4:]
    if not normalized:
        return ""
    parts = [segment for segment in normalized.split(".") if segment]
    if len(parts) <= 2:
        return normalized
    return ".".join(parts[-2:])


def _replace_german_chars_for_domain(text: str) -> str:
    value = str(text or "").lower()
    return (
        value.replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )


def _ascii_domain_slug(text: str) -> str:
    value = _replace_german_chars_for_domain(text)
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    value = re.sub(r"-{2,}", "-", value)
    return value


def _inferred_official_domains_from_query(text: str) -> list[str]:
    compact = " ".join(str(text or "").split()).strip().lower()
    if not compact:
        return []
    normalized_compact = _replace_german_chars_for_domain(compact)
    technical_context = bool(
        re.search(
            r"\b(technical university|technische universita(?:t|et)|technische hochschule|\btu\b)\b",
            normalized_compact,
        )
    )
    domains: list[str] = []
    seen: set[str] = set()

    def _push(domain: str) -> None:
        candidate = str(domain or "").strip().lower()
        if not candidate or candidate in seen:
            return
        if not re.fullmatch(r"(?:[a-z0-9-]+\.)+(?:de|eu)", candidate):
            return
        seen.add(candidate)
        domains.append(candidate)

    known_acronym_domains = {
        "fau": ("fau.de", "fau.eu"),
        "tum": ("tum.de",),
        "lmu": ("lmu.de",),
        "rwth": ("rwth-aachen.de",),
    }
    tokens = [token for token in re.findall(r"[a-z0-9-]{2,}", compact) if token]
    for token in tokens:
        for domain in known_acronym_domains.get(token, ()):
            _push(domain)
    for phrase, phrase_domains in _KNOWN_QUERY_PHRASE_DOMAIN_HINTS:
        if phrase in normalized_compact:
            for domain in phrase_domains:
                _push(domain)

    pattern = re.compile(
        r"\b(?:university|universit[a-z]*|uni|tu|th|fh)\s+(?:of\s+)?"
        r"([a-z0-9äöüß\-]+(?:\s+[a-z0-9äöüß\-]+){0,2})",
        re.IGNORECASE,
    )
    for match in pattern.finditer(compact):
        raw_name = " ".join(str(match.group(1) or "").split()).strip()
        if not raw_name:
            continue
        filtered_tokens: list[str] = []
        for token in re.findall(r"[a-z0-9äöüß-]+", raw_name.lower()):
            if token in _DOMAIN_INFERENCE_STOPWORDS:
                break
            filtered_tokens.append(token)
        raw_name = " ".join(filtered_tokens).strip()
        if not raw_name:
            continue
        slug = _ascii_domain_slug(raw_name)
        if not slug:
            continue
        slug_candidates = [slug]
        first_token_slug = _ascii_domain_slug(raw_name.split()[0]) if raw_name.split() else ""
        if first_token_slug and first_token_slug != slug:
            slug_candidates.append(first_token_slug)
        alias_tokens = [
            _DOMAIN_SLUG_TOKEN_ALIASES.get(token, token) for token in slug.split("-") if token
        ]
        alias_slug = "-".join(alias_tokens).strip("-")
        if alias_slug and alias_slug != slug:
            slug_candidates.append(alias_slug)
        for slug_candidate in slug_candidates:
            _push(f"uni-{slug_candidate}.de")
            if technical_context:
                _push(f"tu-{slug_candidate}.de")
    if (
        "tum.de" in seen
        and any(phrase in normalized_compact for phrase, _ in _KNOWN_QUERY_PHRASE_DOMAIN_HINTS)
    ):
        for conflict_domain in ("uni-munich.de", "uni-muenchen.de", "lmu.de"):
            if conflict_domain in seen:
                seen.remove(conflict_domain)
                domains = [item for item in domains if item != conflict_domain]
    return domains


async def _read_cache_json(cache_key: str) -> dict | None:
    try:
        raw = await _redis_call(async_redis_client.get, cache_key)
    except RedisError as exc:
        logger.warning("Web-search planner cache read failed. %s", exc)
        return None
    if not raw:
        return None
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def _write_cache_json(cache_key: str, payload: dict, *, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    try:
        await _redis_call(
            async_redis_client.setex,
            cache_key,
            ttl_seconds,
            json.dumps(payload, ensure_ascii=True, sort_keys=True),
        )
    except RedisError as exc:
        logger.warning("Web-search planner cache write failed. %s", exc)


def _extract_published_date(raw_html: str) -> str:
    source = raw_html[:200_000]
    for pattern in _META_PUBLISHED_RE:
        match = pattern.search(source)
        if not match:
            continue
        value = str(match.group(1) or "").strip()
        if value:
            return value[:80]
    match = _TIME_TAG_RE.search(source)
    if match:
        value = str(match.group(1) or "").strip()
        if value:
            return value[:80]
    return ""


def _is_boilerplate_line(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in _BOILERPLATE_LINE_MARKERS)


def _clean_html_text(raw_html: str, max_chars: int) -> str:
    text = _COMMENT_RE.sub(" ", raw_html)
    text = _SCRIPT_STYLE_RE.sub("\n", text)
    if settings.web_search.strip_boilerplate:
        text = _BOILERPLATE_BLOCK_RE.sub("\n", text)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = unescape(text).replace("\xa0", " ")
    min_line_chars = max(0, int(settings.web_search.min_clean_line_chars))

    lines: list[str] = []
    used_chars = 0
    for raw_line in text.splitlines():
        line = _WHITESPACE_RE.sub(" ", raw_line).strip(" |-\t")
        if not line:
            continue
        if min_line_chars and len(line) < min_line_chars:
            continue
        if settings.web_search.strip_boilerplate and _is_boilerplate_line(line):
            continue
        lines.append(line)
        used_chars += len(line) + 1
        if used_chars >= max_chars:
            break
    return "\n".join(lines)[:max_chars]


def _clean_plain_text(raw_text: str, max_chars: int) -> str:
    min_line_chars = max(0, int(settings.web_search.min_clean_line_chars))
    lines: list[str] = []
    used_chars = 0
    for raw_line in str(raw_text).splitlines():
        line = _WHITESPACE_RE.sub(" ", raw_line).strip(" |-\t")
        if not line:
            continue
        if min_line_chars and len(line) < min_line_chars:
            continue
        if settings.web_search.strip_boilerplate and _is_boilerplate_line(line):
            continue
        lines.append(line)
        used_chars += len(line) + 1
        if used_chars >= max_chars:
            break
    return "\n".join(lines)[:max_chars]


def _internal_crawl_enabled() -> bool:
    return _is_deep_search_mode() and bool(
        getattr(settings.web_search, "deep_internal_crawl_enabled", True)
    )


def _internal_crawl_max_depth() -> int:
    configured = int(getattr(settings.web_search, "deep_internal_crawl_max_depth", 2) or 2)
    return max(1, min(4, configured))


def _internal_crawl_max_pages() -> int:
    configured = int(getattr(settings.web_search, "deep_internal_crawl_max_pages", 10) or 10)
    return max(1, min(30, configured))


def _internal_crawl_links_per_page() -> int:
    configured = int(getattr(settings.web_search, "deep_internal_crawl_links_per_page", 10) or 10)
    return max(1, min(30, configured))


def _internal_crawl_per_parent_limit() -> int:
    configured = int(getattr(settings.web_search, "deep_internal_crawl_per_parent_limit", 4) or 4)
    return max(1, min(12, configured))


def _canonical_http_url(url: str, *, base_url: str) -> str:
    candidate = str(url or "").strip()
    if not candidate:
        return ""
    if candidate.startswith("#") or candidate.lower().startswith(("mailto:", "javascript:")):
        return ""
    absolute = urljoin(base_url, candidate)
    absolute, _ = urldefrag(absolute)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return absolute


def _clean_anchor_text(text: str) -> str:
    cleaned = _TAG_RE.sub(" ", str(text or ""))
    cleaned = unescape(cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned[:180]


def _is_admissions_noise_text(text: str) -> bool:
    compact = " ".join(str(text or "").split()).strip().lower()
    if not compact:
        return False
    if any(marker in compact for marker in _ADMISSIONS_HARD_NOISE_MARKERS):
        return True
    if any(marker in compact for marker in _MANDATORY_ADMISSIONS_ROUTE_MARKERS):
        return False
    if "proof-of-language-proficiency" in compact and not any(
        marker in compact for marker in ("exchange", "incoming", "erasmus", "going-abroad")
    ):
        return False
    if "news" in compact and not any(
        marker in compact for marker in ("/news", "/newsroom", "newsroom")
    ):
        return False
    return any(marker in compact for marker in _ADMISSIONS_NOISE_MARKERS)


def _is_mandatory_admissions_route_link(*, url: str, text: str) -> bool:
    haystack = f"{url} {text}".lower()
    if any(marker in haystack for marker in _MANDATORY_ADMISSIONS_ROUTE_MARKERS):
        return True
    return str(url).lower().endswith(".pdf") and any(
        marker in haystack for marker in ("statute", "satzung", "deadline", "bewerbung", "apply")
    )


def _extract_internal_links(
    raw_html: str,
    *,
    base_url: str,
    max_links: int,
) -> list[dict]:
    base_host = _normalized_host(base_url)
    base_group = _domain_group_key(base_host)
    if not base_group:
        return []

    links: list[dict] = []
    seen: set[str] = set()
    admissions_precision = _is_high_precision_admissions_context()
    for href, anchor_html in _ANCHOR_TAG_RE.findall(str(raw_html or "")[:600_000]):
        normalized_url = _canonical_http_url(href, base_url=base_url)
        if not normalized_url:
            continue
        host_group = _domain_group_key(_normalized_host(normalized_url))
        if host_group != base_group:
            continue
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        anchor_text = _clean_anchor_text(anchor_html)
        if admissions_precision and _is_admissions_noise_text(f"{normalized_url} {anchor_text}"):
            continue
        path_lower = str(urlparse(normalized_url).path or "").lower()
        score = 0.0
        if path_lower.endswith(".pdf"):
            score += 1.8
        if any(marker in path_lower for marker in _CRAWL_PRIORITY_MARKERS):
            score += 1.2
        if admissions_precision and _is_mandatory_admissions_route_link(
            url=normalized_url, text=anchor_text
        ):
            score += 2.0
        lowered_anchor = anchor_text.lower()
        if any(marker in lowered_anchor for marker in _CRAWL_PRIORITY_MARKERS):
            score += 1.0
        if re.search(r"\b(master|m\.sc|msc|program|programme|course)\b", lowered_anchor):
            score += 0.5
        if len(anchor_text) < 4:
            score -= 0.2
        links.append(
            {
                "url": normalized_url,
                "text": anchor_text,
                "score": round(score, 4),
            }
        )
    links.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
    return links[: max(1, max_links)]


def _crawl_keyword_set(required_fields: list[dict]) -> set[str]:
    keywords: set[str] = set(_CRAWL_PRIORITY_MARKERS)
    for field in required_fields:
        field_id = str((field or {}).get("id", "")).strip()
        for keyword in _REQUIRED_FIELD_CRAWL_HINTS.get(field_id, ()):
            keywords.add(keyword)
    return {item for item in keywords if item}


def _prioritized_internal_links(
    links: list[dict],
    *,
    required_fields: list[dict],
    per_parent_limit: int,
) -> list[dict]:
    if not links:
        return []
    keywords = _crawl_keyword_set(required_fields)
    admissions_precision = _is_admissions_high_precision_query(
        _current_retrieval_query(),
        required_fields,
    )

    def _priority(item: dict) -> float:
        url = str(item.get("url", "")).strip().lower()
        text = str(item.get("text", "")).strip().lower()
        score = float(item.get("score", 0.0) or 0.0)
        for keyword in keywords:
            if keyword in url:
                score += 0.4
            if keyword in text:
                score += 0.3
        if admissions_precision and _is_mandatory_admissions_route_link(url=url, text=text):
            score += 1.8
        return score

    ranked = sorted(links, key=_priority, reverse=True)
    selected = ranked[: max(1, per_parent_limit)]
    if admissions_precision:
        for item in ranked:
            url = str(item.get("url", "")).strip()
            text = str(item.get("text", "")).strip()
            if not _is_mandatory_admissions_route_link(url=url, text=text):
                continue
            if any(str(existing.get("url", "")).strip() == url for existing in selected):
                continue
            selected.append(item)
            if len(selected) >= max(2, per_parent_limit + 2):
                break
    return selected[: max(1, per_parent_limit + (2 if admissions_precision else 0))]


def _clean_pdf_text(raw_text: str, *, max_chars: int) -> str:
    raw = str(raw_text or "").replace("\u00ad", "")
    if not raw:
        return ""
    raw = re.sub(r"-\s*\n\s*", "", raw)
    lines: list[str] = []
    used_chars = 0
    for raw_line in raw.splitlines():
        line = _WHITESPACE_RE.sub(" ", raw_line).strip(" |\t")
        if not line:
            continue
        # PDFs often break critical data into short lines (e.g., IELTS 6.5, DSH-2).
        if len(line) < 3 and not _NUMERIC_TOKEN_RE.search(line):
            continue
        if settings.web_search.strip_boilerplate and _is_boilerplate_line(line):
            continue
        lines.append(line)
        used_chars += len(line) + 1
        if used_chars >= max_chars:
            break
    return "\n".join(lines)[:max_chars]


def _extract_pdf_text(raw_bytes: bytes, *, max_chars: int) -> str:
    if not raw_bytes or PdfReader is None:
        return ""
    try:
        try:
            reader = PdfReader(io.BytesIO(raw_bytes), strict=False)
        except TypeError:
            reader = PdfReader(io.BytesIO(raw_bytes))
    except Exception:
        return ""

    chunks: list[str] = []
    used_chars = 0
    for page in reader.pages:
        page_text = ""
        try:
            page_text = str(page.extract_text(extraction_mode="layout") or "").strip()
        except TypeError:
            try:
                page_text = str(page.extract_text() or "").strip()
            except Exception:
                page_text = ""
        except Exception:
            page_text = ""
        if not page_text:
            continue
        cleaned = _clean_pdf_text(page_text, max_chars=max_chars)
        if not cleaned:
            continue
        chunks.append(cleaned)
        used_chars += len(cleaned) + 1
        if used_chars >= max_chars:
            break
    return "\n".join(chunks)[:max_chars]


def _fetch_page_data_sync(url: str, timeout_seconds: float, max_chars: int) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "unigraph-web-retrieval/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        content_type = str(response.headers.get("Content-Type", "")).lower()
        max_bytes = max(4_000_000, max_chars * 24)
        raw_bytes = response.read(max_bytes)

    if "application/pdf" in content_type or str(url).lower().endswith(".pdf"):
        return {
            "content": _extract_pdf_text(raw_bytes, max_chars=max_chars),
            "published_date": "",
            "internal_links": [],
        }
    if "text/html" in content_type:
        raw = raw_bytes.decode("utf-8", errors="ignore")
        return {
            "content": _clean_html_text(raw, max_chars=max_chars),
            "published_date": _extract_published_date(raw),
            "internal_links": _extract_internal_links(
                raw,
                base_url=url,
                max_links=_internal_crawl_links_per_page(),
            ),
        }
    if "text/" in content_type:
        raw = raw_bytes.decode("utf-8", errors="ignore")
        return {
            "content": _clean_plain_text(raw, max_chars=max_chars),
            "published_date": "",
            "internal_links": [],
        }

    return {
        "content": "",
        "published_date": "",
        "internal_links": [],
    }


def _row_published_date(row: dict) -> str:
    for key in ("date", "published_date", "published"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    extensions = row.get("extensions", [])
    if isinstance(extensions, list):
        for item in extensions:
            value = str(item).strip()
            if value and _DATE_LIKE_RE.search(value):
                return value[:80]
    return ""


def _organic_rows(payload: dict, limit: int) -> list[dict]:
    rows = payload.get("organic_results", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    results: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "")).strip()
        link = str(row.get("link", "")).strip()
        snippet = str(row.get("snippet", "")).strip()
        if not (title or link or snippet):
            continue
        results.append(
            {
                "title": title,
                "url": link,
                "snippet": snippet,
                "published_date": _row_published_date(row),
            }
        )
        if len(results) >= limit:
            break
    return results


def _ai_overview_scalar_parts(ai: dict) -> list[str]:
    parts: list[str] = []
    for key in ("title", "text", "snippet", "description"):
        value = ai.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return parts


def _ai_overview_list_item_text(item) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    title = str(item.get("title", "")).strip()
    snippet = str(item.get("snippet", "")).strip()
    return f"{title}: {snippet}".strip(": ")


def _ai_overview_list_parts(items) -> list[str]:
    if not isinstance(items, list):
        return []
    parts: list[str] = []
    for item in items:
        text = _ai_overview_list_item_text(item)
        if text:
            parts.append(text)
    return parts


def _ai_overview_text(payload: dict) -> str:
    ai = payload.get("ai_overview", {}) if isinstance(payload, dict) else {}
    if not isinstance(ai, dict):
        return ""
    parts = _ai_overview_scalar_parts(ai)
    parts.extend(_ai_overview_list_parts(ai.get("list", [])))
    return " ".join(parts).strip()


async def _afetch_page_data(url: str) -> dict:
    async with dependency_limiter("web_search"):
        return await asyncio.to_thread(
            _fetch_page_data_sync,
            url,
            float(settings.web_search.page_fetch_timeout_seconds),
            int(settings.web_search.max_page_chars),
        )


async def _afetch_organic_pages(
    rows: list[dict], *, max_pages_to_fetch: int | None = None
) -> dict[str, dict]:
    if not settings.web_search.fetch_page_content:
        return {}

    targets = [row for row in rows if row.get("url")]
    page_limit = (
        max(0, int(max_pages_to_fetch))
        if max_pages_to_fetch is not None
        else _max_pages_to_fetch_for_mode()
    )
    targets = targets[:page_limit]
    if not targets:
        return {}

    queue: asyncio.Queue = asyncio.Queue(maxsize=settings.web_search.queue_max_size)
    worker_count = min(settings.web_search.queue_workers, len(targets))
    fetched: dict[str, dict] = {}

    async def _worker():
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                url = str(item).strip()
                if not url:
                    continue
                try:
                    fetched[url] = await _afetch_page_data(url)
                except Exception:
                    fetched[url] = {"content": "", "published_date": ""}
            finally:
                queue.task_done()

    workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]
    for row in targets:
        await queue.put(row["url"])
    for _ in range(worker_count):
        await queue.put(None)

    await queue.join()
    await asyncio.gather(*workers)
    return fetched


def _extract_urls_from_rows(
    rows: list[dict],
    *,
    limit: int,
    allowed_suffixes: list[str],
    strict_official: bool,
    target_domain_groups: list[str] | None,
    enforce_target_domain_scope: bool,
) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url", "")).strip()
        title = str(row.get("title", "")).strip()
        snippet = str(row.get("snippet", "")).strip()
        if not url:
            continue
        if not _source_url_allowed(
            url=url,
            title=title,
            snippet=snippet,
            allowed_suffixes=allowed_suffixes,
            strict_official=strict_official,
        ):
            continue
        if enforce_target_domain_scope and not _url_matches_target_domain_scope(url, target_domain_groups):
            continue
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
        if len(urls) >= max(1, limit):
            break
    return urls


async def _atry_tavily_extract_rows(
    rows: list[dict],
    *,
    query: str,
    allowed_suffixes: list[str],
    strict_official: bool,
    target_domain_groups: list[str] | None,
    enforce_target_domain_scope: bool,
    max_urls: int = 8,
) -> dict[str, dict]:
    if not rows:
        return {}
    if not _is_deep_search_mode():
        return {}
    urls = _extract_urls_from_rows(
        rows,
        limit=max_urls,
        allowed_suffixes=allowed_suffixes,
        strict_official=strict_official,
        target_domain_groups=target_domain_groups,
        enforce_target_domain_scope=enforce_target_domain_scope,
    )
    if not urls:
        return {}
    try:
        payload = await aextract_urls(
            urls,
            extract_depth=_search_depth_for_mode(),
            query=query,
        )
    except Exception as exc:
        logger.warning("Tavily extract failed; continuing with direct page fetch. %s", exc)
        return {}
    results = payload.get("results", []) if isinstance(payload, dict) else []
    if not isinstance(results, list):
        return {}
    extracted: dict[str, dict] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        raw_content = str(item.get("raw_content", "")).strip()
        content = str(item.get("content", "")).strip()
        text = raw_content or content
        if not url or not text:
            continue
        extracted[url] = {
            "content": _clean_plain_text(text, max_chars=int(settings.web_search.max_page_chars)),
            "published_date": str(item.get("published_date", "")).strip(),
            "internal_links": [],
        }
    return extracted


def _should_run_extract_for_step(
    *,
    rows: list[dict],
    page_data_by_url: dict[str, dict],
    step: int,
    missing_required_fields: list[dict],
    missing_research_objectives: list[dict],
) -> bool:
    if not _is_deep_search_mode():
        return False
    if not rows:
        return False
    has_pdf_like_url = any(
        ".pdf" in str(row.get("url", "")).strip().lower()
        for row in rows
        if isinstance(row, dict)
    )
    if has_pdf_like_url:
        return True
    fetch_hits = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url", "")).strip()
        payload = page_data_by_url.get(url)
        if not isinstance(payload, dict):
            continue
        content = " ".join(str(payload.get("content", "")).split()).strip()
        if len(content) >= 120:
            fetch_hits += 1
    weak_fetch_coverage = fetch_hits < max(1, min(3, len(rows) // 2))
    if weak_fetch_coverage:
        return True
    if step <= 2 and (missing_required_fields or missing_research_objectives):
        return True
    return False


def _crawl_row_for_url(*, url: str, anchor_text: str, parent_url: str) -> dict:
    host_label = _host_label(url)
    title = anchor_text or host_label
    snippet = (
        "Internal official page discovered from "
        f"{parent_url}. Prioritize admission requirements, language, deadlines, "
        "application portal, and regulations."
    )
    return {
        "title": title[:160],
        "url": url,
        "snippet": snippet[:320],
        "published_date": "",
    }


async def _acrawl_internal_pages(
    *,
    seed_rows: list[dict],
    seed_page_data_by_url: dict[str, dict],
    required_fields: list[dict],
    allowed_suffixes: list[str],
    target_domain_groups: list[str] | None,
    enforce_target_domain_scope: bool,
) -> tuple[list[dict], dict[str, dict], dict]:
    if not _internal_crawl_enabled():
        return [], {}, {
            "enabled": False,
            "pages_fetched": 0,
            "discovered_urls": 0,
            "depth_reached": 0,
        }
    if not seed_rows:
        return [], {}, {
            "enabled": True,
            "pages_fetched": 0,
            "discovered_urls": 0,
            "depth_reached": 0,
        }

    max_depth = _internal_crawl_max_depth()
    max_pages = _internal_crawl_max_pages()
    per_parent_limit = _internal_crawl_per_parent_limit()
    max_links = _internal_crawl_links_per_page()
    visited_urls = {
        str(row.get("url", "")).strip()
        for row in seed_rows
        if isinstance(row, dict) and str(row.get("url", "")).strip()
    }
    discovered_rows: list[dict] = []
    discovered_page_data: dict[str, dict] = {}
    current_layer: list[str] = sorted(visited_urls)
    total_discovered = 0
    depth_reached = 0

    for depth in range(1, max_depth + 1):
        if not current_layer or len(discovered_rows) >= max_pages:
            break
        next_rows: list[dict] = []
        for parent_url in current_layer:
            parent_payload = seed_page_data_by_url.get(parent_url) or discovered_page_data.get(parent_url)
            parent_payload = parent_payload if isinstance(parent_payload, dict) else {}
            internal_links = parent_payload.get("internal_links")
            internal_links = internal_links if isinstance(internal_links, list) else []
            if not internal_links:
                continue
            ranked_links = _prioritized_internal_links(
                internal_links[:max_links],
                required_fields=required_fields,
                per_parent_limit=per_parent_limit,
            )
            for link in ranked_links:
                url = str(link.get("url", "")).strip()
                if not url or url in visited_urls:
                    continue
                if allowed_suffixes and not _url_matches_allowed_suffix(url, allowed_suffixes):
                    continue
                if enforce_target_domain_scope and not _url_matches_target_domain_scope(
                    url, target_domain_groups
                ):
                    continue
                visited_urls.add(url)
                next_rows.append(
                    _crawl_row_for_url(
                        url=url,
                        anchor_text=str(link.get("text", "")).strip(),
                        parent_url=parent_url,
                    )
                )
                if len(discovered_rows) + len(next_rows) >= max_pages:
                    break
            if len(discovered_rows) + len(next_rows) >= max_pages:
                break

        if not next_rows:
            break
        fetch_rows = _dedupe_rows(
            next_rows,
            limit=max_pages - len(discovered_rows),
        )
        if not fetch_rows:
            break
        fetched = await _afetch_organic_pages(fetch_rows)
        usable_rows: list[dict] = []
        for row in fetch_rows:
            url = str(row.get("url", "")).strip()
            payload = fetched.get(url)
            payload = payload if isinstance(payload, dict) else {}
            content = " ".join(str(payload.get("content", "")).split()).strip()
            if not content:
                continue
            discovered_page_data[url] = payload
            usable_rows.append(row)
        if not usable_rows:
            break
        discovered_rows.extend(usable_rows)
        total_discovered += len(usable_rows)
        current_layer = [
            str(item.get("url", "")).strip()
            for item in usable_rows
            if str(item.get("url", "")).strip()
        ]
        depth_reached = depth

    return discovered_rows, discovered_page_data, {
        "enabled": True,
        "pages_fetched": len(discovered_page_data),
        "discovered_urls": total_discovered,
        "depth_reached": depth_reached,
    }


def _host_label(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or "web"


def _compact_query_keywords(query: str) -> str:
    tokens = _QUERY_TOKEN_RE.findall(query.lower())
    if not tokens:
        return ""
    compact: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if len(token) <= 2 or token in _QUERY_STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        compact.append(token)
        if len(compact) >= 8:
            break
    return " ".join(compact).strip()


def _normalized_allowed_domain_suffixes() -> list[str]:
    raw_values = getattr(settings.web_search, "allowed_domain_suffixes", [".de", ".eu"])
    if raw_values is None:
        raw_values = [".de", ".eu"]
    if isinstance(raw_values, str):
        values = [item.strip() for item in raw_values.split(",") if item.strip()]
    elif isinstance(raw_values, list):
        values = [str(item).strip() for item in raw_values if str(item).strip()]
    else:
        values = [".de", ".eu"]
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        suffix = value.lower()
        if not suffix.startswith("."):
            suffix = f".{suffix}"
        if suffix in seen:
            continue
        seen.add(suffix)
        normalized.append(suffix)
    return normalized


def _normalized_official_source_allowlist() -> list[str]:
    raw_values = getattr(settings.web_search, "official_source_allowlist", [])
    if isinstance(raw_values, str):
        values = [item.strip().lower() for item in raw_values.split(",") if item.strip()]
    elif isinstance(raw_values, list):
        values = [str(item).strip().lower() for item in raw_values if str(item).strip()]
    else:
        values = []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        domain = value[4:] if value.startswith("www.") else value
        if domain in seen:
            continue
        seen.add(domain)
        normalized.append(domain)
    return normalized


def _host_matches_domain(host: str, domain: str) -> bool:
    normalized_host = _normalized_host(host)
    if not normalized_host:
        normalized_host = str(host or "").strip().lower()
        if normalized_host.startswith("www."):
            normalized_host = normalized_host[4:]
    normalized_domain = _normalized_host(domain)
    if not normalized_domain:
        normalized_domain = str(domain or "").strip().lower()
        if normalized_domain.startswith("www."):
            normalized_domain = normalized_domain[4:]
    if not normalized_host or not normalized_domain:
        return False
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def _host_is_official_allowlisted(host: str) -> bool:
    normalized_host = _normalized_host(host)
    if not normalized_host:
        return False
    return any(
        _host_matches_domain(normalized_host, domain)
        for domain in _normalized_official_source_allowlist()
    )


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = str(text).lower()
    return any(marker in lowered for marker in markers)


def _host_looks_non_official(host: str) -> bool:
    grouped = _domain_group_key(host)
    if not grouped:
        return True
    host_text = grouped.replace("-", " ")
    return any(marker in host_text for marker in _NON_OFFICIAL_HOST_MARKERS)


def _host_looks_official_institution(host: str) -> bool:
    grouped = _domain_group_key(host)
    if not grouped:
        return False

    # Check if in official German education domains (DAAD, hochschulkompass, etc.)
    if host in _GERMAN_OFFICIAL_EDUCATION_DOMAINS or any(
        host.endswith(f".{domain}") for domain in _GERMAN_OFFICIAL_EDUCATION_DOMAINS
    ):
        return True

    host_text = grouped.replace("-", " ")
    if any(marker in host_text for marker in _OFFICIAL_SOURCE_HOST_MARKERS):
        return True
    labels = [label for label in grouped.split(".") if label]
    return any(label.startswith(_OFFICIAL_SOURCE_HOST_PREFIXES) for label in labels)


def _host_is_acronym_like(host: str) -> bool:
    grouped = _domain_group_key(host)
    if not grouped:
        return False
    labels = [label for label in grouped.split(".") if label]
    if not labels:
        return False
    root = labels[0]
    if not root or not root.isalpha():
        return False
    if root in _ACRONYM_LIKE_HOST_BLOCKLIST:
        return False
    return 2 <= len(root) <= 6


def _required_field_ids(required_fields: list[dict]) -> set[str]:
    ids: set[str] = set()
    for field in required_fields:
        field_id = str((field or {}).get("id", "")).strip()
        if field_id:
            ids.add(field_id)
    return ids


def _is_admissions_high_precision_query(query: str, required_fields: list[dict] | None = None) -> bool:
    ids = _required_field_ids(required_fields or [])
    if ids & _ADMISSIONS_HIGH_PRECISION_FIELD_IDS:
        return True
    compact = " ".join(str(query or "").split()).strip().lower()
    if not compact:
        return False
    has_program_context = bool(
        re.search(r"\b(university|uni|master|m\.sc|msc|program|programme|course|admission)\b", compact)
    )
    if not has_program_context:
        return False
    return bool(
        re.search(
            r"\b(requirements?|eligibility|ielts|toefl|cefr|international students?|deadline|"
            r"ects|credits?|gpa|grade|language of instruction|taught in|unterrichtssprache)\b",
            compact,
        )
    )


def _is_university_program_query(query: str) -> bool:
    compact = " ".join(str(query or "").split()).strip().lower()
    if not compact:
        return False
    has_institution = bool(
        re.search(
            r"\b(university|universit[a-z]*|uni|technical university|technische universita[et]|tu)\b",
            compact,
        )
    )
    if not has_institution:
        return False
    return bool(re.search(r"\b(master|masters|m\.sc|msc|program|programme|course|degree)\b", compact))


def _target_domain_groups_for_query(query: str) -> list[str]:
    groups: list[str] = []
    seen: set[str] = set()
    for domain in _official_domains_for_query(query):
        group = _domain_group_key(domain)
        if not group or group in seen:
            continue
        seen.add(group)
        groups.append(group)
    return groups


def _filter_rows_by_target_domain_groups(
    rows: list[dict],
    *,
    target_groups: list[str],
    allow_fallback_on_empty: bool = True,
) -> list[dict]:
    if not target_groups:
        return rows
    target_set = {str(item).strip().lower() for item in target_groups if str(item).strip()}
    if not target_set:
        return rows
    allowlist_groups = {
        _domain_group_key(domain)
        for domain in _normalized_official_source_allowlist()
        if _domain_group_key(domain)
    }
    filtered: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url", "")).strip()
        group = _domain_group_key(_normalized_host(url))
        if not group:
            continue
        if group in target_set or group in allowlist_groups:
            filtered.append(row)
    if filtered:
        return filtered
    return rows if allow_fallback_on_empty else []


def _url_matches_target_domain_scope(url: str, target_groups: list[str] | None) -> bool:
    if not target_groups:
        return True
    target_set = {str(item).strip().lower() for item in target_groups if str(item).strip()}
    if not target_set:
        return True
    allowlist_groups = {
        _domain_group_key(domain)
        for domain in _normalized_official_source_allowlist()
        if _domain_group_key(domain)
    }
    group = _domain_group_key(_normalized_host(url))
    if not group:
        return False
    return group in target_set or group in allowlist_groups


def _source_url_rejection_reason(
    *,
    url: str,
    title: str,
    snippet: str,
    allowed_suffixes: list[str],
    strict_official: bool = False,
) -> str:
    if not _url_matches_allowed_suffix(url, allowed_suffixes):
        return "domain_suffix_not_allowed"
    if not bool(getattr(settings.web_search, "official_source_filter_enabled", True)) and not strict_official:
        return ""
    host = _normalized_host(url)
    if not host:
        return "missing_or_invalid_host"

    allowlist = _normalized_official_source_allowlist()
    if any(_host_matches_domain(host, domain) for domain in allowlist):
        return ""

    if _host_looks_non_official(host):
        return "non_official_host"
    if _host_looks_official_institution(host):
        return ""

    evidence_text = f"{title} {snippet}"
    if strict_official:
        if not _host_is_acronym_like(host):
            return "strict_official_host_not_recognized"
        if not _contains_marker(evidence_text, _OFFICIAL_SOURCE_TEXT_MARKERS):
            return "strict_official_missing_official_marker"
        if not _contains_marker(evidence_text, _ACADEMIC_PAGE_MARKERS):
            return "strict_official_missing_academic_marker"
        return ""

    if not _contains_marker(evidence_text, _OFFICIAL_SOURCE_TEXT_MARKERS):
        return "missing_official_marker"
    if not _contains_marker(evidence_text, _ACADEMIC_PAGE_MARKERS):
        return "missing_academic_marker"
    return ""


def _source_url_allowed(
    *,
    url: str,
    title: str,
    snippet: str,
    allowed_suffixes: list[str],
    strict_official: bool = False,
) -> bool:
    return not _source_url_rejection_reason(
        url=url,
        title=title,
        snippet=snippet,
        allowed_suffixes=allowed_suffixes,
        strict_official=strict_official,
    )


def _source_filter_decisions(
    rows: list[dict],
    *,
    allowed_suffixes: list[str],
    strict_official: bool,
) -> list[dict]:
    decisions: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url", "")).strip()
        title = str(row.get("title", "")).strip()
        snippet = str(row.get("snippet", "")).strip()
        reason = _source_url_rejection_reason(
            url=url,
            title=title,
            snippet=snippet,
            allowed_suffixes=allowed_suffixes,
            strict_official=strict_official,
        )
        decisions.append(
            {
                "url": url,
                "host": _normalized_host(url),
                "kept": not bool(reason),
                "reason": reason or "kept",
            }
        )
    return decisions


def _source_filter_summary(decisions: list[dict]) -> dict:
    reason_counts: dict[str, int] = {}
    kept_count = 0
    rejected_count = 0
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        reason = str(decision.get("reason", "")).strip() or "unknown"
        if bool(decision.get("kept", False)):
            kept_count += 1
        else:
            rejected_count += 1
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "candidate_count": len(decisions),
        "kept_count": kept_count,
        "rejected_count": rejected_count,
        "reason_counts": reason_counts,
        "sample": decisions[:10],
    }


def _build_query_variants(query: str, allowed_suffixes: list[str]) -> list[str]:
    base = " ".join(str(query).split()).strip()
    if not base:
        return []

    if not settings.web_search.multi_query_enabled:
        return [base]

    if not _is_deep_search_mode():
        official_domains = _official_domains_for_query(base)[:1]
        candidates = [base]
        if official_domains:
            candidates.append(f"{base} site:{official_domains[0]}")
        else:
            candidates.append(f"{base} official information")

        max_variants = _max_query_variants_for_mode()
        variants: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = " ".join(str(candidate).split()).strip()
            key = normalized.lower()
            if not normalized or key in seen:
                continue
            seen.add(key)
            variants.append(normalized)
            if len(variants) >= max_variants:
                break
        return variants or [base]

    official_domains = _official_domains_for_query(base)[:3]
    official_route_queries = _build_official_source_route_queries(
        base,
        _required_fields_from_query(base),
        max_queries=8,
    )
    comparison_entities = _comparison_entities_from_query(base)
    candidates: list[str] = []
    if not comparison_entities:
        candidates.extend(official_route_queries[:4])
    candidates.append(base)
    if allowed_suffixes:
        site_terms = " OR ".join(f"site:{suffix}" for suffix in allowed_suffixes[:2])
        candidates.append(f"{base} ({site_terms})")
    for entity in comparison_entities:
        entity_focus = _entity_focus_query(entity)
        if not entity_focus:
            continue
        candidates.append(entity_focus)
        for domain in _official_domains_for_query(entity)[:1]:
            candidates.append(f"{entity_focus} site:{domain}")
    if comparison_entities:
        candidates.extend(official_route_queries[:4])
    for domain in official_domains:
        candidates.append(f"{base} site:{domain}")
    candidates.append(f"{base} official information")
    compact = _compact_query_keywords(base)
    if compact and compact != base.lower():
        candidates.append(compact)

    if _DEADLINE_HINT_RE.search(base):
        candidates.append(f"{base} application deadline official")
        # German-specific deadline queries
        if getattr(settings.web_search, "german_university_mode_enabled", True):
            candidates.append(f"{base} bewerbungsfrist")
            candidates.append(f"{base} deadline wintersemester sommersemester")
            candidates.append(f"{base} bewerbungsfrist filetype:pdf")
    if _REQUIREMENTS_HINT_RE.search(base):
        candidates.append(f"{base} admission requirements official")
        # German-specific requirements queries
        if getattr(settings.web_search, "german_university_mode_enabled", True):
            candidates.append(f"{base} zulassungsvoraussetzungen")
            candidates.append(f"{base} auswahlsatzung filetype:pdf")
            candidates.append(f"{base} mindestnote ects")
    # Language requirements enhancement
    if _LANGUAGE_HINT_RE.search(base):
        candidates.append(f"{base} ielts toefl minimum score")
        if getattr(settings.web_search, "german_university_mode_enabled", True):
            candidates.append(f"{base} sprachnachweis englisch")
    # Curriculum/modules enhancement
    if _CURRICULUM_HINT_RE.search(base):
        if getattr(settings.web_search, "german_university_mode_enabled", True):
            candidates.append(f"{base} modulhandbuch filetype:pdf")
            candidates.append(f"{base} prüfungsordnung filetype:pdf")

    # Aggressive PDF targeting for complete admissions info
    if getattr(settings.web_search, "german_university_mode_enabled", True):
        # If query mentions specific university + program, add PDF-specific variants
        if any(term in base.lower() for term in ["mannheim", "tum", "rwth", "tu ", "university"]):
            if any(term in base.lower() for term in ["master", "msc", "m.sc"]):
                # Target admission statute PDFs directly
                candidates.append(f"{base} auswahlsatzung filetype:pdf")
                candidates.append(f"{base} admission statute pdf")
                # Target official portals with complete info
                for domain in official_domains[:2]:
                    candidates.append(f"{base} admission requirements site:{domain}")
                    candidates.append(f"{base} language requirements site:{domain}")

    for domain in official_domains:
        if compact:
            candidates.append(f"{compact} site:{domain}")
    candidates.extend(official_route_queries[4:])

    max_variants = _max_query_variants_for_mode()
    variants: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = " ".join(str(candidate).split()).strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        variants.append(normalized)
        if len(variants) >= max_variants:
            break
    return variants or [base]


def _normalize_query_list(values, *, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    fuzzy_seen: set[str] = set()
    for value in values:
        candidate = " ".join(str(value).split()).strip()
        if len(candidate) > _SEARCH_QUERY_MAX_CHARS:
            clipped = candidate[:_SEARCH_QUERY_MAX_CHARS]
            candidate = clipped.rsplit(" ", 1)[0].strip() or clipped
        if not candidate:
            continue
        key = candidate.lower()
        if key in seen:
            continue
        fuzzy_key = re.sub(r"\bsite:[a-z0-9.-]+\b", " ", key)
        fuzzy_key = re.sub(r"[^a-z0-9\s]", " ", fuzzy_key)
        fuzzy_key = " ".join(
            token
            for token in fuzzy_key.split()
            if token and token not in _QUERY_STOPWORDS
        )
        if fuzzy_key and fuzzy_key in fuzzy_seen:
            continue
        seen.add(key)
        if fuzzy_key:
            fuzzy_seen.add(fuzzy_key)
        normalized.append(candidate)
        if len(normalized) >= limit:
            break
    return normalized


def _normalize_subquestion_list(values, *, limit: int) -> list[str]:
    return _normalize_query_list(values, limit=limit)


def _planner_model_candidates() -> list[str]:
    configured = str(getattr(settings.web_search, "query_planner_model_id", "")).strip()
    primary = str(settings.bedrock.primary_model_id).strip()
    fallback = str(settings.bedrock.fallback_model_id).strip()
    candidates = [configured, primary, fallback]
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = str(candidate).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _retrieval_loop_model_candidates() -> list[str]:
    configured = str(getattr(settings.web_search, "retrieval_loop_model_id", "")).strip()
    if configured:
        candidates = [configured] + _planner_model_candidates()
    else:
        candidates = _planner_model_candidates()
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = str(candidate).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _max_planner_queries() -> int:
    return max(1, int(getattr(settings.web_search, "query_planner_max_queries", 5)))


def _max_planner_subquestions() -> int:
    return max(0, int(getattr(settings.web_search, "query_planner_max_subquestions", 4)))


def _planner_query_limit_for_query(query: str) -> int:
    base = _max_planner_queries()
    if not _is_deep_search_mode():
        return base
    required_fields = _required_fields_from_query(query)
    required_fields_count = len(required_fields)
    focus_count = len(_coverage_subquestions_from_query(query))
    boost = min(5, max(required_fields_count, focus_count))
    limit = min(12, max(base, base + boost))
    if _is_admissions_high_precision_query(query, required_fields):
        limit = min(14, max(limit, base + 4))
    return limit


async def _call_planner_model_text(
    *,
    model_id: str,
    messages: list[dict],
    acquire_timeout_seconds: float,
) -> str:
    from app.infra.bedrock_chat_client import client

    response = await client.chat.completions.create(
        model=model_id,
        messages=messages,
        limiter_name="llm_planner",
        limiter_acquire_timeout_seconds=acquire_timeout_seconds,
        rate_limit_profile="planner",
    )
    if not response or not getattr(response, "choices", None):
        return ""
    return str(response.choices[0].message.content or "").strip()


def _is_student_qa_query(query: str) -> bool:
    compact = " ".join(str(query or "").split()).strip().lower()
    if not compact:
        return False
    if _is_university_program_query(compact):
        return True
    has_university_context = bool(
        re.search(
            r"\b(university|universit[a-z]*|uni\b|hochschule|college|program(?:me)?|course|degree)\b",
            compact,
        )
    )
    has_student_topic = bool(
        re.search(
            r"\b(admission|eligibility|requirements?|deadline|apply|application|portal|"
            r"ielts|toefl|testdaf|dsh|cefr|ects|gpa|grade|tuition|fees?|scholarship|"
            r"funding|visa|aps|professor|supervisor|lab|research group|contact)\b",
            compact,
        )
    )
    return has_university_context and has_student_topic


def _schema_required_fields_from_query(query: str) -> list[dict]:
    if not _is_student_qa_query(query):
        return []
    try:
        schema = resolve_question_schema(query)
    except Exception as exc:
        logger.warning("Student-QA schema resolution failed; using legacy fields. %s", exc)
        return []
    slots = schema.get("required_slots", [])
    if not isinstance(slots, list) or not slots:
        return []
    normalized: list[dict] = []
    seen: set[str] = set()
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        slot_id = str(slot.get("slot_id", "")).strip()
        retrieval_id = _STUDENT_SLOT_TO_RETRIEVAL_FIELD_ID.get(slot_id, slot_id)
        if not retrieval_id or retrieval_id in seen:
            continue
        catalog_row = _RETRIEVAL_FIELD_CATALOG.get(retrieval_id)
        if not catalog_row:
            continue
        seen.add(retrieval_id)
        row = dict(catalog_row)
        row["schema_slot_id"] = slot_id
        row["source_tier_requirement"] = str(
            slot.get("source_tier_requirement", row.get("source_tier_requirement", "tier0_official"))
        ).strip() or "tier0_official"
        row["criticality"] = str(slot.get("criticality", row.get("criticality", ""))).strip()
        row["critical"] = bool(slot.get("critical", False))
        row["freshness_rule_days"] = int(slot.get("freshness_rule_days", 0) or 0)
        row["conflict_rule"] = str(slot.get("conflict_rule", "")).strip()
        row["label"] = str(slot.get("label", row.get("label", retrieval_id))).strip() or retrieval_id
        normalized.append(row)
    if normalized:
        emit_trace_event(
            "schema_resolved",
            {
                "schema_id": str(schema.get("schema_id", "student_general")),
                "slot_ids": [
                    str(slot.get("slot_id", "")).strip()
                    for slot in slots
                    if isinstance(slot, dict)
                ],
                "retrieval_field_ids": [str(row.get("id", "")).strip() for row in normalized],
            },
        )
    return normalized


def _required_fields_from_query(query: str) -> list[dict]:
    schema_fields = _schema_required_fields_from_query(query)
    if schema_fields:
        return schema_fields
    compact = " ".join(str(query or "").split()).strip().lower()
    if not compact:
        return []
    field_catalog = {
        "program_overview": {
            "id": "program_overview",
            "label": "program overview",
            "subquestion": "program overview, degree type, department, and teaching language",
            "query_focus": "official program overview degree language",
        },
        "admission_requirements": {
            "id": "admission_requirements",
            "label": "course requirements",
            "subquestion": "course requirements, eligibility criteria, and required documents",
            "query_focus": "admission requirements eligibility required documents",
        },
        "gpa_threshold": {
            "id": "gpa_threshold",
            "label": "GPA/grade threshold",
            "subquestion": "minimum GPA/grade threshold and grading scale details",
            "query_focus": "minimum GPA grade threshold admission score requirement",
        },
        "ects_breakdown": {
            "id": "ects_breakdown",
            "label": "ECTS/prerequisite credits",
            "subquestion": "required ECTS or prerequisite credit breakdown by subject area",
            "query_focus": "required ECTS prerequisite credits mathematics computer science",
        },
        "language_requirements": {
            "id": "language_requirements",
            "label": "language requirements",
            "subquestion": "language requirements with accepted tests and minimum scores",
            "query_focus": "language requirements IELTS TOEFL minimum score",
        },
        "instruction_language": {
            "id": "instruction_language",
            "label": "language of instruction",
            "subquestion": "official language of instruction for this specific degree program",
            "query_focus": "language of instruction teaching language taught in",
        },
        "language_score_thresholds": {
            "id": "language_score_thresholds",
            "label": "language score thresholds",
            "subquestion": "exact IELTS/TOEFL/CEFR minimum score thresholds",
            "query_focus": "IELTS TOEFL CEFR minimum score thresholds exact values",
        },
        "application_deadline": {
            "id": "application_deadline",
            "label": "application deadlines",
            "subquestion": "application deadline and intake timeline with exact dates",
            "query_focus": "application deadline exact dates intake timeline",
        },
        "application_portal": {
            "id": "application_portal",
            "label": "application portal",
            "subquestion": "official application portal URL and where to apply",
            "query_focus": "official application portal URL where to apply",
        },
        "duration_ects": {
            "id": "duration_ects",
            "label": "duration and ECTS",
            "subquestion": "program duration in semesters/years and total ECTS credits",
            "query_focus": "program duration semesters years total ECTS credits",
        },
        "curriculum_modules": {
            "id": "curriculum_modules",
            "label": "curriculum and modules",
            "subquestion": "curriculum structure and core modules from official regulations",
            "query_focus": "curriculum structure core modules regulations",
        },
        "tuition_fees": {
            "id": "tuition_fees",
            "label": "tuition and fees",
            "subquestion": "tuition fees and semester contribution amounts",
            "query_focus": "tuition fees semester contribution costs",
        },
    }
    selected_ids: list[str] = []
    explicit_admission_scope = bool(
        re.search(
            r"\b(admission requirements?|eligibility|entry|documents?|course requirements?)\b",
            compact,
        )
    )
    has_grade_or_credit_scope = bool(
        re.search(r"\b(gpa|grade|cgpa|ects|credit|credits|prerequisite)\b", compact)
    )
    language_only_requirements = bool(_LANGUAGE_HINT_RE.search(compact)) and (
        not explicit_admission_scope and not has_grade_or_credit_scope
    )
    has_program_context = bool(re.search(r"\b(master|m\.sc|msc|program|course|study|degree)\b", compact))
    if _REQUIREMENTS_HINT_RE.search(compact) and not language_only_requirements:
        selected_ids.append("admission_requirements")
        selected_ids.append("gpa_threshold")
        selected_ids.append("ects_breakdown")
    if _LANGUAGE_HINT_RE.search(compact):
        selected_ids.append("language_requirements")
        selected_ids.append("language_score_thresholds")
    if _INSTRUCTION_LANGUAGE_CONTENT_RE.search(compact):
        selected_ids.append("instruction_language")
    if _DEADLINE_HINT_RE.search(compact) or "application" in compact:
        selected_ids.append("application_deadline")
    if _PORTAL_HINT_RE.search(compact):
        selected_ids.append("application_portal")
    if _CURRICULUM_HINT_RE.search(compact) or "course" in compact:
        selected_ids.append("curriculum_modules")
    if _TUITION_HINT_RE.search(compact):
        selected_ids.append("tuition_fees")
    if re.search(r"\b(duration|ects|credit|semester|year)\b", compact):
        selected_ids.append("duration_ects")

    broad_profile_query = bool(
        has_program_context
        and re.search(r"\b(tell me about|about|overview|details?|information)\b", compact)
    )
    explicit_scope_present = bool(
        _REQUIREMENTS_HINT_RE.search(compact)
        or _LANGUAGE_HINT_RE.search(compact)
        or _DEADLINE_HINT_RE.search(compact)
        or _TUITION_HINT_RE.search(compact)
    )
    if has_program_context and (broad_profile_query or not explicit_scope_present):
        selected_ids.insert(0, "program_overview")

    if not selected_ids:
        if has_program_context:
            selected_ids = ["program_overview"]
        else:
            selected_ids = []

    if broad_profile_query and not explicit_scope_present:
        selected_ids.extend(
            [
                "duration_ects",
                "admission_requirements",
                "language_requirements",
                "application_deadline",
                "application_portal",
                "curriculum_modules",
            ]
        )

    normalized: list[dict] = []
    seen: set[str] = set()
    for field_id in selected_ids:
        if field_id in seen:
            continue
        seen.add(field_id)
        field = field_catalog.get(field_id)
        if field:
            normalized.append(dict(field))
    return normalized


def _required_field_subquestions(required_fields: list[dict]) -> list[str]:
    items = [str(field.get("subquestion", "")).strip() for field in required_fields]
    return [item for item in items if item]


def _coverage_subquestions_from_query(query: str) -> list[str]:
    candidates: list[str] = []
    required_fields = _required_fields_from_query(query)
    candidates.extend(_required_field_subquestions(required_fields))
    compact = " ".join(str(query or "").split()).strip().lower()
    if not compact:
        return _normalize_subquestion_list(candidates, limit=_max_planner_subquestions())
    explicit_admission_scope = bool(
        re.search(
            r"\b(admission requirements?|eligibility|entry|documents?|course requirements?)\b",
            compact,
        )
    )
    has_grade_or_credit_scope = bool(
        re.search(r"\b(gpa|grade|cgpa|ects|credit|credits|prerequisite)\b", compact)
    )
    language_only_requirements = bool(_LANGUAGE_HINT_RE.search(compact)) and (
        not explicit_admission_scope and not has_grade_or_credit_scope
    )
    if _REQUIREMENTS_HINT_RE.search(compact) and not language_only_requirements:
        candidates.append("course requirements and eligibility criteria")
    if _LANGUAGE_HINT_RE.search(compact):
        candidates.append("language requirements and accepted English test minimum scores with exact numbers")
    if _DEADLINE_HINT_RE.search(compact):
        candidates.append("application deadline and intake timeline with exact dates")
    if _CURRICULUM_HINT_RE.search(compact):
        candidates.append("curriculum structure and core modules")
    if _TUITION_HINT_RE.search(compact):
        candidates.append("tuition and semester fees")
    return _normalize_subquestion_list(candidates, limit=_max_planner_subquestions())


def _build_query_planner_messages(query: str, allowed_suffixes: list[str]) -> list[dict]:
    max_queries = _planner_query_limit_for_query(query)
    max_subquestions = _max_planner_subquestions()
    suffix_clause = ", ".join(allowed_suffixes[:3]) if allowed_suffixes else "none"
    required_fields = _required_fields_from_query(query)
    focus_subquestions = _coverage_subquestions_from_query(query)
    focus_text = "\n".join(f"- {item}" for item in focus_subquestions) or "- (none)"
    required_field_text = (
        "\n".join(
            f"- {field.get('label', '')}: {field.get('query_focus', '')}" for field in required_fields
        )
        or "- (none)"
    )
    system_prompt = (
        "You are a web search query planner for high-coverage deep retrieval. "
        "Think in phases: plan -> search fan-out -> evidence fan-in. "
        "Return strict JSON only with keys: queries, subquestions. "
        "queries must preserve key entities, numbers, dates, and negations from the user query. "
        "Make queries independent and non-overlapping so they can run in parallel."
    )
    user_prompt = (
        f"User query: {query}\n"
        f"Allowed domain suffixes (optional): {suffix_clause}\n"
        f"Coverage dimensions to include when relevant:\n{focus_text}\n"
        f"Required field-focused query intents:\n{required_field_text}\n"
        "For fields asking deadlines/scores/duration, include dedicated exact-number/date queries.\n"
        "Prioritize official university pages and DAAD pages.\n"
        f"Return at most {max_queries} queries and at most {max_subquestions} subquestions."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _extract_json_object(raw: str) -> dict | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _heuristic_subquestions(query: str) -> list[str]:
    compact = " ".join(str(query).split()).strip()
    if not compact:
        return []
    parts = re.split(r"\s+(?:and|vs|versus)\s+", compact, flags=re.IGNORECASE)
    normalized = _normalize_subquestion_list(parts, limit=_max_planner_subquestions())
    return normalized if len(normalized) > 1 else []


def _build_heuristic_query_plan(query: str, allowed_suffixes: list[str]) -> dict:
    multi_query_enabled = bool(getattr(settings.web_search, "multi_query_enabled", False))
    coverage_subquestions = _coverage_subquestions_from_query(query)
    heuristic_subquestions = _heuristic_subquestions(query)
    if multi_query_enabled:
        merged_subquestions = _normalize_subquestion_list(
            coverage_subquestions + heuristic_subquestions,
            limit=max(_max_planner_subquestions(), len(coverage_subquestions)),
        )
    else:
        merged_subquestions = []
    query_limit = _planner_query_limit_for_query(query)
    base_queries = _build_query_variants(query, allowed_suffixes)
    if multi_query_enabled:
        query_candidates = base_queries + _build_gap_queries(query, merged_subquestions)
    else:
        query_candidates = base_queries
    merged_queries = _normalize_query_list(
        query_candidates,
        limit=query_limit,
    )
    return {
        "queries": merged_queries or base_queries,
        "subquestions": merged_subquestions,
        "planner": "heuristic",
        "llm_used": False,
    }


def _normalize_query_plan_payload(
    *,
    query: str,
    allowed_suffixes: list[str],
    payload: dict,
) -> dict:
    max_queries = _planner_query_limit_for_query(query)
    max_subquestions = _max_planner_subquestions()
    base_queries = [query] + _build_query_variants(query, allowed_suffixes)
    focus_subquestions = _coverage_subquestions_from_query(query)
    llm_queries = _normalize_query_list(payload.get("queries"), limit=max_queries)
    focus_queries = _build_gap_queries(query, focus_subquestions)
    merged_queries = _normalize_query_list(
        base_queries + focus_queries + llm_queries,
        limit=max_queries,
    )
    dynamic_subquestion_limit = min(12, max(max_subquestions, len(focus_subquestions)))
    merged_subquestions = _normalize_subquestion_list(
        focus_subquestions
        + _normalize_subquestion_list(
            payload.get("subquestions"),
            limit=dynamic_subquestion_limit,
        ),
        limit=dynamic_subquestion_limit,
    )
    return {
        "queries": merged_queries or _build_query_variants(query, allowed_suffixes),
        "subquestions": merged_subquestions,
        "planner": "llm",
        "llm_used": True,
    }


def _query_planner_repair_messages(query: str, raw_planner_output: str) -> list[dict]:
    max_queries = _planner_query_limit_for_query(query)
    max_subquestions = _max_planner_subquestions()
    return [
        {
            "role": "system",
            "content": (
                "Convert planner output into strict JSON only. "
                "Allowed keys: queries, subquestions. "
                "Each value must be an array of short strings."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Repair this planner output:\n{raw_planner_output}\n\n"
                f"Return at most {max_queries} queries and at most {max_subquestions} subquestions."
            ),
        },
    ]


async def _aplan_queries_with_llm(query: str, allowed_suffixes: list[str]) -> dict | None:
    if not bool(getattr(settings.web_search, "query_planner_use_llm", False)):
        return None

    model_candidates = _planner_model_candidates()
    if not model_candidates:
        return None

    acquire_timeout = float(getattr(settings.web_search, "query_planner_acquire_timeout_seconds", 0.0))
    use_cache = bool(getattr(settings.web_search, "query_planner_cache_enabled", True))
    ttl_seconds = int(getattr(settings.web_search, "query_planner_cache_ttl_seconds", 900))
    for model_id in model_candidates:
        cache_key = _planner_cache_key(
            model_id=model_id,
            query=query,
            allowed_suffixes=allowed_suffixes,
        )
        if use_cache:
            cached = await _read_cache_json(cache_key)
            if cached:
                cached_plan = _normalize_query_plan_payload(
                    query=query,
                    allowed_suffixes=allowed_suffixes,
                    payload=cached,
                )
                cached_plan["planner"] = "llm_cache"
                cached_plan["llm_used"] = True
                emit_trace_event(
                    "query_plan_cache_hit",
                    {
                        "query": query[:220],
                        "model_id": model_id,
                    },
                )
                return cached_plan

        try:
            messages = _build_query_planner_messages(query, allowed_suffixes)
            content = await _call_planner_model_text(
                model_id=model_id,
                messages=messages,
                acquire_timeout_seconds=acquire_timeout,
            )
        except DependencyBackpressureError as exc:
            logger.warning(
                "Web-search query planner backpressure for model=%s; trying fallback planner model. %s",
                model_id,
                exc,
            )
            emit_trace_event(
                "query_plan_backpressure",
                {
                    "query": query[:220],
                    "model_id": model_id,
                    "retry_after_seconds": round(float(exc.retry_after_seconds), 3),
                },
            )
            continue
        except Exception as exc:
            logger.warning(
                "Web-search query planner failed for model=%s; trying fallback planner model. %s",
                model_id,
                exc,
            )
            continue

        payload = _extract_json_object(content)
        if not payload and content:
            try:
                repair_messages = _query_planner_repair_messages(query, content)
                repaired = await _call_planner_model_text(
                    model_id=model_id,
                    messages=repair_messages,
                    acquire_timeout_seconds=acquire_timeout,
                )
                payload = _extract_json_object(repaired)
            except DependencyBackpressureError:
                payload = None
            except Exception:
                payload = None
        if not payload:
            continue

        plan = _normalize_query_plan_payload(
            query=query,
            allowed_suffixes=allowed_suffixes,
            payload=payload,
        )
        if use_cache:
            await _write_cache_json(
                cache_key,
                {
                    "queries": plan.get("queries", []),
                    "subquestions": plan.get("subquestions", []),
                },
                ttl_seconds=ttl_seconds,
            )
        if model_id != model_candidates[0]:
            emit_trace_event(
                "query_plan_model_fallback_used",
                {
                    "query": query[:220],
                    "model_id": model_id,
                },
            )
        return plan

    return None


def _planner_enabled() -> bool:
    return _is_deep_search_mode() and bool(getattr(settings.web_search, "query_planner_enabled", True))


async def _resolve_query_plan(query: str, allowed_suffixes: list[str]) -> dict:
    if not _planner_enabled():
        return _build_heuristic_query_plan(query, allowed_suffixes)
    llm_plan = await _aplan_queries_with_llm(query, allowed_suffixes)
    if llm_plan:
        return llm_plan
    return _build_heuristic_query_plan(query, allowed_suffixes)


def _loop_llm_enabled() -> bool:
    return _is_deep_search_mode() and bool(getattr(settings.web_search, "retrieval_loop_use_llm", False))


def _compact_facts_for_prompt(facts: list[dict], *, limit: int) -> list[str]:
    lines: list[str] = []
    for item in facts:
        if not isinstance(item, dict):
            continue
        fact = " ".join(str(item.get("fact", "")).split()).strip()
        if not fact:
            continue
        url = str(item.get("url", "")).strip()
        if url:
            lines.append(f"- {fact} | {url}")
        else:
            lines.append(f"- {fact}")
        if len(lines) >= limit:
            break
    return lines


def _build_gap_analyzer_messages(
    query: str,
    *,
    subquestions: list[str],
    facts: list[dict],
) -> list[dict]:
    max_gap_queries = max(1, int(getattr(settings.web_search, "retrieval_loop_max_gap_queries", 2)))
    compact_facts = _compact_facts_for_prompt(facts, limit=12)
    subquestion_text = "\n".join(f"- {item}" for item in subquestions) or "- (none)"
    facts_text = "\n".join(compact_facts) or "- (none)"
    system_prompt = (
        "You analyze retrieval coverage. "
        "Reason silently and return JSON only with keys: missing_subquestions, queries. "
        "Do not include explanations."
    )
    user_prompt = (
        f"User query: {query}\n"
        f"Subquestions:\n{subquestion_text}\n"
        f"Extracted facts:\n{facts_text}\n"
        f"Return at most {max_gap_queries} targeted follow-up queries."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _normalize_gap_plan_payload(payload: dict, *, query: str, fallback_missing: list[str]) -> dict:
    max_gap_queries = max(1, int(getattr(settings.web_search, "retrieval_loop_max_gap_queries", 2)))
    missing = _normalize_subquestion_list(
        payload.get("missing_subquestions"), limit=_max_planner_subquestions()
    )
    if not missing:
        missing = list(fallback_missing)
    queries = _normalize_query_list(payload.get("queries"), limit=max_gap_queries)
    if not queries and missing:
        queries = _build_gap_queries(query, missing)
    return {
        "missing_subquestions": missing,
        "queries": queries,
    }


def _gap_planner_repair_messages(
    query: str,
    *,
    fallback_missing: list[str],
    raw_planner_output: str,
) -> list[dict]:
    max_gap_queries = max(1, int(getattr(settings.web_search, "retrieval_loop_max_gap_queries", 2)))
    fallback_text = "\n".join(f"- {item}" for item in fallback_missing[:8]) or "- (none)"
    return [
        {
            "role": "system",
            "content": (
                "Convert coverage-gap analysis output into strict JSON only. "
                "Allowed keys: missing_subquestions, queries. "
                "Each value must be an array of short strings."
            ),
        },
        {
            "role": "user",
            "content": (
                f"User query: {query}\n"
                f"Fallback missing coverage items:\n{fallback_text}\n\n"
                f"Repair this planner output:\n{raw_planner_output}\n\n"
                f"Return at most {max_gap_queries} queries."
            ),
        },
    ]


async def _aidentify_gap_plan_with_llm(
    query: str,
    *,
    subquestions: list[str],
    facts: list[dict],
    fallback_missing: list[str],
) -> dict | None:
    if not _loop_llm_enabled() or not subquestions:
        return None
    model_candidates = _retrieval_loop_model_candidates()
    if not model_candidates:
        return None

    acquire_timeout = float(getattr(settings.web_search, "retrieval_loop_acquire_timeout_seconds", 0.0))
    use_cache = bool(getattr(settings.web_search, "retrieval_loop_cache_enabled", True))
    ttl_seconds = int(getattr(settings.web_search, "retrieval_loop_cache_ttl_seconds", 300))
    for model_id in model_candidates:
        cache_key = _gap_planner_cache_key(
            model_id=model_id,
            query=query,
            subquestions=subquestions,
            facts=facts,
            fallback_missing=fallback_missing,
        )
        if use_cache:
            cached = await _read_cache_json(cache_key)
            if cached:
                plan = _normalize_gap_plan_payload(
                    cached,
                    query=query,
                    fallback_missing=fallback_missing,
                )
                emit_trace_event(
                    "retrieval_gap_plan_cache_hit",
                    {
                        "query": query[:220],
                        "model_id": model_id,
                    },
                )
                return plan

        try:
            messages = _build_gap_analyzer_messages(
                query,
                subquestions=subquestions,
                facts=facts,
            )
            content = await _call_planner_model_text(
                model_id=model_id,
                messages=messages,
                acquire_timeout_seconds=acquire_timeout,
            )
        except DependencyBackpressureError as exc:
            logger.warning(
                "Web-search retrieval-loop backpressure for model=%s; trying fallback loop model. %s",
                model_id,
                exc,
            )
            emit_trace_event(
                "retrieval_gap_plan_backpressure",
                {
                    "query": query[:220],
                    "model_id": model_id,
                    "retry_after_seconds": round(float(exc.retry_after_seconds), 3),
                },
            )
            continue
        except Exception as exc:
            logger.warning(
                "Web-search retrieval-loop gap analysis failed for model=%s; trying fallback loop model. %s",
                model_id,
                exc,
            )
            continue

        payload = _extract_json_object(content)
        if not payload and content:
            try:
                repaired = await _call_planner_model_text(
                    model_id=model_id,
                    messages=_gap_planner_repair_messages(
                        query,
                        fallback_missing=fallback_missing,
                        raw_planner_output=content,
                    ),
                    acquire_timeout_seconds=acquire_timeout,
                )
                payload = _extract_json_object(repaired)
            except DependencyBackpressureError:
                payload = None
            except Exception:
                payload = None
        if not payload:
            continue

        plan = _normalize_gap_plan_payload(
            payload,
            query=query,
            fallback_missing=fallback_missing,
        )
        if use_cache:
            await _write_cache_json(
                cache_key,
                {
                    "missing_subquestions": plan.get("missing_subquestions", []),
                    "queries": plan.get("queries", []),
                },
                ttl_seconds=ttl_seconds,
            )
        if model_id != model_candidates[0]:
            emit_trace_event(
                "retrieval_gap_model_fallback_used",
                {
                    "query": query[:220],
                    "model_id": model_id,
                },
            )
        return plan

    return None


def _subquestion_token_coverage(subquestion: str, evidence_text: str) -> float:
    tokens = _token_signature(subquestion)
    if not tokens:
        return 1.0
    evidence_tokens = _token_signature(evidence_text)
    if not evidence_tokens:
        return 0.0
    return len(tokens & evidence_tokens) / max(1, len(tokens))


def _identify_missing_subquestions(subquestions: list[str], facts: list[dict]) -> list[str]:
    if not subquestions:
        return []
    evidence_text = " ".join(str(item.get("fact", "")) for item in facts if isinstance(item, dict))
    threshold = float(getattr(settings.web_search, "retrieval_gap_min_token_coverage", 0.5))
    missing: list[str] = []
    for subquestion in subquestions:
        if _subquestion_token_coverage(subquestion, evidence_text) >= threshold:
            continue
        missing.append(subquestion)
    return missing


def _candidate_evidence_text(candidate: dict) -> str:
    if not isinstance(candidate, dict):
        return ""
    metadata = candidate.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    parts = [
        str(candidate.get("content", "")),
        str(metadata.get("title", "")),
        str(metadata.get("snippet", "")),
        str(metadata.get("url", "")),
    ]
    return " ".join(part for part in parts if part).strip().lower()


def _sentence_candidates_from_text(text: str, *, limit: int = 22) -> list[str]:
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return []
    compact = _FIELD_BOUNDARY_LABEL_RE.sub(". ", compact)
    candidates: list[str] = []
    seen: set[str] = set()
    # Sentence-level fragments work better for typed extraction than whole-page blobs.
    for sentence in re.split(r"(?<=[.!?])\s+|[;•]\s+", compact):
        fragment = " ".join(str(sentence).split()).strip(" -|")
        if len(fragment) < 8:
            continue
        key = fragment.lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(fragment[:360])
        if len(candidates) >= max(1, limit):
            break
    if candidates:
        return candidates
    return [compact[:360]]


def _normalize_decimal_value(value: str) -> str:
    compact = " ".join(str(value or "").split()).strip()
    if not compact:
        return ""
    return compact.replace(",", ".")


def _extract_instruction_language_value(sentence: str) -> str:
    compact = " ".join(str(sentence or "").split()).strip()
    lowered = compact.lower()
    if not compact:
        return ""
    if not (
        _INSTRUCTION_LANGUAGE_CONTENT_RE.search(lowered)
        or re.search(r"\b(taught in|offered in|sprache)\b", lowered)
    ):
        return ""
    values = [item.lower() for item in _INSTRUCTION_LANGUAGE_VALUE_RE.findall(compact)]
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = raw
        if raw in {"englisch"}:
            value = "English"
        elif raw in {"deutsch"}:
            value = "German"
        elif raw == "english":
            value = "English"
        elif raw == "german":
            value = "German"
        elif raw == "bilingual":
            value = "Bilingual"
        if value.lower() in seen:
            continue
        seen.add(value.lower())
        normalized.append(value)
    if not normalized:
        return ""
    return ", ".join(normalized[:3])


def _extract_language_threshold_value(sentence: str) -> str:
    compact = " ".join(str(sentence or "").split()).strip()
    if not compact:
        return ""
    match = _LANGUAGE_SCORE_VALUE_RE.search(compact)
    if match:
        test_name = str(match.group(1) or "").upper().replace("UNI-CERT", "UNI-CERT")
        score = _normalize_decimal_value(str(match.group(2) or ""))
        if test_name and score:
            return f"{test_name} {score}"
    level_match = _LANGUAGE_LEVEL_VALUE_RE.search(compact)
    if level_match and _LANGUAGE_CONTENT_RE.search(compact):
        return " ".join(str(level_match.group(0) or "").split()).strip().upper()
    return ""


def _extract_language_requirement_value(sentence: str) -> str:
    compact = " ".join(str(sentence or "").split()).strip()
    if not compact:
        return ""
    threshold_value = _extract_language_threshold_value(compact)
    if threshold_value:
        return threshold_value
    lowered = compact.lower()
    if not _LANGUAGE_CONTENT_RE.search(lowered):
        return ""
    languages: list[str] = []
    seen_languages: set[str] = set()
    for raw in _LANGUAGE_NAME_VALUE_RE.findall(compact):
        value = raw.lower()
        if value in {"englisch", "english"}:
            normalized = "English"
        elif value in {"deutsch", "german"}:
            normalized = "German"
        else:
            normalized = raw
        key = normalized.lower()
        if key in seen_languages:
            continue
        seen_languages.add(key)
        languages.append(normalized)
    level_match = _LANGUAGE_LEVEL_VALUE_RE.search(compact)
    level = ""
    if level_match:
        level = " ".join(str(level_match.group(0) or "").split()).strip().upper()
    if languages and level:
        return f"{'/'.join(languages)} {level}".strip()
    if languages:
        return "/".join(languages)
    if level:
        return level
    return compact[:180]


def _extract_admission_requirement_value(sentence: str) -> str:
    compact = " ".join(str(sentence or "").split()).strip()
    if not compact:
        return ""
    if not _ADMISSION_CONTENT_RE.search(compact):
        return ""
    compact = _NEXT_FIELD_LABEL_RE.split(compact, maxsplit=1)[0].strip(" .:-")
    compact = re.sub(
        r"^(?:eligibility requirements?|admission requirements?|course requirements?)\s*:\s*",
        "",
        compact,
        flags=re.IGNORECASE,
    ).strip()
    if not compact:
        return ""
    lowered = compact.lower()
    has_specific_requirement = bool(
        re.search(
            r"\b(bachelor|degree|qualifying degree|university degree|subject|"
            r"ects|credit|credits|gpa|grade|mindestnote|selection|documents?|"
            r"zulassungsvoraussetzungen|auswahlsatzung)\b",
            lowered,
        )
    )
    # Reject page-summary values such as "120" that are not eligibility requirements.
    if not has_specific_requirement:
        return ""
    if re.fullmatch(r"\d{1,3}(?:\s*ects)?", lowered):
        return ""
    return compact[:220]


def _extract_gpa_threshold_value(sentence: str) -> str:
    compact = " ".join(str(sentence or "").split()).strip()
    if not compact:
        return ""
    match = _GPA_VALUE_RE.search(compact)
    if not match:
        match = _GPA_FALLBACK_VALUE_RE.search(compact)
    if not match:
        return ""
    numeric = _normalize_decimal_value(str(match.group(1) or ""))
    if not numeric:
        return ""
    return numeric


def _extract_ects_value(sentence: str, *, require_prerequisite_context: bool = False) -> str:
    compact = " ".join(str(sentence or "").split()).strip()
    if not compact:
        return ""
    if require_prerequisite_context and not re.search(
        r"\b(requirements?|prerequisites?|admission|eligibility|qualifying|"
        r"voraussetzungen|zulassung|subject area|computer science|mathematics|business)\b",
        compact,
        flags=re.IGNORECASE,
    ):
        return ""
    if require_prerequisite_context and re.search(
        r"\b(total|duration|overall|program(?:me)? comprises|program(?:me)? has)\b",
        compact,
        flags=re.IGNORECASE,
    ):
        return ""
    match = _ECTS_VALUE_RE.search(compact)
    if not match:
        return ""
    return f"{str(match.group(1) or '').strip()} ECTS"


def _extract_admission_decision_signal_value(sentence: str) -> str:
    compact = " ".join(str(sentence or "").split()).strip()
    if not compact:
        return ""
    compact = _NEXT_FIELD_LABEL_RE.split(compact, maxsplit=1)[0].strip(" .:-")
    lowered = compact.lower()
    if not re.search(
        r"\b(selection criteria|selection statute|ranking|ranked|minimum grade|"
        r"admission score|selection threshold|capacity|places|cutoff|cut-off|"
        r"auswahl|auswahlsatzung|rangliste|mindestnote)\b",
        lowered,
    ):
        return ""
    return compact[:240]


def _extract_deadline_value(sentence: str, source_url: str) -> str:
    compact = " ".join(str(sentence or "").split()).strip()
    if not compact:
        return ""
    has_deadline_context = bool(_DEADLINE_CONTENT_RE.search(compact))
    if not has_deadline_context:
        # Check for German date format (DD.MM.YYYY) which is unambiguous
        german_date_match = re.search(r'\b\d{1,2}\.\d{1,2}\.\d{4}\b', compact)
        if german_date_match:
            return german_date_match.group(0)

        # Fallback to URL hint check
        source_hint = " ".join(str(source_url or "").split()).strip().lower()
        if not (_DEADLINE_URL_HINT_RE.search(source_hint) and _DEADLINE_VALUE_RE.search(compact)):
            return ""
    range_match = _DEADLINE_RANGE_VALUE_RE.search(compact)
    if range_match:
        return str(range_match.group(1) or "").strip()
    match = _DEADLINE_VALUE_RE.search(compact)
    if not match:
        return ""
    return str(match.group(0) or "").strip()


def _typed_field_value_from_sentence(field_id: str, sentence: str, source_url: str) -> str:
    compact = " ".join(str(sentence or "").split()).strip()
    lowered = compact.lower()
    if not compact:
        return ""
    if (
        _is_high_precision_admissions_context()
        and _is_university_program_query(_current_retrieval_query())
        and _is_admissions_noise_text(
        f"{source_url} {compact}"
        )
    ):
        return ""
    if field_id == "program_overview":
        if re.search(r"\b(master|m\.sc|msc|program|programme|department)\b", lowered):
            return compact
        return ""
    if field_id == "admission_requirements":
        return _extract_admission_requirement_value(compact)
    if field_id == "gpa_threshold":
        return _extract_gpa_threshold_value(compact)
    if field_id == "ects_breakdown":
        return _extract_ects_value(compact, require_prerequisite_context=True)
    if field_id == "instruction_language":
        return _extract_instruction_language_value(compact)
    if field_id == "language_requirements":
        if not _LANGUAGE_CONTENT_RE.search(lowered):
            return ""
        return _extract_language_requirement_value(compact)
    if field_id == "language_score_thresholds":
        return _extract_language_threshold_value(compact)
    if field_id == "application_deadline":
        return _extract_deadline_value(compact, source_url)
    if field_id == "application_portal":
        return _extract_portal_value(compact, source_url)
    if field_id == "duration_ects":
        return _extract_ects_value(compact)
    if field_id == "curriculum_modules":
        return compact if _CURRICULUM_CONTENT_RE.search(lowered) else ""
    if field_id == "tuition_fees":
        if _TUITION_CONTENT_RE.search(lowered) and _NUMERIC_TOKEN_RE.search(lowered):
            return compact
        return ""
    if field_id == "professors_or_supervisors":
        return compact if re.search(r"\b(professor|faculty|supervisor|advisor|chair)\b", lowered) else ""
    if field_id == "labs_or_research_groups":
        return compact if re.search(r"\b(lab|laboratory|research group|institute|chair)\b", lowered) else ""
    if field_id == "contact_information":
        has_marker = re.search(r"\b(contact|email|e-mail|phone|telephone|admissions office|coordinator)\b", lowered)
        has_value = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|https?://|www\.|\+\d|\b\d{6,}\b", compact)
        return compact if has_marker and has_value else ""
    if field_id == "visa_or_work_rights":
        return compact if re.search(r"\b(visa|residence permit|work rights|working hours|student visa)\b", lowered) else ""
    if field_id == "funding_or_scholarship":
        return compact if re.search(r"\b(scholarship|funding|grant|financial aid|stipend)\b", lowered) else ""
    if field_id == "admission_decision_signal":
        return _extract_admission_decision_signal_value(compact)
    return ""


def _typed_field_value_from_text(field_id: str, text: str, source_url: str) -> str:
    for sentence in _sentence_candidates_from_text(text):
        value = _typed_field_value_from_sentence(field_id, sentence, source_url)
        if value:
            return value
    return ""


def _required_field_covered_by_text(field_id: str, text: str) -> bool:
    return bool(_typed_field_value_from_text(field_id, text, ""))


def _required_field_coverage(
    required_fields: list[dict],
    candidates: list[dict],
) -> dict:
    if not required_fields:
        return {
            "fields": [],
            "missing_ids": [],
            "missing_labels": [],
            "missing_subquestions": [],
            "coverage": 1.0,
        }

    evidence_rows = _required_field_evidence_table(
        required_fields,
        [item for item in candidates if isinstance(item, dict)],
        emit_events=False,
    )
    found_ids = {
        str(row.get("id", row.get("field", ""))).strip()
        for row in evidence_rows
        if str(row.get("status", "")).strip().lower() == "found"
    }
    statuses: list[dict] = []
    missing_ids: list[str] = []
    missing_labels: list[str] = []
    missing_subquestions: list[str] = []

    for field in required_fields:
        field_id = str(field.get("id", "")).strip()
        if not field_id:
            continue
        covered = field_id in found_ids
        label = str(field.get("label", field_id)).strip() or field_id
        statuses.append({"id": field_id, "label": label, "covered": covered})
        if covered:
            continue
        missing_ids.append(field_id)
        missing_labels.append(label)
        subquestion = " ".join(str(field.get("subquestion", "")).split()).strip()
        if subquestion:
            missing_subquestions.append(subquestion)

    total = len(statuses)
    covered_count = len([item for item in statuses if item.get("covered")])
    coverage = 1.0 if total <= 0 else (covered_count / total)
    return {
        "fields": statuses,
        "missing_ids": missing_ids,
        "missing_labels": missing_labels,
        "missing_subquestions": _normalize_subquestion_list(
            missing_subquestions,
            limit=max(1, _max_planner_subquestions()),
        ),
        "coverage": max(0.0, min(1.0, coverage)),
    }


def _field_sentence_priority(field_id: str, sentence: str) -> float:
    text = " ".join(str(sentence or "").split()).strip().lower()
    if not text:
        return 0.0
    score = 0.0
    if field_id == "application_deadline":
        if _DEADLINE_CONTENT_RE.search(text):
            score += 1.2
        if _DEADLINE_VALUE_RE.search(text):
            score += 1.1
    elif field_id == "application_portal":
        if _PORTAL_CONTENT_RE.search(text):
            score += 1.0
        if _URL_VALUE_RE.search(text):
            score += 1.0
    elif field_id == "instruction_language":
        if _INSTRUCTION_LANGUAGE_CONTENT_RE.search(text):
            score += 1.4
        if _INSTRUCTION_LANGUAGE_VALUE_RE.search(text):
            score += 0.9
    elif field_id == "language_score_thresholds":
        if _LANGUAGE_SCORE_VALUE_RE.search(text):
            score += 1.4
    elif field_id == "language_requirements":
        if _LANGUAGE_CONTENT_RE.search(text):
            score += 1.0
        if _LANGUAGE_SCORE_VALUE_RE.search(text):
            score += 0.8
    elif field_id == "gpa_threshold":
        if _GPA_VALUE_RE.search(text):
            score += 1.3
    elif field_id == "ects_breakdown":
        if _ECTS_VALUE_RE.search(text):
            score += 1.2
        elif _DURATION_ECTS_CONTENT_RE.search(text):
            score += 0.5
    elif field_id == "admission_requirements":
        if _ADMISSION_CONTENT_RE.search(text):
            score += 1.1
    elif field_id == "duration_ects":
        if _ECTS_VALUE_RE.search(text):
            score += 1.2
        elif _DURATION_ECTS_CONTENT_RE.search(text):
            score += 1.1
    elif field_id == "curriculum_modules":
        if _CURRICULUM_CONTENT_RE.search(text):
            score += 1.1
    elif field_id == "tuition_fees":
        if _TUITION_CONTENT_RE.search(text):
            score += 1.1
        if _NUMERIC_TOKEN_RE.search(text):
            score += 0.5
    elif field_id == "professors_or_supervisors":
        if re.search(r"\b(professor|faculty|supervisor|advisor|chair)\b", text):
            score += 1.1
    elif field_id == "labs_or_research_groups":
        if re.search(r"\b(lab|laboratory|research group|institute|chair)\b", text):
            score += 1.1
    elif field_id == "contact_information":
        if re.search(r"\b(contact|email|e-mail|phone|admissions office|coordinator)\b", text):
            score += 1.0
        if re.search(r"@|https?://|www\.", text):
            score += 0.7
    elif field_id == "visa_or_work_rights":
        if re.search(r"\b(visa|residence permit|work rights|working hours|student visa)\b", text):
            score += 1.1
    elif field_id == "funding_or_scholarship":
        if re.search(r"\b(scholarship|funding|grant|financial aid|stipend)\b", text):
            score += 1.1
    elif field_id == "admission_decision_signal":
        if re.search(r"\b(selection criteria|ranking|minimum grade|admission score|selection threshold)\b", text):
            score += 1.1
    elif field_id == "program_overview":
        if re.search(r"\b(master|m\.sc|msc|english|semester|ects)\b", text):
            score += 0.9
    if len(text) >= 48:
        score += 0.15
    return score


def _best_field_sentence(field_id: str, content: str) -> str:
    text = " ".join(str(content or "").split()).strip()
    if not text:
        return ""
    sentences = _sentence_candidates_from_text(text, limit=24)
    if not sentences:
        return text[:300]
    ranked = sorted(
        ((float(_field_sentence_priority(field_id, sentence)), sentence) for sentence in sentences),
        key=lambda item: item[0],
        reverse=True,
    )
    top_score, top_sentence = ranked[0]
    if top_score <= 0:
        return _fact_text_from_content(text)
    return str(top_sentence).strip()[:300]


def _extract_portal_value(text: str, source_url: str) -> str:
    compact = " ".join(str(text or "").split()).strip()
    links = [str(item).strip(".,);") for item in _URL_VALUE_RE.findall(compact)]
    for link in links:
        if _PORTAL_URL_RE.search(link):
            lowered = link.lower()
            if lowered.startswith("www."):
                return f"https://{link}"
            return link
    source = str(source_url or "").strip()
    if source:
        source_lower = source.lower()
        if _PORTAL_SOURCE_URL_RE.search(source_lower):
            return source
        if _PORTAL_CONTENT_RE.search(compact.lower()) and _PORTAL_APPLY_SOURCE_URL_RE.search(source_lower):
            return source
    return ""


def _field_value_from_sentence(field_id: str, sentence: str, source_url: str) -> str:
    return _typed_field_value_from_sentence(field_id, sentence, source_url)


def _field_evidence_source_type(url: str) -> str:
    host = _normalized_host(str(url or ""))
    if not host:
        return "discovery"
    if _host_is_official_allowlisted(host) or _host_looks_official_institution(host):
        return "official"
    return "discovery"


def _field_evidence_timestamp(value: str) -> str:
    if value and re.match(r"^\d{4}-\d{2}-\d{2}T", value):
        return value
    return datetime.now(timezone.utc).isoformat()


_PROGRAM_SPECIFIC_CRITICAL_FIELD_IDS = {
    "admission_requirements",
    "gpa_threshold",
    "ects_breakdown",
    "instruction_language",
    "language_requirements",
    "language_score_thresholds",
}


def _program_specific_field_scope_status(
    *,
    field_id: str,
    title: str,
    url: str,
    snippet: str,
    content: str,
    sentence: str,
) -> tuple[bool, str]:
    if field_id not in _PROGRAM_SPECIFIC_CRITICAL_FIELD_IDS:
        return True, ""
    query = _current_retrieval_query()
    if not _is_university_program_query(query):
        return True, ""
    scope_text = f"{title} {url} {snippet} {sentence} {content[:1200]}"
    if _is_admissions_noise_text(scope_text):
        return False, "admissions_noise"
    if not _passes_degree_level_lock(title=title, url=url, snippet=snippet, content=scope_text):
        return False, "degree_level_mismatch"
    if field_id in {"language_requirements", "language_score_thresholds"}:
        lowered_scope = scope_text.lower()
        generic_language_page = any(
            marker in lowered_scope
            for marker in (
                "foreign language requirements",
                "masters-programs-foreign-language-requirements",
                "language requirements",
                "proof-of-language-proficiency",
                "sprachnachweis",
            )
        )
        if generic_language_page:
            target_groups = {
                str(item).strip().lower()
                for item in _RETRIEVAL_TARGET_DOMAINS_CTX.get(())
                if str(item).strip()
            }
            domain_group = _domain_group_key(_normalized_host(url))
            if not target_groups or (domain_group and domain_group in target_groups):
                return True, ""
    if not _passes_program_scope_lock(title=title, url=url, snippet=snippet, content=scope_text):
        return False, "program_scope_mismatch"
    signature = _program_focus_signature(query)
    subject_tokens = signature.get("subject", set()) or set()
    if not subject_tokens:
        return True, ""
    if _evidence_has_subject_alias(scope_text, subject_tokens):
        return True, ""
    evidence_tokens = _token_signature(scope_text)
    min_subject_overlap = 1 if len(subject_tokens) <= 1 else 2
    if len(subject_tokens & evidence_tokens) >= min_subject_overlap:
        return True, ""
    path = str(urlparse(url).path or "").strip().lower()
    slug_hints = _subject_slug_hints(subject_tokens)
    if slug_hints and any(hint in path for hint in slug_hints):
        return True, ""
    return False, "program_scope_mismatch"


def _required_field_evidence_table(
    required_fields: list[dict],
    candidates: list[dict],
    *,
    emit_events: bool = True,
) -> list[dict]:
    if not required_fields:
        return []
    strict_official = bool(_RETRIEVAL_STRICT_OFFICIAL_CTX.get(False))
    evidence_rows: list[dict] = []
    rejected_rows: list[dict] = []
    for field in required_fields:
        field_id = str(field.get("id", "")).strip()
        label = str(field.get("label", field_id)).strip() or field_id
        if not field_id:
            continue
        best_row: dict | None = None
        best_score = -1.0
        seen_values: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            metadata = candidate.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            source_url = str(metadata.get("url", "")).strip()
            content = " ".join(
                part
                for part in (
                    str(candidate.get("content", "")),
                    str(metadata.get("title", "")),
                    str(metadata.get("snippet", "")),
                    source_url,
                )
                if part
            )
            content = " ".join(content.split()).strip()
            if not content:
                continue
            sentence = ""
            value = ""
            for sentence_candidate in _sentence_candidates_from_text(content, limit=24):
                typed_value = _field_value_from_sentence(field_id, sentence_candidate, source_url)
                if not typed_value:
                    continue
                sentence = sentence_candidate
                value = typed_value
                break
            if not value:
                continue
            trust = float(metadata.get("trust_score", 0.0) or 0.0)
            match_score = _field_sentence_priority(field_id, sentence)
            score = (trust * 0.62) + (match_score * 0.38)
            if field_id == "application_portal":
                source_lower = source_url.lower()
                if _PORTAL_SOURCE_URL_RE.search(source_lower):
                    score += 1.0
                elif _PORTAL_APPLY_SOURCE_URL_RE.search(source_lower):
                    score += 0.25
            normalized_value_key = " ".join(str(value).lower().split())
            source_type = _field_evidence_source_type(source_url)
            if strict_official and source_type != "official":
                rejected = {
                    "field": field_id,
                    "source_url": source_url,
                    "reason": "non_official_source",
                    "evidence_snippet": sentence,
                }
                rejected_rows.append(rejected)
                if emit_events:
                    emit_trace_event("slot_candidate_rejected", rejected)
                continue
            in_scope, rejection_reason = _program_specific_field_scope_status(
                field_id=field_id,
                title=str(metadata.get("title", "")),
                url=source_url,
                snippet=str(metadata.get("snippet", "")),
                content=content,
                sentence=sentence,
            )
            if not in_scope:
                rejected = {
                    "field": field_id,
                    "source_url": source_url,
                    "reason": rejection_reason,
                    "evidence_snippet": sentence,
                }
                rejected_rows.append(rejected)
                if emit_events:
                    emit_trace_event("slot_candidate_rejected", rejected)
                continue
            source_tier = str(metadata.get("source_tier", "")).strip().lower()
            if not source_tier:
                source_tier = "tier0_official" if source_type == "official" else "discovery"
            if normalized_value_key:
                seen_values.add(normalized_value_key)
            row = {
                "field": field_id,
                "id": field_id,
                "label": label,
                "status": "found",
                "value": value,
                "source_url": source_url,
                "source_type": source_type,
                "source_tier": source_tier,
                "evidence_snippet": sentence,
                "evidence_text": sentence,
                "confidence": round(max(0.0, min(1.0, 0.45 + (trust * 0.5))), 4),
                "retrieved_at": _field_evidence_timestamp(str(metadata.get("published_date", ""))),
            }
            if emit_events:
                emit_trace_event(
                    "slot_candidate_extracted",
                    {
                        "field": field_id,
                        "value": value,
                        "source_url": source_url,
                        "source_type": source_type,
                    },
                )
            if score <= best_score:
                continue
            best_score = score
            best_row = row
        if (
            best_row is not None
            and field_id in _CONFLICT_SENSITIVE_REQUIRED_FIELDS
            and len(seen_values) > 1
        ):
            if emit_events:
                emit_trace_event(
                    "slot_conflict",
                    {
                        "field": field_id,
                        "values": sorted(seen_values)[:5],
                        "source_url": str(best_row.get("source_url", "")),
                    },
                )
            evidence_rows.append(
                {
                    "field": field_id,
                    "id": field_id,
                    "label": label,
                    "status": "conflict",
                    "value": "Conflict between official sources. Manual verification required.",
                    "source_url": str(best_row.get("source_url", "")),
                    "source_type": str(best_row.get("source_type", "official")),
                    "source_tier": str(best_row.get("source_tier", "tier0_official")),
                    "evidence_snippet": str(best_row.get("evidence_snippet", "")),
                    "evidence_text": str(best_row.get("evidence_text", "")),
                    "confidence": 0.35,
                    "retrieved_at": _field_evidence_timestamp(str(best_row.get("retrieved_at", ""))),
                }
            )
            continue
        if best_row is not None:
            if emit_events:
                emit_trace_event(
                    "slot_filled",
                    {
                        "field": field_id,
                        "value": str(best_row.get("value", "")),
                        "source_url": str(best_row.get("source_url", "")),
                    },
                )
            evidence_rows.append(best_row)
        else:
            if emit_events:
                emit_trace_event(
                    "slot_missing",
                    {
                        "field": field_id,
                        "rejection_reasons": [
                            str(row.get("reason", ""))
                            for row in rejected_rows
                            if str(row.get("field", "")) == field_id
                        ][:5],
                    },
                )
            evidence_rows.append(
                {
                    "field": field_id,
                    "id": field_id,
                    "label": label,
                    "status": "missing",
                    "value": "Not verified from official sources.",
                    "source_url": "",
                    "source_type": "official",
                    "source_tier": "tier0_official",
                    "evidence_snippet": "",
                    "evidence_text": "",
                    "confidence": 0.0,
                    "retrieved_at": _field_evidence_timestamp(""),
                    "rejected_candidates": [
                        row
                        for row in rejected_rows
                        if str(row.get("field", "")) == field_id
                    ][:5],
                }
            )
    return evidence_rows


def _required_fields_by_ids(required_fields: list[dict], missing_ids: list[str]) -> list[dict]:
    if not required_fields or not missing_ids:
        return []
    missing_set = {str(item).strip() for item in missing_ids if str(item).strip()}
    return [
        field
        for field in required_fields
        if str(field.get("id", "")).strip() in missing_set
    ]


def _research_objectives_by_ids(objectives: list[dict], missing_ids: list[str]) -> list[dict]:
    if not objectives or not missing_ids:
        return []
    missing_set = {str(item).strip() for item in missing_ids if str(item).strip()}
    return [
        objective
        for objective in objectives
        if str(objective.get("id", "")).strip() in missing_set
    ]


def _research_objective_mode_enabled(*, query: str, deep_mode: bool, required_fields: list[dict]) -> bool:
    if not deep_mode:
        return False
    compact_query = " ".join(str(query or "").split())
    if _RESEARCHER_HINT_RE.search(compact_query):
        return True
    if required_fields:
        return False
    return bool(_RESEARCH_OBJECTIVE_CONTEXT_RE.search(compact_query))


def _build_research_objective_queries(
    query: str,
    *,
    missing_objectives: list[dict],
    unique_domains: list[str],
) -> list[str]:
    if not missing_objectives:
        return []
    max_gap_queries = max(1, int(getattr(settings.web_search, "retrieval_loop_max_gap_queries", 2)))
    limit = min(24, max_gap_queries * max(3, min(6, len(missing_objectives) + 2)))
    return build_queries_for_missing_objectives(
        query,
        missing_objectives=missing_objectives,
        official_domains=list(unique_domains) + _official_domains_for_query(query),
        max_queries=limit,
    )


def _build_gap_queries(query: str, missing_subquestions: list[str]) -> list[str]:
    if not missing_subquestions:
        return []
    max_gap_queries = max(1, int(getattr(settings.web_search, "retrieval_loop_max_gap_queries", 2)))
    candidates = [
        f"{query} {subquestion}".strip() for subquestion in missing_subquestions[:max_gap_queries]
    ]
    return _normalize_query_list(candidates, limit=max_gap_queries)


def _next_queries_for_loop(
    planned_queries: list[str],
    seen_queries: set[str],
    *,
    max_queries: int | None = None,
) -> list[str]:
    query_limit = max_queries if isinstance(max_queries, int) and max_queries > 0 else _max_planner_queries()
    next_queries: list[str] = []
    for query in planned_queries:
        key = str(query).strip().lower()
        if not key or key in seen_queries:
            continue
        seen_queries.add(key)
        next_queries.append(str(query).strip())
        if len(next_queries) >= query_limit:
            break
    return next_queries


def _url_matches_allowed_suffix(url: str, allowed_suffixes: list[str]) -> bool:
    if not allowed_suffixes:
        return True
    host = str(urlparse(url).hostname or "").strip().lower()
    if not host:
        return False
    return any(host.endswith(suffix) for suffix in allowed_suffixes)


def _filter_rows_by_allowed_domains(rows: list[dict], allowed_suffixes: list[str]) -> list[dict]:
    return _filter_rows_by_allowed_domains_with_policy(
        rows,
        allowed_suffixes,
        strict_official=False,
    )


def _filter_rows_by_allowed_domains_with_policy(
    rows: list[dict],
    allowed_suffixes: list[str],
    *,
    strict_official: bool,
) -> list[dict]:
    filtered: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url", "")).strip()
        title = str(row.get("title", "")).strip()
        snippet = str(row.get("snippet", "")).strip()
        if not _source_url_allowed(
            url=url,
            title=title,
            snippet=snippet,
            allowed_suffixes=allowed_suffixes,
            strict_official=strict_official,
        ):
            continue
        filtered.append(row)
    return filtered


def _dedupe_rows(rows: list[dict], limit: int) -> list[dict]:
    deduped: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url", "")).strip()
        if url:
            key = f"url:{url.lower()}"
        else:
            title = str(row.get("title", "")).strip().lower()
            snippet = str(row.get("snippet", "")).strip().lower()
            key = f"text:{' '.join(f'{title} {snippet}'.split())[:220]}"
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(row)
        if len(deduped) >= limit:
            break
    return deduped


def _wrap_words(text: str, max_chars: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    parts: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            parts.append(current)
            current = word
    parts.append(current)
    return parts


def _segment_text_for_chunking(text: str, max_chars: int) -> list[str]:
    segments: list[str] = []
    for line in text.splitlines():
        normalized = _WHITESPACE_RE.sub(" ", line).strip()
        if not normalized:
            continue
        sentences = [item.strip() for item in _SENTENCE_SPLIT_RE.split(normalized) if item.strip()]
        if not sentences:
            sentences = [normalized]
        for sentence in sentences:
            if len(sentence) <= max_chars:
                segments.append(sentence)
            else:
                segments.extend(_wrap_words(sentence, max_chars))
    return segments


def _append_chunk_if_ready(
    chunks: list[str],
    current: str,
    *,
    min_chunk_chars: int,
    max_chunks: int,
) -> bool:
    if len(current) < min_chunk_chars:
        return False
    chunks.append(current)
    return len(chunks) >= max_chunks


def _next_current_segment(chunks: list[str], *, overlap: int, segment: str) -> str:
    if overlap > 0 and chunks:
        tail = chunks[-1][-overlap:].strip()
        return f"{tail} {segment}".strip()
    return segment


def _finalize_chunks(
    *,
    chunks: list[str],
    current: str,
    segments: list[str],
    max_chars: int,
    min_chunk_chars: int,
    max_chunks: int,
) -> list[str]:
    if current and len(chunks) < max_chunks:
        if len(current) >= min_chunk_chars or not chunks:
            chunks.append(current)
    if not chunks and segments:
        chunks.append(segments[0][:max_chars])
    return chunks[:max_chunks]


def _chunk_clean_text(clean_text: str) -> list[str]:
    max_chars = max(120, int(settings.web_search.page_chunk_chars))
    overlap = max(0, min(int(settings.web_search.page_chunk_overlap_chars), max_chars // 2))
    max_chunks = _max_chunks_per_page_for_mode()
    min_chunk_chars = max(20, int(settings.web_search.min_chunk_chars))
    segments = _segment_text_for_chunking(clean_text, max_chars=max_chars)
    if not segments:
        return []

    chunks: list[str] = []
    current = ""
    for segment in segments:
        if not current:
            current = segment
            continue
        candidate = f"{current} {segment}"
        if len(candidate) <= max_chars:
            current = candidate
            continue

        if _append_chunk_if_ready(
            chunks,
            current,
            min_chunk_chars=min_chunk_chars,
            max_chunks=max_chunks,
        ):
            return chunks
        current = _next_current_segment(chunks, overlap=overlap, segment=segment)

    return _finalize_chunks(
        chunks=chunks,
        current=current,
        segments=segments,
        max_chars=max_chars,
        min_chunk_chars=min_chunk_chars,
        max_chunks=max_chunks,
    )


def _token_signature(text: str) -> set[str]:
    return {
        token
        for token in _QUERY_TOKEN_RE.findall(text.lower())
        if len(token) > 2 and token not in _QUERY_STOPWORDS
    }


def _jaccard_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    if union <= 0:
        return 0.0
    return intersection / union


def _dedupe_chunk_candidates(candidates: list[dict]) -> list[dict]:
    threshold = float(settings.web_search.chunk_dedupe_similarity)
    deduped: list[dict] = []
    signatures: list[tuple[set[str], str]] = []
    for candidate in candidates:
        extracted = _candidate_signature_and_url(candidate)
        if not extracted:
            continue
        signature, url = extracted
        if _is_duplicate_candidate(signature, url, signatures, threshold):
            continue
        deduped.append(candidate)
        signatures.append((signature, url))
    return deduped


def _candidate_signature_and_url(candidate: dict) -> tuple[set[str], str] | None:
    content = str(candidate.get("content", "")).strip()
    if not content:
        return None
    signature = _token_signature(content)
    if not signature:
        signature = {content.lower()[:80]}
    metadata = candidate.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    url = str(metadata.get("url", "")).strip().lower()
    return signature, url


def _is_duplicate_candidate(
    signature: set[str],
    url: str,
    prior_signatures: list[tuple[set[str], str]],
    threshold: float,
) -> bool:
    for prior_signature, prior_url in prior_signatures:
        similarity = _jaccard_similarity(signature, prior_signature)
        if similarity < threshold:
            continue
        same_url = bool(url and prior_url and url == prior_url)
        rich_enough = min(len(signature), len(prior_signature)) >= 6
        if same_url or rich_enough:
            return True
    return False


def _query_tokens(query: str) -> set[str]:
    return _token_signature(query)


def _program_focus_signature(query: str) -> dict:
    compact = " ".join(str(query or "").split()).strip().lower()
    if not compact:
        return {"institution": set(), "subject": set(), "degree": ""}
    institution_tokens: set[str] = set()
    institution_match = re.search(
        r"\b(?:university|universit[a-z]*|uni|technical university|technische universita[et]|tu)\s+"
        r"(?:of\s+)?([a-z0-9äöüß\- ]{2,80})",
        compact,
    )
    if institution_match:
        institution_text = " ".join(str(institution_match.group(1) or "").split()).strip()
        # Stop institution extraction before degree/program requirement phrases.
        institution_text = re.split(
            r"\b(?:m\.?sc|msc|master(?:['’]s)?|b\.?sc|bsc|phd|requirements?|deadline|"
            r"application|apply|program(?:me)?|course|language|ielts|toefl|gpa|ects|portal)\b",
            institution_text,
            maxsplit=1,
        )[0].strip()
        for token in _QUERY_TOKEN_RE.findall(institution_text):
            lowered = token.lower()
            if len(lowered) < 3 or lowered in _PROGRAM_FOCUS_STOPWORDS:
                continue
            institution_tokens.add(lowered)
    subject_tokens: set[str] = set()
    subject_match = re.search(
        r"\b(?:m\.?sc|msc|master(?:['’]s)?(?:\s+of\s+science)?)\s+([a-z0-9&/\- ]{2,90})",
        compact,
    )
    if subject_match:
        subject_text = " ".join(str(subject_match.group(1) or "").split()).strip()
        subject_text = re.split(
            r"\b(?:language|requirement|requirements|deadline|application|where|portal|"
            r"ielts|toefl|gpa|ects|international)\b",
            subject_text,
            maxsplit=1,
        )[0]
        for token in _QUERY_TOKEN_RE.findall(subject_text):
            lowered = token.lower()
            if len(lowered) < 3 or lowered in _PROGRAM_FOCUS_STOPWORDS:
                continue
            if lowered in institution_tokens:
                continue
            subject_tokens.add(lowered)
    degree = ""
    query_tokens = _token_signature(compact)
    if query_tokens & _MASTER_LEVEL_TOKENS:
        degree = "master"
    elif query_tokens & _BACHELOR_LEVEL_TOKENS:
        degree = "bachelor"
    return {
        "institution": institution_tokens,
        "subject": subject_tokens,
        "degree": degree,
    }


def _program_scope_bias(
    *,
    title: str,
    url: str,
    snippet: str,
    content: str,
) -> float:
    query = " ".join(str(_RETRIEVAL_QUERY_CTX.get("") or "").split()).strip()
    if not _is_university_program_query(query):
        return 0.0
    signature = _program_focus_signature(query)
    institution_tokens = signature.get("institution", set()) or set()
    subject_tokens = signature.get("subject", set()) or set()
    degree = str(signature.get("degree", "") or "")
    if not institution_tokens and not subject_tokens and not degree:
        return 0.0

    evidence_tokens = _token_signature(
        f"{title} {url} {snippet} {' '.join(str(content or '').split())[:500]}"
    )
    if not evidence_tokens:
        return 0.0
    bias = 0.0
    if subject_tokens:
        subject_overlap = len(subject_tokens & evidence_tokens) / max(1, len(subject_tokens))
        if subject_overlap >= 0.75:
            bias += 0.1
        elif subject_overlap <= 0.25:
            bias -= 0.18
    if institution_tokens:
        institution_overlap = len(institution_tokens & evidence_tokens) / max(1, len(institution_tokens))
        if institution_overlap >= 0.5:
            bias += 0.04
        elif institution_overlap <= 0.2:
            bias -= 0.1
    has_master = bool(evidence_tokens & _MASTER_LEVEL_TOKENS)
    has_bachelor = bool(evidence_tokens & _BACHELOR_LEVEL_TOKENS)
    if degree == "master" and has_bachelor and not has_master:
        bias -= 0.14
    elif degree == "bachelor" and has_master and not has_bachelor:
        bias -= 0.14
    return bias


def _current_required_field_ids() -> set[str]:
    return {
        str(item).strip()
        for item in _RETRIEVAL_REQUIRED_FIELD_IDS_CTX.get(())
        if str(item).strip()
    }


def _matched_required_field_ids_for_text(text: str, *, required_ids: set[str] | None = None) -> set[str]:
    compact = " ".join(str(text or "").split()).strip().lower()
    if not compact:
        return set()
    active_required_ids = required_ids if required_ids is not None else _current_required_field_ids()
    if not active_required_ids:
        return set()
    matched: set[str] = set()
    for field_id in active_required_ids:
        markers = _PROGRAM_SCOPE_FIELD_ROUTE_MARKERS.get(field_id, ())
        if not markers:
            continue
        if any(str(marker).lower() in compact for marker in markers):
            matched.add(field_id)
    return matched


def _evidence_has_subject_alias(evidence_text: str, subject_tokens: set[str]) -> bool:
    compact = " ".join(str(evidence_text or "").split()).strip().lower()
    if not compact:
        return False
    if "business" in subject_tokens and "informatics" in subject_tokens:
        return bool(
            re.search(
                r"\b(business informatics|business information systems?|information systems?|wirtschaftsinformatik|wifo)\b",
                compact,
            )
        )
    return False


def _subject_slug_hints(subject_tokens: set[str]) -> set[str]:
    lowered_tokens = {str(token).strip().lower() for token in subject_tokens if str(token).strip()}
    hints: set[str] = set()
    if "business" in lowered_tokens and "informatics" in lowered_tokens:
        hints.update({"business-informatics", "businessinformatics", "wirtschaftsinformatik", "wifo"})
    ordered = sorted(lowered_tokens)
    if len(ordered) >= 2:
        hints.add("-".join(ordered[:2]))
    return {hint for hint in hints if hint}


def _passes_program_scope_lock(*, title: str, url: str, snippet: str, content: str) -> bool:
    query = _current_retrieval_query()
    if not _is_university_program_query(query):
        return True

    signature = _program_focus_signature(query)
    institution_tokens = signature.get("institution", set()) or set()
    subject_tokens = signature.get("subject", set()) or set()
    if not institution_tokens and not subject_tokens:
        return True

    evidence_tokens = _token_signature(f"{title} {url} {snippet} {content[:420]}")
    if not evidence_tokens:
        return False

    required_ids = _current_required_field_ids()
    route_matched_field_ids = _matched_required_field_ids_for_text(
        f"{title} {url} {snippet} {content[:640]}",
        required_ids=required_ids,
    )
    target_groups = {
        str(item).strip().lower()
        for item in _RETRIEVAL_TARGET_DOMAINS_CTX.get(())
        if str(item).strip()
    }
    domain_group = _domain_group_key(_normalized_host(url))
    domain_in_scope = bool(domain_group and domain_group in target_groups)
    institution_overlap = 0

    if institution_tokens:
        institution_overlap = len(institution_tokens & evidence_tokens)
        if institution_overlap <= 0 and not domain_in_scope:
            return False

    if subject_tokens:
        slug_hints = _subject_slug_hints(subject_tokens)
        path = str(urlparse(url).path or "").strip().lower()
        if slug_hints and ("/programs/" in path or "/programme/" in path or "/programmes/" in path):
            if not any(hint in path for hint in slug_hints):
                return False
        subject_overlap = len(subject_tokens & evidence_tokens)
        min_subject_overlap = 1 if len(subject_tokens) <= 1 else 2
        if subject_overlap < min_subject_overlap:
            if route_matched_field_ids:
                if route_matched_field_ids.issubset(_PROGRAM_SCOPE_GENERIC_FIELD_IDS):
                    if domain_in_scope or institution_overlap > 0:
                        return True
                if (domain_in_scope or institution_overlap > 0) and _evidence_has_subject_alias(
                    f"{title} {url} {snippet} {content[:640]}",
                    subject_tokens,
                ):
                    return True
            return False
    return True


def _passes_degree_level_lock(*, title: str, url: str, snippet: str, content: str) -> bool:
    query = " ".join(str(_RETRIEVAL_QUERY_CTX.get("") or "").split()).strip()
    if not query:
        return True
    query_tokens = _token_signature(query)
    wants_master = bool(query_tokens & _MASTER_LEVEL_TOKENS)
    wants_bachelor = bool(query_tokens & _BACHELOR_LEVEL_TOKENS)
    if not wants_master and not wants_bachelor:
        return True
    evidence_tokens = _token_signature(f"{title} {url} {snippet} {content[:240]}")
    has_master = bool(evidence_tokens & _MASTER_LEVEL_TOKENS)
    has_bachelor = bool(evidence_tokens & _BACHELOR_LEVEL_TOKENS)
    if wants_master and has_bachelor and not has_master:
        return False
    if wants_bachelor and has_master and not has_bachelor:
        return False
    return True


def _filter_rows_by_program_scope(rows: list[dict], *, allow_fallback_on_empty: bool) -> list[dict]:
    query = _current_retrieval_query()
    if not _is_university_program_query(query):
        return rows
    filtered: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "")).strip()
        snippet = str(row.get("snippet", "")).strip()
        url = str(row.get("url", "")).strip()
        if not _passes_degree_level_lock(title=title, url=url, snippet=snippet, content=snippet):
            continue
        if not _passes_program_scope_lock(title=title, url=url, snippet=snippet, content=snippet):
            continue
        filtered.append(row)
    if filtered:
        return filtered
    return rows if allow_fallback_on_empty else []


def _is_high_precision_admissions_context() -> bool:
    query = _current_retrieval_query()
    required_ids = _current_required_field_ids()
    if required_ids & _ADMISSIONS_HIGH_PRECISION_FIELD_IDS:
        return True
    if not query:
        return False
    return _is_admissions_high_precision_query(query, [{"id": item} for item in sorted(required_ids)])


def _filter_rows_for_admissions_precision(rows: list[dict], *, allow_fallback_on_empty: bool) -> list[dict]:
    if not rows:
        return rows
    if not _is_high_precision_admissions_context():
        return rows
    required_ids = _current_required_field_ids()
    signature = _program_focus_signature(_current_retrieval_query())
    subject_tokens = signature.get("subject", set()) or set()
    filtered: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "")).strip()
        snippet = str(row.get("snippet", "")).strip()
        url = str(row.get("url", "")).strip()
        haystack = " ".join(f"{title} {url} {snippet}".lower().split())
        if _is_admissions_noise_text(haystack):
            continue
        route_matched_field_ids = _matched_required_field_ids_for_text(
            haystack,
            required_ids=required_ids,
        )
        if route_matched_field_ids:
            filtered.append(row)
            continue
        if subject_tokens and _evidence_has_subject_alias(haystack, subject_tokens):
            if re.search(r"\b(program|programme|curriculum|module|statute|admission)\b", haystack):
                filtered.append(row)
    if filtered:
        return filtered
    return rows if allow_fallback_on_empty else []


def _chunk_relevance_score(
    *,
    query_tokens: set[str],
    title: str,
    url: str,
    content: str,
    snippet: str,
    rank_index: int,
) -> float:
    if not content:
        return 0.0
    content_tokens = _token_signature(content)
    overlap = 0.0
    if query_tokens and content_tokens:
        overlap = len(query_tokens & content_tokens) / max(1, len(query_tokens))
    snippet_bonus = 0.0
    if snippet and snippet.lower() in content.lower():
        snippet_bonus = 0.05
    rank_bonus = max(0.0, 0.08 - ((rank_index - 1) * 0.01))
    degree_bias = 0.0
    wants_master = bool(query_tokens & _MASTER_LEVEL_TOKENS)
    wants_bachelor = bool(query_tokens & _BACHELOR_LEVEL_TOKENS)
    has_master = bool(content_tokens & _MASTER_LEVEL_TOKENS)
    has_bachelor = bool(content_tokens & _BACHELOR_LEVEL_TOKENS)
    if wants_master:
        if has_bachelor and not has_master:
            degree_bias -= 0.22
        elif has_master:
            degree_bias += 0.04
    elif wants_bachelor:
        if has_master and not has_bachelor:
            degree_bias -= 0.22
        elif has_bachelor:
            degree_bias += 0.04
    program_bias = _program_scope_bias(
        title=title,
        url=url,
        snippet=snippet,
        content=content,
    )
    return overlap + snippet_bonus + rank_bonus + degree_bias + program_bias


def _domain_authority_score(url: str, allowed_suffixes: list[str]) -> float:
    host = str(urlparse(url).hostname or "").strip().lower()
    parsed = urlparse(url)
    path = parsed.path.lower()

    if not host:
        return 0.35

    # German university mode enhancements
    german_mode = getattr(settings.web_search, "german_university_mode_enabled", True)
    if german_mode:
        # Direct German university domains (highest priority)
        for pattern in _GERMAN_UNIVERSITY_DOMAIN_PATTERNS:
            if re.match(pattern, host):
                base_score = 0.92
                # Boost for relevant page paths
                if any(page_pattern in path for page_pattern in _GERMAN_UNIVERSITY_PAGE_PATTERNS):
                    base_score += 0.05
                return min(base_score, 0.97)

        # Official German education portals
        if host in _GERMAN_OFFICIAL_EDUCATION_DOMAINS or any(host.endswith(f".{domain}") for domain in _GERMAN_OFFICIAL_EDUCATION_DOMAINS):
            return 0.90

    # High authority global domains
    if any(host.endswith(suffix) for suffix in _HIGH_AUTHORITY_SUFFIXES):
        return 0.95

    # Configured allowed suffixes (e.g., .de, .eu)
    if allowed_suffixes and any(host.endswith(suffix) for suffix in allowed_suffixes):
        base_score = 0.82
        # German domain priority boost
        if german_mode and (host.endswith(".de") or host.endswith(".eu")):
            boost = float(getattr(settings.web_search, "german_university_authority_boost", 0.15))
            base_score += boost
        return min(base_score, 0.90)

    if host.endswith(".org"):
        return 0.7
    if host.endswith(".com") or host.endswith(".net"):
        return 0.55
    return 0.5


def _parse_published_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    for candidate in (normalized, normalized[:10]):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _recency_score(published_date: str) -> float:
    parsed = _parse_published_datetime(published_date)
    if parsed is None:
        return 0.5
    now = datetime.now(timezone.utc)
    age_days = max(0, int((now - parsed).total_seconds() // 86400))
    if age_days <= 30:
        return 1.0
    if age_days <= 180:
        return 0.85
    if age_days <= 365:
        return 0.7
    if age_days <= 730:
        return 0.55
    return 0.4


def _agreement_score(candidate: dict, candidates: list[dict]) -> float:
    if not isinstance(candidate, dict):
        return 0.0
    extracted = _candidate_signature_and_url(candidate)
    if not extracted:
        return 0.0
    signature, url = extracted
    support = 0
    for peer in candidates:
        if peer is candidate:
            continue
        peer_extracted = _candidate_signature_and_url(peer)
        if not peer_extracted:
            continue
        peer_signature, peer_url = peer_extracted
        if url and peer_url and url == peer_url:
            continue
        if _jaccard_similarity(signature, peer_signature) >= 0.2:
            support += 1
            if support >= 3:
                break
    return min(1.0, support / 3.0)


def _normalized_trust_weights() -> tuple[float, float, float, float]:
    relevance = max(0.0, float(getattr(settings.web_search, "trust_relevance_weight", 0.6)))
    authority = max(0.0, float(getattr(settings.web_search, "trust_authority_weight", 0.2)))
    recency = max(0.0, float(getattr(settings.web_search, "trust_recency_weight", 0.1)))
    agreement = max(0.0, float(getattr(settings.web_search, "trust_agreement_weight", 0.1)))
    total = relevance + authority + recency + agreement
    if total <= 0:
        return 0.6, 0.2, 0.1, 0.1
    return (
        relevance / total,
        authority / total,
        recency / total,
        agreement / total,
    )


def _apply_trust_scores(candidates: list[dict], allowed_suffixes: list[str]) -> list[dict]:
    relevance_w, authority_w, recency_w, agreement_w = _normalized_trust_weights()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        metadata = candidate.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            candidate["metadata"] = metadata
        url = str(metadata.get("url", "")).strip()
        published_date = str(metadata.get("published_date", "")).strip()
        relevance = max(0.0, min(1.0, float(candidate.get("_score", 0.0))))
        authority = _domain_authority_score(url, allowed_suffixes)
        recency = _recency_score(published_date)
        agreement = _agreement_score(candidate, candidates)
        trust = (
            (relevance * relevance_w)
            + (authority * authority_w)
            + (recency * recency_w)
            + (agreement * agreement_w)
        )
        metadata["trust_score"] = round(trust, 4)
        metadata["trust_components"] = {
            "relevance": round(relevance, 4),
            "authority": round(authority, 4),
            "recency": round(recency, 4),
            "agreement": round(agreement, 4),
        }
        candidate["_trust_score"] = trust
        candidate["_final_score"] = (relevance * 0.65) + (trust * 0.35)
    return candidates


def _boost_pdf_scores(candidates: list[dict]) -> list[dict]:
    """Boost authority and final scores for PDF documents containing critical data."""
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        metadata = candidate.get("metadata", {})
        if not isinstance(metadata, dict):
            continue
        url = str(metadata.get("url", "")).lower()

        # Check if this is a PDF or contains PDF-specific keywords
        is_pdf = (
            url.endswith(".pdf") or
            "filetype:pdf" in url or
            "auswahlsatzung" in url or
            "selection statute" in url or
            "modulhandbuch" in url or
            "prüfungsordnung" in url or
            "pruefungsordnung" in url
        )

        if is_pdf:
            # Boost trust score for PDFs (they contain authoritative data)
            current_trust = candidate.get("_trust_score", 0.5)
            candidate["_trust_score"] = min(1.0, current_trust + 0.15)

            # Boost final score
            current_final = candidate.get("_final_score", 0.5)
            candidate["_final_score"] = min(1.0, current_final + 0.15)

            # Mark in metadata for debugging
            metadata["pdf_boosted"] = True

    return candidates


def _validate_evidence_specificity(candidates: list[dict], required_fields: list[dict]) -> dict:
    """Validate evidence contains specific data vs. generic descriptions."""
    validation = {
        "has_specific_data": False,
        "specific_fields": [],
        "missing_fields": [],
        "specificity_score": 0.0,
        "issues": [],
        "german_data_found": False,
    }

    if not candidates:
        validation["issues"].append("No evidence candidates provided")
        return validation

    # Collect all content
    all_content = " ".join(
        str(c.get("content", "")) for c in candidates if isinstance(c, dict)
    )

    if not all_content.strip():
        validation["issues"].append("Evidence content is empty")
        return validation

    # Check for specific data patterns
    specific_patterns = {
        "deadline": _DATE_VALUE_RE,
        "gpa": _GPA_VALUE_RE,
        "ielts": re.compile(r"\bIELTS[^.]{0,30}\d+[.,]?\d*\b", re.IGNORECASE),
        "toefl": re.compile(r"\bTOEFL[^.]{0,30}\d{2,3}\b", re.IGNORECASE),
        "ects": _ECTS_VALUE_RE,
        "fee": re.compile(r"\b\d{1,5}(?:[.,]\d{1,2})?\s*(?:EUR|€|Euro)\b", re.IGNORECASE),
    }

    # Check German-specific patterns
    german_patterns = {
        "bewerbungsfrist": _GERMAN_DEADLINE_ENHANCED_RE,
        "mindestnote": _GERMAN_GPA_ENHANCED_RE,
        "semester_intake": _GERMAN_SEMESTER_INTAKE_RE,
    }

    found_count = 0
    for field_name, pattern in specific_patterns.items():
        if pattern.search(all_content):
            validation["specific_fields"].append(field_name)
            found_count += 1

    # Check German patterns
    german_found = 0
    for field_name, pattern in german_patterns.items():
        if pattern.search(all_content):
            validation["specific_fields"].append(f"german_{field_name}")
            german_found += 1

    validation["german_data_found"] = german_found > 0

    # Check against required fields
    if required_fields:
        for field in required_fields:
            field_id = str(field.get("id", "")).lower()
            field_found = False

            if "deadline" in field_id and ("deadline" in validation["specific_fields"] or "german_bewerbungsfrist" in validation["specific_fields"]):
                field_found = True
            elif "gpa" in field_id and ("gpa" in validation["specific_fields"] or "german_mindestnote" in validation["specific_fields"]):
                field_found = True
            elif "language" in field_id and ("ielts" in validation["specific_fields"] or "toefl" in validation["specific_fields"]):
                field_found = True
            elif "ects" in field_id and "ects" in validation["specific_fields"]:
                field_found = True
            elif "fee" in field_id and "fee" in validation["specific_fields"]:
                field_found = True

            if not field_found:
                validation["missing_fields"].append(field_id)

    # Calculate specificity score
    total_patterns = len(specific_patterns) + len(german_patterns)
    total_found = found_count + german_found
    validation["specificity_score"] = total_found / max(1, total_patterns)

    # Adjust score based on required fields coverage
    if required_fields:
        coverage = 1.0 - (len(validation["missing_fields"]) / max(1, len(required_fields)))
        validation["specificity_score"] = (validation["specificity_score"] * 0.6) + (coverage * 0.4)

    min_score = float(getattr(settings.web_search, "evidence_specificity_min_score", 0.3))
    validation["has_specific_data"] = validation["specificity_score"] >= min_score

    # Add issues
    if not validation["has_specific_data"]:
        validation["issues"].append(f"Evidence specificity score {validation['specificity_score']:.2f} below threshold {min_score:.2f}")

    if len(validation["missing_fields"]) > len(required_fields) * 0.5 if required_fields else False:
        validation["issues"].append(f"Missing critical fields: {', '.join(validation['missing_fields'][:3])}")

    return validation


def _extract_german_structured_facts(text: str, url: str) -> dict:
    """Extract German university-specific structured data from content."""
    if not getattr(settings.web_search, "german_specific_extraction_enabled", True):
        return {}

    facts = {}

    # Deadline extraction (German format)
    deadline_match = _GERMAN_DEADLINE_ENHANCED_RE.search(text)
    if deadline_match:
        facts['deadline_de'] = deadline_match.group(2).strip()
        facts['deadline_context'] = deadline_match.group(1).strip()

    # GPA extraction (German grading system)
    gpa_match = _GERMAN_GPA_ENHANCED_RE.search(text)
    if gpa_match:
        facts['gpa_threshold_de'] = gpa_match.group(2).strip()
        facts['gpa_context'] = gpa_match.group(1).strip()

    # Semester intake extraction
    semester_match = _GERMAN_SEMESTER_INTAKE_RE.search(text)
    if semester_match:
        facts['semester_intake'] = f"{semester_match.group(1)} {semester_match.group(2)}"

    # Module extraction from Modulhandbuch PDFs
    if 'modulhandbuch' in url.lower() or 'modulhandbuch' in text.lower()[:500]:
        modules = []
        for match in _MODULHANDBUCH_SECTION_RE.finditer(text[:5000]):
            modules.append({
                'module_id': match.group(1).strip(),
                'module_name': match.group(2).strip()
            })
        if modules:
            facts['modules_extracted'] = modules[:10]

    # Mark source as German educational content
    if any(domain in url.lower() for domain in ['uni-', 'tu-', 'fh-', 'hs-', 'daad.de']):
        facts['is_german_official'] = True

    return facts


def _domains_from_site_filters(queries: list[str]) -> list[str]:
    domains: list[str] = []
    seen: set[str] = set()
    for query in queries:
        for match in re.findall(r"\bsite:([a-z0-9.-]+\.[a-z]{2,})\b", str(query).lower()):
            domain = str(match).strip().lower().rstrip(").,;:")
            if not domain or domain in seen:
                continue
            seen.add(domain)
            domains.append(domain)
    return domains


def _admissions_required_field_ids_from_context() -> set[str]:
    return {
        str(item).strip()
        for item in _RETRIEVAL_REQUIRED_FIELD_IDS_CTX.get(())
        if str(item).strip()
    }


def _search_policy_for_current_request(query_variants: list[str], *, top_k: int) -> dict:
    query = " ".join(str(_RETRIEVAL_QUERY_CTX.get("") or "").split()).strip()
    strict_official = bool(_RETRIEVAL_STRICT_OFFICIAL_CTX.get(False))
    target_domains = [
        str(item).strip().lower()
        for item in _RETRIEVAL_TARGET_DOMAINS_CTX.get(())
        if str(item).strip()
    ]
    required_ids = _admissions_required_field_ids_from_context()
    high_precision = bool(required_ids & _ADMISSIONS_HIGH_PRECISION_FIELD_IDS) or (
        bool(query)
        and _is_admissions_high_precision_query(
            query,
            [{"id": item} for item in sorted(required_ids)],
        )
    )

    include_domains: list[str] = []
    seen_domains: set[str] = set()
    for domain in (
        _domains_from_site_filters(query_variants)
        + target_domains
        + _normalized_official_source_allowlist()
        + _official_domains_for_query(query)
    ):
        normalized = _domain_group_key(str(domain).strip().lower())
        if not normalized or normalized in seen_domains:
            continue
        seen_domains.add(normalized)
        include_domains.append(normalized)
    if strict_official:
        include_domains = include_domains[:8]
    elif not _is_deep_search_mode() and include_domains:
        include_domains = include_domains[:2]
    elif high_precision:
        include_domains = include_domains[:5]
    else:
        include_domains = []

    # Deep mode needs richer context; fast/standard keep lightweight snippets.
    include_raw_content: bool | str | None = "markdown" if _is_deep_search_mode() else None

    topic: str | None = None
    time_range: str | None = None
    if _NEWS_HINT_RE.search(query):
        topic = "news"
        time_range = "day" if re.search(r"\b(today|latest)\b", query, flags=re.IGNORECASE) else "week"
    elif bool(re.search(r"\b(deadline|closing date|apply by|intake)\b", query, flags=re.IGNORECASE)):
        # Deadline pages can change yearly; bias towards fresher documents.
        time_range = "year"

    num_results = _default_num_for_mode(top_k)
    if _is_deep_search_mode() and high_precision:
        num_results = max(num_results, min(10, int(settings.web_search.default_num) + 2))

    return {
        "num_results": num_results,
        "topic": topic,
        "time_range": time_range,
        "include_raw_content": include_raw_content,
        "include_answer": False,
        "include_domains": include_domains,
        "exclude_domains": [],
    }


async def _asearch_payloads(query_variants: list[str], *, top_k: int) -> list[dict]:
    if not query_variants:
        return []

    search_depth = _search_depth_for_mode()
    policy = _search_policy_for_current_request(query_variants, top_k=top_k)
    num_results = max(1, int(policy.get("num_results", _default_num_for_mode(top_k)) or 1))
    multi_query_enabled = bool(getattr(settings.web_search, "multi_query_enabled", False))
    if not multi_query_enabled:
        payloads: list[dict] = []
        for query in query_variants:
            payload = await asearch_google(
                query,
                gl=settings.web_search.default_gl,
                hl=settings.web_search.default_hl,
                num=num_results,
                search_depth=search_depth,
                topic=policy.get("topic"),
                time_range=policy.get("time_range"),
                include_raw_content=policy.get("include_raw_content"),
                include_answer=policy.get("include_answer"),
                include_domains=policy.get("include_domains"),
                exclude_domains=policy.get("exclude_domains"),
            )
            if isinstance(payload, dict) and payload:
                payloads.append(payload)
        return payloads

    batch = await asearch_google_batch(
        query_variants,
        gl=settings.web_search.default_gl,
        hl=settings.web_search.default_hl,
        num=num_results,
        search_depth=search_depth,
        topic=policy.get("topic"),
        time_range=policy.get("time_range"),
        include_raw_content=policy.get("include_raw_content"),
        include_answer=policy.get("include_answer"),
        include_domains=policy.get("include_domains"),
        exclude_domains=policy.get("exclude_domains"),
    )

    payloads: list[dict] = []
    first_error = ""
    for item in batch:
        if not isinstance(item, dict):
            continue
        error = str(item.get("error", "")).strip()
        if error and not first_error:
            first_error = error
        payload = item.get("result")
        if isinstance(payload, dict) and payload:
            payloads.append(payload)
    if payloads:
        return payloads
    if first_error:
        hard_error = bool(
            re.search(
                r"\b(exceeds your plan|usage limit|quota|insufficient credits|invalid api key|unauthorized|forbidden|401|403)\b",
                first_error,
                flags=re.IGNORECASE,
            )
        )
        emit_trace_event(
            "search_batch_failed",
            {
                "query_count": len(query_variants),
                "search_depth": search_depth,
                "recoverable": not hard_error,
                "error": first_error[:220],
            },
        )
        if hard_error:
            raise RuntimeError(first_error)
    return []


def _collect_search_rows(
    payloads: list[dict],
    query_variants: list[str],
    *,
    top_k: int,
    allowed_suffixes: list[str],
    strict_official: bool = False,
    target_domain_groups: list[str] | None = None,
    enforce_target_domain_scope: bool = False,
) -> list[dict]:
    per_query_limit = _default_num_for_mode(top_k)
    merged_rows: list[dict] = []
    for payload in payloads:
        merged_rows.extend(_organic_rows(payload, limit=per_query_limit))
    dedupe_limit = max(top_k, _max_context_results_for_mode()) * max(1, len(query_variants))
    rows = _dedupe_rows(merged_rows, limit=dedupe_limit)
    source_decisions = _source_filter_decisions(
        rows,
        allowed_suffixes=allowed_suffixes,
        strict_official=strict_official,
    )
    if source_decisions:
        emit_trace_event(
            "source_filter_evaluated",
            {
                "query_count": len(query_variants),
                "allowed_suffixes": allowed_suffixes,
                "strict_official": strict_official,
                **_source_filter_summary(source_decisions),
            },
        )
    rows = _filter_rows_by_allowed_domains_with_policy(
        rows,
        allowed_suffixes,
        strict_official=strict_official,
    )
    strict_admissions_filter = _is_high_precision_admissions_context() and _is_university_program_query(
        _current_retrieval_query()
    )
    if target_domain_groups:
        rows = _filter_rows_by_target_domain_groups(
            rows,
            target_groups=target_domain_groups,
            allow_fallback_on_empty=not (enforce_target_domain_scope or strict_admissions_filter),
        )
    rows = _filter_rows_by_program_scope(
        rows,
        allow_fallback_on_empty=not (enforce_target_domain_scope or strict_admissions_filter),
    )
    rows = _filter_rows_for_admissions_precision(
        rows,
        allow_fallback_on_empty=not (enforce_target_domain_scope or strict_admissions_filter),
    )
    return rows


def _collect_search_rows_with_domain_retry(
    payloads: list[dict],
    query_variants: list[str],
    *,
    top_k: int,
    allowed_suffixes: list[str],
    strict_official: bool = False,
    target_domain_groups: list[str] | None = None,
    enforce_target_domain_scope: bool = False,
) -> tuple[list[dict], bool]:
    """Collect rows with strict source filtering and no domain-relax fallback."""
    rows = _collect_search_rows(
        payloads,
        query_variants,
        top_k=top_k,
        allowed_suffixes=allowed_suffixes,
        strict_official=strict_official,
        target_domain_groups=target_domain_groups,
        enforce_target_domain_scope=enforce_target_domain_scope,
    )
    return rows, False


def _ai_overview_candidate(payloads: list[dict], allowed_suffixes: list[str]) -> dict | None:
    if allowed_suffixes:
        return None
    for payload in payloads:
        ai_text = _ai_overview_text(payload)
        if not ai_text:
            continue
        return {
            "_score": 1.5,
            "chunk_id": "web:ai_overview",
            "source_path": "web_search://google/ai_overview",
            "distance": 0.0,
            "content": ai_text[: settings.web_search.max_page_chars],
            "metadata": {
                "university": "Google AI Overview",
                "title": "Google AI Overview",
                "section_heading": "Web Fallback",
                "url": "",
                "published_date": "",
                "source_type": "google_ai_overview",
            },
        }
    return None


def _page_text_and_date(page_payload) -> tuple[str, str]:
    if isinstance(page_payload, dict):
        page_text = str(page_payload.get("content", "")).strip()
        page_published_date = str(page_payload.get("published_date", "")).strip()
        return page_text, page_published_date
    # Backward-compatible with old test doubles returning a plain page text string.
    return str(page_payload).strip(), ""


def _row_chunk_texts(*, title: str, url: str, snippet: str, page_text: str) -> list[str]:
    chunk_texts = _chunk_clean_text(page_text) if page_text else []
    if snippet and not chunk_texts:
        chunk_texts = [snippet]
    if chunk_texts:
        return chunk_texts
    fallback_text = title or url
    if fallback_text:
        return [fallback_text]
    return []


def _ranked_page_candidates(
    *,
    chunk_texts: list[str],
    query_tokens: set[str],
    title: str,
    url: str,
    snippet: str,
    rank_index: int,
) -> list[tuple[float, int, str]]:
    page_candidates: list[tuple[float, int, str]] = []
    for chunk_index, chunk_text in enumerate(chunk_texts, start=1):
        content = str(chunk_text).strip()
        if not content:
            continue
        score = _chunk_relevance_score(
            query_tokens=query_tokens,
            title=title,
            url=url,
            content=content,
            snippet=snippet,
            rank_index=rank_index,
        )
        page_candidates.append((score, chunk_index, content))
    page_candidates.sort(key=lambda item: item[0], reverse=True)
    return page_candidates


def _organic_row_candidates(
    *,
    index: int,
    row: dict,
    page_data_by_url: dict[str, dict],
    allowed_suffixes: list[str],
    query_tokens: set[str],
    strict_official: bool = False,
    target_domain_groups: list[str] | None = None,
    enforce_target_domain_scope: bool = False,
) -> list[dict]:
    title = str(row.get("title", "")).strip()
    url = str(row.get("url", "")).strip()
    snippet = str(row.get("snippet", "")).strip()
    if not _source_url_allowed(
        url=url,
        title=title,
        snippet=snippet,
        allowed_suffixes=allowed_suffixes,
        strict_official=strict_official,
    ):
        return []
    if enforce_target_domain_scope and not _url_matches_target_domain_scope(url, target_domain_groups):
        return []

    row_published_date = str(row.get("published_date", "")).strip()
    page_text, page_published_date = _page_text_and_date(page_data_by_url.get(url, {}))
    if not _passes_degree_level_lock(
        title=title,
        url=url,
        snippet=snippet,
        content=page_text,
    ):
        return []
    if not _passes_program_scope_lock(
        title=title,
        url=url,
        snippet=snippet,
        content=page_text,
    ):
        return []
    published_date = row_published_date or page_published_date

    chunk_texts = _row_chunk_texts(
        title=title,
        url=url,
        snippet=snippet,
        page_text=page_text,
    )
    if not chunk_texts:
        return []

    max_chunks = _max_chunks_per_page_for_mode()
    ranked = _ranked_page_candidates(
        chunk_texts=chunk_texts,
        query_tokens=query_tokens,
        title=title,
        url=url,
        snippet=snippet,
        rank_index=index,
    )
    candidates: list[dict] = []
    for score, chunk_index, content in ranked[:max_chunks]:
        candidates.append(
            {
                "_score": score,
                "chunk_id": f"web:organic:{index}:{chunk_index}",
                "source_path": url or f"web_search://google/organic/{index}",
                "distance": round(max(0.0, 1.0 - min(1.0, score)), 4),
                "content": content[: settings.web_search.max_page_chars],
                "metadata": {
                    "university": title or _host_label(url),
                    "title": title or _host_label(url),
                    "section_heading": "Web Result",
                    "url": url,
                    "published_date": published_date,
                    "source_type": "google_organic",
                },
            }
        )
    return candidates


def _build_organic_candidates(
    *,
    rows: list[dict],
    page_data_by_url: dict[str, dict],
    query_tokens: set[str],
    allowed_suffixes: list[str],
    strict_official: bool = False,
    target_domain_groups: list[str] | None = None,
    enforce_target_domain_scope: bool = False,
) -> list[dict]:
    candidates: list[dict] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        candidates.extend(
            _organic_row_candidates(
                index=index,
                row=row,
                page_data_by_url=page_data_by_url,
                allowed_suffixes=allowed_suffixes,
                query_tokens=query_tokens,
                strict_official=strict_official,
                target_domain_groups=target_domain_groups,
                enforce_target_domain_scope=enforce_target_domain_scope,
            )
        )
    return candidates


def _finalize_candidates(candidates: list[dict]) -> list[dict]:
    candidates.sort(
        key=lambda item: float(item.get("_final_score", item.get("_score", 0.0))), reverse=True
    )
    deduped = _dedupe_chunk_candidates(candidates)
    max_results = _max_context_results_for_mode()
    min_unique_domains = _retrieval_min_unique_domains()

    selected_indexes: set[int] = set()
    selected_domains: set[str] = set()
    ordered_items: list[dict] = []

    if min_unique_domains > 1:
        for index, item in enumerate(deduped):
            metadata = item.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            domain = _domain_group_key(_normalized_host(str(metadata.get("url", "")))
            )
            if not domain or domain in selected_domains:
                continue
            selected_domains.add(domain)
            selected_indexes.add(index)
            ordered_items.append(item)
            if len(ordered_items) >= max_results or len(selected_domains) >= min_unique_domains:
                break

    for index, item in enumerate(deduped):
        if index in selected_indexes:
            continue
        ordered_items.append(item)
        if len(ordered_items) >= max_results:
            break

    results: list[dict] = []
    for item in ordered_items[:max_results]:
        cleaned = dict(item)
        cleaned.pop("_score", None)
        cleaned.pop("_trust_score", None)
        cleaned.pop("_final_score", None)
        results.append(cleaned)
    return results


def _fact_text_from_content(content: str) -> str:
    text = " ".join(str(content or "").split()).strip()
    if not text:
        return ""
    sentences = [item.strip() for item in _SENTENCE_SPLIT_RE.split(text) if item.strip()]
    if not sentences:
        sentences = [text]

    def _score(sentence: str) -> float:
        score = 0.0
        lowered = sentence.lower()
        if _NUMERIC_TOKEN_RE.search(lowered):
            score += 0.9
        if _DATE_VALUE_RE.search(lowered):
            score += 0.7
        if _DEADLINE_CONTENT_RE.search(lowered):
            score += 0.9
        if _LANGUAGE_CONTENT_RE.search(lowered):
            score += 0.8
        if _DURATION_ECTS_CONTENT_RE.search(lowered):
            score += 0.8
        if _ADMISSION_CONTENT_RE.search(lowered):
            score += 0.7
        if _CURRICULUM_CONTENT_RE.search(lowered):
            score += 0.5
        if _TUITION_CONTENT_RE.search(lowered):
            score += 0.6
        if len(sentence) < 25:
            score -= 0.3
        return score

    sentence = max(sentences[:8], key=_score)
    return sentence[:280].strip()


def _extract_facts(candidates: list[dict], *, limit: int) -> list[dict]:
    facts: list[dict] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        metadata = candidate.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        fact_text = _fact_text_from_content(candidate.get("content", ""))
        if not fact_text:
            continue
        key = fact_text.lower()
        if key in seen:
            continue
        seen.add(key)
        facts.append(
            {
                "fact": fact_text,
                "url": str(metadata.get("url", "")).strip(),
                "title": str(metadata.get("title", "")).strip(),
                "published_date": str(metadata.get("published_date", "")).strip(),
                "trust_score": float(metadata.get("trust_score", 0.0) or 0.0),
            }
        )
        if len(facts) >= limit:
            break
    return facts


def _normalized_host(url: str) -> str:
    host = str(urlparse(url).hostname or "").strip().lower()
    if host.startswith("www."):
        return host[4:]
    return host


def _unique_domains_from_candidates(candidates: list[dict]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        metadata = candidate.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        host = _domain_group_key(_normalized_host(str(metadata.get("url", ""))))
        if not host or host in seen:
            continue
        seen.add(host)
        ordered.append(host)
    return ordered


def _retrieval_min_unique_domains() -> int:
    configured = max(1, int(getattr(settings.web_search, "retrieval_min_unique_domains", 2)))
    if not _is_deep_search_mode():
        return configured
    if bool(_RETRIEVAL_STRICT_OFFICIAL_CTX.get(False)) and bool(_RETRIEVAL_TARGET_DOMAINS_CTX.get(())):
        # For strict university admissions mode we intentionally stay inside the official domain scope.
        return 1
    deep_override = max(1, int(getattr(settings.web_search, "deep_min_unique_domains", configured)))
    return max(configured, deep_override)


def _domain_diversity_gap(unique_domains: list[str]) -> int:
    return max(0, _retrieval_min_unique_domains() - len(unique_domains))


def _domain_gap_subquestions(unique_domains: list[str]) -> list[str]:
    gap = _domain_diversity_gap(unique_domains)
    if gap <= 0:
        return []
    return [f"confirm with at least {gap} additional independent website(s)"]


def _build_domain_gap_queries(query: str, unique_domains: list[str]) -> list[str]:
    gap = _domain_diversity_gap(unique_domains)
    if gap <= 0:
        return []
    candidates: list[str] = []
    seen_domains = {
        _domain_group_key(str(host).strip().lower())
        for host in unique_domains
        if str(host).strip()
    }
    seen_domains.discard("")
    for entity in _comparison_entities_from_query(query):
        entity_focus = _entity_focus_query(entity)
        if not entity_focus:
            continue
        for domain in _official_domains_for_query(entity)[:1]:
            grouped = _domain_group_key(domain)
            if grouped in seen_domains:
                continue
            candidates.append(f"{entity_focus} site:{domain}")
    for domain in _official_domains_for_query(query):
        grouped = _domain_group_key(domain)
        if grouped in seen_domains:
            continue
        candidates.append(f"{query} admission requirements application deadline site:{domain}")
    for _ in range(gap):
        candidates.extend(
            [
                f"{query} official source",
                f"{query} independent source",
                f"{query} corroborated information",
            ]
        )
    return _normalize_query_list(
        candidates,
        limit=max(1, int(getattr(settings.web_search, "retrieval_loop_max_gap_queries", 2))),
    )


def _combine_missing_subquestions(base_missing: list[str], unique_domains: list[str]) -> list[str]:
    return _normalize_subquestion_list(
        list(base_missing) + _domain_gap_subquestions(unique_domains),
        limit=max(_max_planner_subquestions(), _retrieval_min_unique_domains() + 2),
    )


def _build_required_field_queries(
    query: str,
    *,
    missing_required_fields: list[dict],
    allowed_suffixes: list[str],
    unique_domains: list[str],
) -> list[str]:
    if not missing_required_fields:
        return []

    candidates: list[str] = []
    field_query_buckets: list[list[str]] = []
    high_precision = _is_admissions_high_precision_query(
        query,
        missing_required_fields,
    ) and _is_university_program_query(query)
    query_base = _compact_query_keywords(query) or " ".join(str(query).split()).strip()
    if not query_base:
        query_base = " ".join(str(query).split()).strip()
    ordered_missing_fields = sorted(
        missing_required_fields,
        key=lambda field: (
            -int(
                _REQUIRED_FIELD_QUERY_PRIORITY.get(
                    str((field or {}).get("id", "")).strip(),
                    0,
                )
            ),
            str((field or {}).get("id", "")).strip(),
        ),
    )
    official_domains = _official_domains_for_query(query)[:4]
    domain_candidates: list[str] = []
    domain_seen: set[str] = set()
    for domain in list(unique_domains) + official_domains:
        grouped = _domain_group_key(str(domain).strip().lower())
        if not grouped or grouped in domain_seen:
            continue
        if not (_host_looks_official_institution(grouped) or _host_is_acronym_like(grouped)):
            continue
        domain_seen.add(grouped)
        domain_candidates.append(grouped)
    if high_precision and domain_candidates:
        domain_candidates = domain_candidates[:1]

    suffix_scope = ""
    if allowed_suffixes:
        suffix_scope = " (" + " OR ".join(f"site:{suffix}" for suffix in allowed_suffixes[:2]) + ")"

    targeted_focus_by_field: dict[str, list[str]] = {
        "admission_requirements": [
            "admission requirements eligibility criteria required documents",
            "prerequisite credits bachelor degree requirements",
        ],
        "gpa_threshold": [
            "minimum GPA grade threshold grading scale required score",
            "minimum final grade admission criteria",
        ],
        "ects_breakdown": [
            "required ECTS credits in mathematics computer science prerequisite modules",
            "prerequisite credit breakdown by subject area",
        ],
        "instruction_language": [
            "language of instruction teaching language taught in",
            "unterrichtssprache programm",
        ],
        "language_requirements": [
            "english language requirements IELTS TOEFL CEFR minimum score",
            "accepted language certificates and minimum scores",
        ],
        "language_score_thresholds": [
            "IELTS TOEFL CEFR minimum score exact thresholds",
            "accepted English test score requirements exact values",
        ],
        "application_deadline": [
            "application deadline exact dates apply by closing date intake timeline",
            "application period start date end date winter semester summer semester",
        ],
        "application_portal": [
            "official application portal URL where to apply",
            "apply online portal application system official page",
        ],
        "duration_ects": [
            "program duration semesters years total ECTS credits",
            "standard period of study semesters and credit points",
        ],
        "curriculum_modules": [
            "curriculum structure core modules module handbook regulations",
            "study and examination regulations program modules",
        ],
        "tuition_fees": [
            "tuition fees semester contribution exact amount EUR",
            "study costs and semester fees for international students",
        ],
    }

    for field in ordered_missing_fields:
        field_id = str(field.get("id", "")).strip()
        field_candidates: list[str] = []
        if high_precision and field_id in {
            "admission_requirements",
            "gpa_threshold",
            "ects_breakdown",
            "instruction_language",
            "language_requirements",
            "language_score_thresholds",
            "application_deadline",
            "application_portal",
        }:
            for domain in domain_candidates:
                field_candidates.append(
                    f"{query_base} official selection statute admission pdf site:{domain}"
                )
                field_candidates.append(
                    f"{query_base} official language of instruction taught in site:{domain}"
                )
                field_candidates.append(
                    f"{query_base} official foreign language requirements site:{domain}"
                )
                field_candidates.append(
                    f"{query_base} official auswahlsatzung zulassung filetype:pdf site:{domain}"
                )
                field_candidates.append(
                    f"{query_base} IELTS TOEFL minimum score official site:{domain}"
                )
                field_candidates.append(
                    f"{query_base} official application deadlines international students site:{domain}"
                )
                field_candidates.append(
                    f"{query_base} official apply online portal site:{domain}"
                )
        focus = " ".join(str(field.get("query_focus", "")).split()).strip()
        focus_items: list[str] = []
        focus_items.extend(list(_REQUIRED_FIELD_SOURCE_ROUTE_HINTS.get(field_id, ())))
        if focus:
            focus_items.append(focus)
        focus_items.extend(targeted_focus_by_field.get(field_id, []))
        normalized_focus_items = _normalize_query_list(
            focus_items,
            limit=max(3, len(focus_items)),
        )
        prioritize_pdf = field_id in _REQUIRED_FIELD_PDF_PRIORITY_IDS
        for focus_item in normalized_focus_items:
            normalized_focus = " ".join(str(focus_item).split()).strip()
            if not normalized_focus:
                continue
            field_candidates.append(f"{query_base} {normalized_focus} official source")
            if prioritize_pdf:
                field_candidates.append(f"{query_base} {normalized_focus} official pdf")
            for domain in domain_candidates:
                field_candidates.append(f"{query_base} {normalized_focus} site:{domain}")
                if prioritize_pdf:
                    field_candidates.append(f"{query_base} {normalized_focus} pdf site:{domain}")
            if suffix_scope:
                field_candidates.append(f"{query_base} {normalized_focus}{suffix_scope}")
        field_query_buckets.append(
            _normalize_query_list(
                field_candidates,
                limit=18 if high_precision else 6,
            )
        )

    if field_query_buckets:
        max_bucket_len = max(len(bucket) for bucket in field_query_buckets)
        for idx in range(max_bucket_len):
            for bucket in field_query_buckets:
                if idx >= len(bucket):
                    continue
                candidates.append(bucket[idx])

    max_gap_queries = max(1, int(getattr(settings.web_search, "retrieval_loop_max_gap_queries", 2)))
    if high_precision:
        candidate_limit = min(40, max(24, len(missing_required_fields) * 8))
    else:
        candidate_limit = min(
            24,
            max_gap_queries * max(3, min(6, len(missing_required_fields) + 2)),
        )
    return _normalize_query_list(
        candidates,
        limit=candidate_limit,
    )


def _build_follow_up_queries(
    query: str,
    *,
    missing_subquestions: list[str],
    llm_gap_queries: list[str],
    missing_required_fields: list[dict],
    missing_research_objectives: list[dict] | None = None,
    allowed_suffixes: list[str],
    unique_domains: list[str],
) -> list[str]:
    max_gap_queries = max(1, int(getattr(settings.web_search, "retrieval_loop_max_gap_queries", 2)))
    high_precision = _is_admissions_high_precision_query(query, missing_required_fields)
    candidate_limit = min(42 if high_precision else 28, max_gap_queries * (8 if high_precision else 6))
    required_field_queries = _build_required_field_queries(
        query,
        missing_required_fields=missing_required_fields,
        allowed_suffixes=allowed_suffixes,
        unique_domains=unique_domains,
    )
    research_objective_queries = _build_research_objective_queries(
        query,
        missing_objectives=missing_research_objectives or [],
        unique_domains=unique_domains,
    )
    heuristic_queries = _build_gap_queries(query, missing_subquestions)
    domain_queries = _build_domain_gap_queries(query, unique_domains)
    if llm_gap_queries:
        return _normalize_query_list(
            required_field_queries + research_objective_queries + llm_gap_queries + domain_queries,
            limit=candidate_limit,
        )
    return _normalize_query_list(
        required_field_queries + research_objective_queries + heuristic_queries + domain_queries,
        limit=candidate_limit,
    )


def _deterministic_university_route_queries(query: str, required_fields: list[dict]) -> list[str]:
    if not required_fields:
        return []
    required_ids = {
        str(field.get("id", "")).strip()
        for field in required_fields
        if str(field.get("id", "")).strip()
    }
    query_base = _compact_query_keywords(query) or " ".join(str(query).split()).strip()
    if not query_base:
        return []
    official_domains = _official_domains_for_query(query)[:2]
    primary_domain = official_domains[0] if official_domains else ""
    domain_suffix = f" site:{primary_domain}" if primary_domain else ""
    signature = _program_focus_signature(query)
    institution_phrase = " ".join(sorted(signature.get("institution", set()) or set())).strip()
    subject_phrase = " ".join(sorted(signature.get("subject", set()) or set())).strip()
    queries: list[str] = _build_official_source_route_queries(
        query,
        required_fields,
        max_queries=8,
    )
    has_admissions_slots = bool(
        required_ids
        & {
            "admission_requirements",
            "gpa_threshold",
            "ects_breakdown",
            "instruction_language",
            "language_requirements",
            "language_score_thresholds",
            "application_deadline",
            "application_portal",
        }
    )
    if has_admissions_slots:
        # Deterministic admissions routes across universities: program page, statute PDF,
        # language requirements, deadline page, and application portal.
        queries.append(f"{query_base} official program page master site:{primary_domain}" if primary_domain else f"{query_base} official program page master")
        queries.append(
            f"{query_base} official selection statute auswahlsatzung admission filetype:pdf{domain_suffix}"
        )
        queries.append(
            f"{query_base} official language of instruction foreign language requirements IELTS TOEFL minimum score{domain_suffix}"
        )
        queries.append(
            f"{query_base} official application deadline international students bewerbungsfrist{domain_suffix}"
        )
        queries.append(f"{query_base} official application portal apply online{domain_suffix}")
    if required_ids & {"admission_requirements", "gpa_threshold", "ects_breakdown"}:
        queries.append(
            f"{query_base} official selection statute admission requirements minimum grade ECTS{domain_suffix}"
        )
    if required_ids & {"instruction_language", "language_requirements", "language_score_thresholds"}:
        queries.append(
            f"{query_base} official language of instruction foreign language requirements IELTS TOEFL minimum score{domain_suffix}"
        )
    if required_ids & {"application_deadline", "application_portal"}:
        queries.append(
            f"{query_base} official application deadlines international students apply online portal{domain_suffix}"
        )
    if required_ids & {"duration_ects", "curriculum_modules"}:
        queries.append(
            f"{query_base} official program handbook duration semesters ECTS module regulations{domain_suffix}"
        )
    if required_ids & {"tuition_fees"}:
        queries.append(f"{query_base} official tuition fees semester contribution{domain_suffix}")
    if primary_domain and institution_phrase and subject_phrase:
        queries.append(
            f"\"{institution_phrase}\" \"{subject_phrase}\" master selection statute filetype:pdf site:{primary_domain}"
        )
        queries.append(
            f"\"{institution_phrase}\" \"{subject_phrase}\" admission requirements minimum grade ECTS site:{primary_domain}"
        )
        queries.append(
            f"\"{institution_phrase}\" \"{subject_phrase}\" language of instruction taught in site:{primary_domain}"
        )
        queries.append(
            f"\"{institution_phrase}\" \"{subject_phrase}\" language requirements IELTS TOEFL site:{primary_domain}"
        )
        queries.append(
            f"\"{institution_phrase}\" \"{subject_phrase}\" application deadline international site:{primary_domain}"
        )
    for domain in official_domains:
        if required_ids & {"application_portal"}:
            queries.append(f"{query_base} apply online application portal site:{domain}")
    if primary_domain:
        queries.append(f"{query_base} auswahlsatzung wirtschaftsinformatik filetype:pdf site:{primary_domain}")
        queries.append(f"{query_base} bewerbungsfrist master wirtschaftsinformatik site:{primary_domain}")
    return _normalize_query_list(queries, limit=16 if has_admissions_slots else 12)


def _next_loop_queries(
    *,
    base_query: str,
    initial_queries: list[str],
    first_wave_queries: list[str],
    missing_subquestions: list[str],
    llm_gap_queries: list[str],
    follow_up_queries: list[str],
    seen_queries: set[str],
    loop_step: int,
    deep_mode: bool,
) -> list[str]:
    if loop_step <= 1:
        if not deep_mode:
            return _next_queries_for_loop(
                initial_queries,
                seen_queries,
                max_queries=min(2, _max_planner_queries()),
            )
        first_wave_limit = max(
            _max_planner_queries(),
            _max_planner_queries() + max(
                1, int(getattr(settings.web_search, "retrieval_loop_max_gap_queries", 2))
            ),
        )
        first_wave_queries = _normalize_query_list(
            list(initial_queries) + list(first_wave_queries),
            limit=first_wave_limit,
        )
        return _next_queries_for_loop(
            first_wave_queries,
            seen_queries,
            max_queries=first_wave_limit,
        )
    gap_queries = (
        llm_gap_queries or follow_up_queries or _build_gap_queries(base_query, missing_subquestions)
    )
    return _next_queries_for_loop(
        gap_queries,
        seen_queries,
        max_queries=max(1, int(getattr(settings.web_search, "retrieval_loop_max_gap_queries", 2))),
    )


def _retrieval_loop_enabled() -> bool:
    return _is_deep_search_mode() and bool(getattr(settings.web_search, "retrieval_loop_enabled", True))


def _required_field_rescue_enabled() -> bool:
    return _is_deep_search_mode() and bool(
        getattr(settings.web_search, "deep_required_field_rescue_enabled", True)
    )


def _required_field_rescue_max_queries() -> int:
    configured = int(getattr(settings.web_search, "deep_required_field_rescue_max_queries", 6) or 6)
    return max(1, min(12, configured))


def _required_field_coverage_target(query: str, required_fields: list[dict]) -> float:
    if not required_fields:
        return 1.0
    configured = float(getattr(settings.web_search, "deep_required_field_min_coverage", 0.85) or 0.85)
    target = max(0.5, min(1.0, configured))
    if _is_admissions_high_precision_query(query, required_fields):
        target = max(target, 0.9)
    if _is_university_program_query(query) and len(required_fields) >= 4:
        # Explicit multi-field university queries should close all requested fields in deep mode.
        target = 1.0
    return target


def _target_domain_coverage_count(unique_domains: list[str], target_domain_groups: list[str]) -> int:
    if not target_domain_groups:
        return 0
    target_set = {str(item).strip().lower() for item in target_domain_groups if str(item).strip()}
    if not target_set:
        return 0
    count = 0
    for domain in unique_domains:
        grouped = _domain_group_key(str(domain))
        if grouped in target_set:
            count += 1
    return count


def _effective_retrieval_loop_max_steps(query: str, required_fields: list[dict], *, deep_mode: bool) -> int:
    base = max(1, int(getattr(settings.web_search, "retrieval_loop_max_steps", 2)))
    if not deep_mode:
        return 1
    boost = 0
    if len(required_fields) >= 4:
        boost += 1
    if _is_university_program_query(query):
        boost += 1
    if _is_admissions_high_precision_query(query, required_fields):
        boost += 1
    if _is_university_program_query(query) and len(required_fields) >= 4:
        boost += 1
    return max(1, min(8, base + boost))


def _retrieval_loop_max_stagnant_steps() -> int:
    configured = int(getattr(settings.web_search, "retrieval_loop_max_stagnant_steps", 1) or 1)
    return max(0, min(3, configured))


def _deep_standard_first_enabled() -> bool:
    return bool(getattr(settings.web_search, "deep_standard_first_enabled", True))


def _deterministic_controller_enabled() -> bool:
    return bool(getattr(settings.web_search, "deterministic_controller_enabled", True))


def _deep_escalate_only_if_unresolved() -> bool:
    return bool(getattr(settings.web_search, "deep_escalate_only_if_unresolved", True))


def _standard_search_max_queries() -> int:
    configured = int(getattr(settings.web_search, "standard_search_max_queries", 2) or 2)
    return max(1, min(8, configured))


def _deep_search_max_queries() -> int:
    configured = int(getattr(settings.web_search, "deep_search_max_queries", 3) or 3)
    return max(1, min(12, configured))


def _deep_extract_max_urls() -> int:
    configured = int(getattr(settings.web_search, "deep_extract_max_urls", 4) or 4)
    return max(0, min(20, configured))


def _retrieval_no_progress_cutoff() -> int:
    configured = int(getattr(settings.web_search, "retrieval_no_progress_cutoff", 1) or 1)
    return max(0, min(3, configured))


def _should_run_standard_first_pass(
    *,
    query: str,
    normalized_mode: str,
    required_fields: list[dict],
) -> bool:
    if not _is_deep_search_mode(normalized_mode):
        return False
    if not _deep_standard_first_enabled():
        return False
    if _is_admissions_high_precision_query(query, required_fields):
        required_ids = {
            str(field.get("id", "")).strip()
            for field in required_fields
            if str(field.get("id", "")).strip()
        }
        high_cost_core = {
            "admission_requirements",
            "gpa_threshold",
            "ects_breakdown",
            "application_deadline",
        }
        if len(required_ids & high_cost_core) >= 2 and len(required_ids) >= 5:
            return False
    # Cost optimization policy: for university/program asks, always try standard/basic first,
    # then escalate to deep only when unresolved.
    return _is_university_program_query(query)


async def _run_retrieval_with_context(
    query: str,
    *,
    top_k: int,
    mode: str,
    required_fields_ctx: list[dict],
    strict_official_ctx: bool,
    target_domain_groups_ctx: tuple[str, ...],
) -> dict:
    required_field_ids = tuple(
        str(field.get("id", "")).strip()
        for field in required_fields_ctx
        if str(field.get("id", "")).strip()
    )
    mode_token = _RETRIEVAL_MODE_CTX.set(_normalized_search_mode(mode))
    query_token = _RETRIEVAL_QUERY_CTX.set(" ".join(str(query or "").split()).strip())
    strict_token = _RETRIEVAL_STRICT_OFFICIAL_CTX.set(bool(strict_official_ctx))
    target_domains_token = _RETRIEVAL_TARGET_DOMAINS_CTX.set(target_domain_groups_ctx)
    required_ids_token = _RETRIEVAL_REQUIRED_FIELD_IDS_CTX.set(required_field_ids)
    try:
        return await _aretrieve_web_chunks_impl(
            query,
            top_k=top_k,
            search_mode=mode,
        )
    finally:
        _RETRIEVAL_REQUIRED_FIELD_IDS_CTX.reset(required_ids_token)
        _RETRIEVAL_TARGET_DOMAINS_CTX.reset(target_domains_token)
        _RETRIEVAL_STRICT_OFFICIAL_CTX.reset(strict_token)
        _RETRIEVAL_QUERY_CTX.reset(query_token)
        _RETRIEVAL_MODE_CTX.reset(mode_token)


def _required_field_status_from_result(required_fields: list[dict], result: dict) -> dict:
    rows = result.get("results")
    candidates = rows if isinstance(rows, list) else []
    normalized_candidates = [item for item in candidates if isinstance(item, dict)]
    return _required_field_coverage(required_fields, normalized_candidates)


def _result_quality_tuple(
    *,
    result: dict,
    required_fields: list[dict],
    target_domain_groups: tuple[str, ...],
) -> tuple[float, int, int, int]:
    rows = result.get("results")
    candidates = rows if isinstance(rows, list) else []
    normalized_candidates = [item for item in candidates if isinstance(item, dict)]
    required_status = _required_field_coverage(required_fields, normalized_candidates)
    coverage = float(required_status.get("coverage", 1.0) or 0.0)
    missing_count = len(required_status.get("missing_ids", []))
    unique_domains = _unique_domains_from_candidates(normalized_candidates)
    target_coverage_count = _target_domain_coverage_count(unique_domains, list(target_domain_groups))
    return (
        round(max(0.0, min(1.0, coverage)), 4),
        -missing_count,
        target_coverage_count,
        len(normalized_candidates),
    )


def _standard_pass_satisfies_requirements(
    *,
    query: str,
    standard_result: dict,
    required_fields: list[dict],
    target_domain_groups: tuple[str, ...],
    strict_official_sources: bool,
) -> bool:
    rows = standard_result.get("results")
    candidates = rows if isinstance(rows, list) else []
    if not candidates:
        return False
    if not required_fields:
        return True
    required_status = _required_field_status_from_result(required_fields, standard_result)
    coverage = float(required_status.get("coverage", 1.0) or 0.0)
    missing_ids = list(required_status.get("missing_ids", []))
    coverage_target = _required_field_coverage_target(query, required_fields)
    if missing_ids or coverage < coverage_target:
        return False
    if _is_admissions_high_precision_query(query, required_fields):
        content_chars = 0
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content_chars += len(" ".join(str(candidate.get("content", "")).split()).strip())
        if content_chars < 80:
            return False
    if strict_official_sources and target_domain_groups:
        unique_domains = _unique_domains_from_candidates(candidates)
        if _target_domain_coverage_count(unique_domains, list(target_domain_groups)) <= 0:
            return False
    return True


async def aretrieve_web_chunks(
    query: str,
    *,
    top_k: int = 3,
    search_mode: str = "deep",
) -> dict:
    normalized_mode = _normalized_search_mode(search_mode)
    deep_mode = _is_deep_search_mode(normalized_mode)
    required_fields = _required_fields_from_query(query) if deep_mode else []
    strict_official_sources = bool(
        deep_mode
        and (
            _is_admissions_high_precision_query(query, required_fields)
            or _is_university_program_query(query)
        )
    )
    target_domain_groups = (
        tuple(_target_domain_groups_for_query(query))
        if strict_official_sources
        else tuple()
    )
    if _should_run_standard_first_pass(
        query=query,
        normalized_mode=normalized_mode,
        required_fields=required_fields,
    ):
        standard_result: dict = {"results": []}
        standard_failed = False
        standard_error = ""
        try:
            standard_result = await _run_retrieval_with_context(
                query,
                top_k=top_k,
                mode="standard",
                required_fields_ctx=required_fields,
                strict_official_ctx=strict_official_sources,
                target_domain_groups_ctx=target_domain_groups,
            )
        except Exception as exc:
            standard_failed = True
            standard_error = " ".join(str(exc).split()).strip()
            logger.warning(
                "Standard-first pass failed; escalating to deep. query=%s error=%s",
                query,
                standard_error,
            )
            emit_trace_event(
                "standard_first_pass_failed",
                {
                    "query": query[:220],
                    "error": standard_error[:220],
                },
            )

        standard_satisfies = False
        if not standard_failed:
            standard_satisfies = _standard_pass_satisfies_requirements(
                query=query,
                standard_result=standard_result,
                required_fields=required_fields,
                target_domain_groups=target_domain_groups,
                strict_official_sources=strict_official_sources,
            )
        if standard_satisfies and _deep_escalate_only_if_unresolved():
            emit_trace_event(
                "standard_first_pass_completed",
                {
                    "query": query[:220],
                    "escalated_to_deep": False,
                    "reason": "standard_pass_satisfied",
                    "required_field_coverage": _required_field_status_from_result(
                        required_fields,
                        standard_result,
                    ).get("coverage", 1.0),
                },
            )
            return _with_student_contract(query, standard_result)

        emit_trace_event(
            "standard_first_pass_completed",
            {
                "query": query[:220],
                "escalated_to_deep": True,
                "reason": (
                    "standard_pass_failed"
                    if standard_failed
                    else (
                        "forced_deep_after_standard"
                        if standard_satisfies and not _deep_escalate_only_if_unresolved()
                        else "required_fields_missing_after_standard"
                    )
                ),
                "required_fields_missing": _required_field_status_from_result(
                    required_fields,
                    standard_result,
                ).get("missing_ids", []),
                "standard_error": standard_error[:220] if standard_error else "",
            },
        )
        try:
            deep_result = await _run_retrieval_with_context(
                query,
                top_k=top_k,
                mode=normalized_mode,
                required_fields_ctx=required_fields,
                strict_official_ctx=strict_official_sources,
                target_domain_groups_ctx=target_domain_groups,
            )
        except Exception as exc:
            logger.warning(
                "Deep escalation failed after standard pass; returning standard result. %s",
                exc,
            )
            return _with_student_contract(query, standard_result)

        if _result_quality_tuple(
            result=deep_result,
            required_fields=required_fields,
            target_domain_groups=target_domain_groups,
        ) >= _result_quality_tuple(
            result=standard_result,
            required_fields=required_fields,
            target_domain_groups=target_domain_groups,
        ):
            return _with_student_contract(query, deep_result)
        return _with_student_contract(query, standard_result)

    result = await _run_retrieval_with_context(
        query,
        top_k=top_k,
        mode=normalized_mode,
        required_fields_ctx=required_fields,
        strict_official_ctx=strict_official_sources,
        target_domain_groups_ctx=target_domain_groups,
    )
    return _with_student_contract(query, result)


async def _aretrieve_web_chunks_impl(
    query: str,
    *,
    top_k: int = 3,
    search_mode: str = "deep",
) -> dict:
    normalized_mode = _normalized_search_mode(search_mode)
    deep_mode = _is_deep_search_mode(normalized_mode)
    deterministic_applicable = _is_university_program_query(query)
    if _deterministic_controller_enabled() and deterministic_applicable and not deep_mode:
        return await _aretrieve_web_chunks_impl_deterministic(
            query,
            top_k=top_k,
            search_mode=search_mode,
        )
    # Legacy retrieval loop path (kept for backward compatibility / test overrides).
    started_at = time.perf_counter()
    allowed_suffixes = _normalized_allowed_domain_suffixes()
    context_required_ids = _current_required_field_ids()
    if deep_mode:
        required_fields = _required_fields_from_query(query)
    elif context_required_ids:
        required_fields = [
            field
            for field in _required_fields_from_query(query)
            if str(field.get("id", "")).strip() in context_required_ids
        ]
    else:
        required_fields = []
    research_objective_mode = _research_objective_mode_enabled(
        query=query,
        deep_mode=deep_mode,
        required_fields=required_fields,
    )
    research_plan = (
        build_research_plan(
            query,
            max_objectives=max(4, _max_planner_subquestions()),
            max_queries=max(6, _planner_query_limit_for_query(query)),
        )
        if research_objective_mode
        else {"planner": "disabled", "objectives": [], "subquestions": [], "queries": []}
    )
    research_objectives = (
        research_plan.get("objectives", [])
        if isinstance(research_plan.get("objectives", []), list)
        else []
    )
    strict_official_sources = bool(
        deep_mode
        and (
            _is_admissions_high_precision_query(query, required_fields)
            or _is_university_program_query(query)
        )
    )
    target_domain_groups = _target_domain_groups_for_query(query) if strict_official_sources else []
    enforce_target_domain_scope = bool(strict_official_sources and target_domain_groups)
    coverage_target = _required_field_coverage_target(query, required_fields) if deep_mode else 1.0
    if deep_mode:
        query_plan = await _resolve_query_plan(query, allowed_suffixes)
    else:
        query_plan = _build_heuristic_query_plan(query, allowed_suffixes)
        query_plan["planner"] = "fast_heuristic"
        query_plan["llm_used"] = False
        query_plan["subquestions"] = []
    emit_trace_event(
        "query_plan_created",
        {
            "query": query[:220],
            "search_mode": normalized_mode,
            "planner": str(query_plan.get("planner", "heuristic")),
            "llm_used": bool(query_plan.get("llm_used", False)),
            "subquestions": query_plan.get("subquestions", []),
            "required_fields": [str(field.get("id", "")).strip() for field in required_fields],
            "queries": query_plan.get("queries", []),
            "strict_official_sources": strict_official_sources,
            "target_domain_groups": target_domain_groups,
            "target_domain_scope_enforced": enforce_target_domain_scope,
            "required_field_coverage_target": coverage_target,
            "research_objective_mode": research_objective_mode,
            "research_objectives": [str(item.get("id", "")).strip() for item in research_objectives],
        },
    )
    if research_objective_mode:
        emit_trace_event(
            "research_plan_created",
            {
                "query": query[:220],
                "planner": str(research_plan.get("planner", "research_orchestrator")),
                "objective_count": len(research_objectives),
                "objective_ids": [str(item.get("id", "")).strip() for item in research_objectives],
                "queries": research_plan.get("queries", []),
            },
        )
    planned_query_limit = (
        _planner_query_limit_for_query(query) if deep_mode else min(2, _max_planner_queries())
    )
    planned_queries = _normalize_query_list(
        query_plan.get("queries", []),
        limit=planned_query_limit,
    ) or _build_query_variants(query, allowed_suffixes)
    official_route_queries = _build_official_source_route_queries(
        query,
        required_fields,
        max_queries=max(6, planned_query_limit),
    )
    if official_route_queries:
        planned_queries = _normalize_query_list(
            official_route_queries + planned_queries,
            limit=max(planned_query_limit, min(12, planned_query_limit + len(official_route_queries))),
        )
    if deterministic_applicable:
        deterministic_seed_queries = _deterministic_university_route_queries(query, required_fields)
        planned_queries = _normalize_query_list(
            official_route_queries + deterministic_seed_queries + planned_queries,
            limit=max(planned_query_limit, min(10, len(deterministic_seed_queries) + 2)),
        )
    if deep_mode:
        subquestions = _normalize_subquestion_list(
            query_plan.get("subquestions", []),
            limit=_max_planner_subquestions(),
        )
    else:
        subquestions = []
    if research_objective_mode:
        planned_queries = _normalize_query_list(
            planned_queries + list(research_plan.get("queries", [])),
            limit=max(planned_query_limit, min(12, planned_query_limit + 4)),
        )
        subquestions = _normalize_subquestion_list(
            subquestions + list(research_plan.get("subquestions", [])),
            limit=max(_max_planner_subquestions(), _retrieval_min_unique_domains() + 4),
        )

    search_ms_total = 0
    fetch_ms_total = 0
    domain_filter_relaxed = False
    all_candidates: list[dict] = []
    all_facts: list[dict] = []
    gap_iterations: list[dict] = []
    seen_queries: set[str] = set()
    executed_queries: list[str] = []
    loop_llm_used = False
    max_steps = _effective_retrieval_loop_max_steps(query, required_fields, deep_mode=deep_mode)
    if not deep_mode or not _retrieval_loop_enabled():
        max_steps = 1
    stagnant_steps = 0
    stagnation_limit = _retrieval_loop_max_stagnant_steps()
    best_coverage = -1.0
    best_domain_count = 0
    best_missing_total = 10**6

    for step in range(1, max_steps + 1):
        current_domains = _unique_domains_from_candidates(all_candidates)
        heuristic_missing = _identify_missing_subquestions(subquestions, all_facts)
        required_status = _required_field_coverage(required_fields, all_candidates)
        objective_status = research_objective_coverage(research_objectives, all_candidates)
        missing_required_fields = _required_fields_by_ids(
            required_fields,
            required_status.get("missing_ids", []),
        )
        missing_research_objectives = _research_objectives_by_ids(
            research_objectives,
            objective_status.get("missing_ids", []),
        )
        missing_subquestions = _combine_missing_subquestions(
            list(heuristic_missing)
            + list(required_status.get("missing_subquestions", []))
            + list(objective_status.get("missing_subquestions", [])),
            current_domains,
        )
        llm_gap_queries: list[str] = []
        follow_up_queries = _build_follow_up_queries(
            query,
            missing_subquestions=missing_subquestions,
            llm_gap_queries=llm_gap_queries,
            missing_required_fields=missing_required_fields,
            missing_research_objectives=missing_research_objectives,
            allowed_suffixes=allowed_suffixes,
            unique_domains=current_domains,
        )
        if deep_mode and step > 1 and missing_subquestions:
            llm_gap_plan = await _aidentify_gap_plan_with_llm(
                query,
                subquestions=subquestions or missing_subquestions,
                facts=all_facts,
                fallback_missing=missing_subquestions,
            )
            if llm_gap_plan:
                loop_llm_used = True
                missing_subquestions = _normalize_subquestion_list(
                    list(llm_gap_plan.get("missing_subquestions", []))
                    + list(required_status.get("missing_subquestions", [])),
                    limit=max(_max_planner_subquestions(), _retrieval_min_unique_domains() + 2),
                ) or list(missing_subquestions)
                llm_gap_queries = _normalize_query_list(
                    llm_gap_plan.get("queries", []),
                    limit=max(
                        1, int(getattr(settings.web_search, "retrieval_loop_max_gap_queries", 2))
                    ),
                )
                follow_up_queries = _build_follow_up_queries(
                    query,
                    missing_subquestions=missing_subquestions,
                    llm_gap_queries=llm_gap_queries,
                    missing_required_fields=missing_required_fields,
                    missing_research_objectives=missing_research_objectives,
                    allowed_suffixes=allowed_suffixes,
                    unique_domains=current_domains,
                )

        deterministic_first_wave_limit = min(
            10,
            max(
                _max_planner_queries(),
                2
                + len(required_fields)
                + min(2, len(research_objectives))
                + max(1, int(getattr(settings.web_search, "retrieval_loop_max_gap_queries", 2))),
            ),
        )
        route_with_required_fields_first = bool(
            required_fields
            and _is_university_program_query(query)
            and _is_admissions_high_precision_query(query, required_fields)
        )
        multi_query_enabled = bool(getattr(settings.web_search, "multi_query_enabled", False))
        deterministic_first_wave_queries = (
            _normalize_query_list(
                _deterministic_university_route_queries(query, required_fields)
                + _build_required_field_queries(
                    query,
                    missing_required_fields=required_fields,
                    allowed_suffixes=allowed_suffixes,
                    unique_domains=current_domains,
                )
                + _build_research_objective_queries(
                    query,
                    missing_objectives=research_objectives,
                    unique_domains=current_domains,
                )
                + _build_gap_queries(query, subquestions),
                limit=deterministic_first_wave_limit,
            )
            if route_with_required_fields_first or (deep_mode and multi_query_enabled)
            else []
        )
        if step == 1 and route_with_required_fields_first:
            prioritized_first_wave = deterministic_first_wave_queries or planned_queries
            if deep_mode and multi_query_enabled:
                first_wave_max_queries = deterministic_first_wave_limit
            elif _is_admissions_high_precision_query(query, required_fields):
                first_wave_max_queries = min(6, max(4, len(required_fields)))
            else:
                first_wave_max_queries = min(2, max(1, len(required_fields)))
            loop_queries = _next_queries_for_loop(
                prioritized_first_wave,
                seen_queries,
                max_queries=max(1, first_wave_max_queries),
            )
        else:
            loop_queries = _next_loop_queries(
                base_query=query,
                initial_queries=planned_queries,
                first_wave_queries=deterministic_first_wave_queries,
                missing_subquestions=missing_subquestions,
                llm_gap_queries=llm_gap_queries,
                follow_up_queries=follow_up_queries,
                seen_queries=seen_queries,
                loop_step=step,
                deep_mode=deep_mode,
            )
        if not loop_queries:
            break
        executed_queries.extend(loop_queries)
        emit_trace_event(
            "search_started",
            {
                "step": step,
                "queries": loop_queries,
            },
        )

        search_started_at = time.perf_counter()
        payloads = await _asearch_payloads(loop_queries, top_k=top_k)
        search_ms_total += _elapsed_ms(search_started_at)

        rows, relaxed = _collect_search_rows_with_domain_retry(
            payloads,
            loop_queries,
            top_k=top_k,
            allowed_suffixes=allowed_suffixes,
            strict_official=strict_official_sources,
            target_domain_groups=target_domain_groups,
            enforce_target_domain_scope=enforce_target_domain_scope,
        )
        domain_filter_relaxed = domain_filter_relaxed or relaxed
        emit_trace_event(
            "search_results",
            {
                "step": step,
                "result_count": len(rows),
                "urls": [str(row.get("url", "")).strip() for row in rows[:8]],
                "domain_filter_relaxed": relaxed,
            },
        )

        fetch_started_at = time.perf_counter()
        if deep_mode:
            page_data_by_url = await _afetch_organic_pages(rows)
        else:
            page_data_by_url = await _afetch_organic_pages(rows, max_pages_to_fetch=1)
        extracted_page_data: dict[str, dict] = {}
        if _should_run_extract_for_step(
            rows=rows,
            page_data_by_url=page_data_by_url,
            step=step,
            missing_required_fields=missing_required_fields,
            missing_research_objectives=missing_research_objectives,
        ):
            extracted_page_data = await _atry_tavily_extract_rows(
                rows,
                query=query,
                allowed_suffixes=allowed_suffixes,
                strict_official=strict_official_sources,
                target_domain_groups=target_domain_groups,
                enforce_target_domain_scope=enforce_target_domain_scope,
                max_urls=max(3, min(8, _max_pages_to_fetch_for_mode())),
            )
        if extracted_page_data:
            for url, payload in extracted_page_data.items():
                existing = page_data_by_url.get(url)
                existing_content = (
                    " ".join(str((existing or {}).get("content", "")).split())
                    if isinstance(existing, dict)
                    else ""
                )
                new_content = " ".join(str((payload or {}).get("content", "")).split())
                if not existing_content or len(new_content) > len(existing_content):
                    page_data_by_url[url] = payload
        fetch_ms_total += _elapsed_ms(fetch_started_at)
        crawl_summary = {
            "enabled": False,
            "pages_fetched": 0,
            "discovered_urls": 0,
            "depth_reached": 0,
        }
        rows_for_candidates = list(rows)
        if deep_mode and rows:
            crawl_started_at = time.perf_counter()
            crawl_rows, crawl_page_data, crawl_summary = await _acrawl_internal_pages(
                seed_rows=rows,
                seed_page_data_by_url=page_data_by_url,
                required_fields=missing_required_fields or required_fields,
                allowed_suffixes=allowed_suffixes,
                target_domain_groups=target_domain_groups,
                enforce_target_domain_scope=enforce_target_domain_scope,
            )
            fetch_ms_total += _elapsed_ms(crawl_started_at)
            if crawl_page_data:
                page_data_by_url.update(crawl_page_data)
            if crawl_rows:
                rows_for_candidates = _dedupe_rows(
                    rows + crawl_rows,
                    limit=max(
                        _max_context_results_for_mode() * 8,
                        len(rows) + len(crawl_rows),
                    ),
                )
                for crawl_row in crawl_rows[:12]:
                    emit_trace_event(
                        "official_route_followed",
                        {
                            "step": step,
                            "url": str(crawl_row.get("url", "")).strip(),
                            "anchor_text": str(crawl_row.get("title", "")).strip(),
                        },
                    )
            emit_trace_event(
                "internal_crawl_completed",
                {
                    "step": step,
                    "enabled": bool(crawl_summary.get("enabled", False)),
                    "depth_reached": int(crawl_summary.get("depth_reached", 0) or 0),
                    "pages_fetched": int(crawl_summary.get("pages_fetched", 0) or 0),
                    "discovered_urls": int(crawl_summary.get("discovered_urls", 0) or 0),
                    "urls": [str(row.get("url", "")).strip() for row in crawl_rows[:8]],
                },
            )
        emit_trace_event(
            "pages_read",
            {
                "step": step,
                "pages_fetched": len(page_data_by_url),
                "extract_fetched": len(extracted_page_data),
                "urls": list(page_data_by_url.keys())[:8],
            },
        )

        query_tokens = _query_tokens(" ".join(loop_queries))
        candidates = _build_organic_candidates(
            rows=rows_for_candidates,
            page_data_by_url=page_data_by_url,
            query_tokens=query_tokens,
            allowed_suffixes=allowed_suffixes,
            strict_official=strict_official_sources,
            target_domain_groups=target_domain_groups,
            enforce_target_domain_scope=enforce_target_domain_scope,
        )
        ai_candidate = _ai_overview_candidate(payloads, allowed_suffixes)
        if ai_candidate:
            candidates.append(ai_candidate)

        candidates = _apply_trust_scores(candidates, allowed_suffixes)
        all_candidates.extend(candidates)
        all_candidates = _apply_trust_scores(all_candidates, allowed_suffixes)
        all_facts = _extract_facts(
            all_candidates,
            limit=(
                max(2, _max_context_results_for_mode() * 3)
                if deep_mode
                else max(2, _max_context_results_for_mode())
            ),
        )
        emit_trace_event(
            "facts_extracted",
            {
                "step": step,
                "fact_count": len(all_facts),
                "facts": all_facts[:5],
            },
        )

        unique_domains = _unique_domains_from_candidates(all_candidates)
        next_heuristic_missing = _identify_missing_subquestions(subquestions, all_facts)
        next_required_status = _required_field_coverage(required_fields, all_candidates)
        next_objective_status = research_objective_coverage(research_objectives, all_candidates)
        next_missing_required_ids = list(next_required_status.get("missing_ids", []))
        next_missing_objective_ids = list(next_objective_status.get("missing_ids", []))
        next_coverage = float(next_required_status.get("coverage", 1.0) or 0.0)
        target_coverage_count = _target_domain_coverage_count(unique_domains, target_domain_groups)
        next_missing_required_fields = _required_fields_by_ids(required_fields, next_missing_required_ids)
        next_missing_research_objectives = _research_objectives_by_ids(
            research_objectives,
            next_missing_objective_ids,
        )
        next_missing = _combine_missing_subquestions(
            list(next_heuristic_missing)
            + list(next_required_status.get("missing_subquestions", []))
            + list(next_objective_status.get("missing_subquestions", [])),
            unique_domains,
        )
        next_follow_up_queries = _build_follow_up_queries(
            query,
            missing_subquestions=next_missing,
            llm_gap_queries=llm_gap_queries,
            missing_required_fields=next_missing_required_fields,
            missing_research_objectives=next_missing_research_objectives,
            allowed_suffixes=allowed_suffixes,
            unique_domains=unique_domains,
        )
        retrieval_verified = (
            (
                len(unique_domains) >= _retrieval_min_unique_domains()
                and not next_missing
                and not next_missing_required_ids
                and next_coverage >= coverage_target
                and (not enforce_target_domain_scope or target_coverage_count > 0)
            )
            if deep_mode
            else True
        )
        emit_trace_event(
            "retrieval_verification",
            {
                "step": step,
                "verified": retrieval_verified,
                "search_mode": normalized_mode,
                "min_unique_domains": _retrieval_min_unique_domains(),
                "unique_domain_count": len(unique_domains),
                "unique_domains": unique_domains[:8],
                "missing_subquestions": next_missing,
                "required_field_coverage": round(
                    next_coverage,
                    4,
                ),
                "required_field_coverage_target": coverage_target,
                "required_fields_missing": next_missing_required_ids,
                "research_objectives_missing": next_missing_objective_ids,
                "target_domain_coverage_count": target_coverage_count,
            },
        )
        gap_iterations.append(
            {
                "step": step,
                "queries": loop_queries,
                "llm_gap_queries": llm_gap_queries,
                "actions": (
                    ["search_web", "read_pages", "extract_evidence", "verify_coverage"]
                    + (
                        ["crawl_internal_links"]
                        if int(crawl_summary.get("pages_fetched", 0) or 0) > 0
                        else []
                    )
                ),
                "follow_up_queries": next_follow_up_queries,
                "missing_subquestions": next_missing,
                "required_field_coverage": round(
                    next_coverage,
                    4,
                ),
                "required_fields_missing": next_missing_required_ids,
                "research_objectives_missing": next_missing_objective_ids,
                "unique_domains": unique_domains,
                "unique_domain_count": len(unique_domains),
                "target_domain_coverage_count": target_coverage_count,
            }
        )
        emit_trace_event(
            "gaps_identified",
            {
                "step": step,
                "missing_subquestions": next_missing,
                "follow_up_queries": next_follow_up_queries,
            },
        )
        next_missing_total = (
            len(next_missing_required_ids) + len(next_missing_objective_ids) + len(next_missing)
        )
        improved = (
            next_coverage > (best_coverage + 1e-6)
            or len(unique_domains) > best_domain_count
            or next_missing_total < best_missing_total
        )
        if improved:
            best_coverage = max(best_coverage, next_coverage)
            best_domain_count = max(best_domain_count, len(unique_domains))
            best_missing_total = min(best_missing_total, next_missing_total)
            stagnant_steps = 0
        else:
            stagnant_steps += 1

        if retrieval_verified:
            break
        if (
            deep_mode
            and stagnation_limit > 0
            and stagnant_steps >= stagnation_limit
            and step < max_steps
        ):
            emit_trace_event(
                "retrieval_loop_stopped_no_progress",
                {
                    "step": step,
                    "stagnant_steps": stagnant_steps,
                    "stagnation_limit": stagnation_limit,
                    "required_field_coverage": round(next_coverage, 4),
                    "required_fields_missing": next_missing_required_ids,
                    "research_objectives_missing": next_missing_objective_ids,
                    "missing_subquestions": next_missing[:6],
                },
            )
            break

    results = _finalize_candidates(all_candidates)
    extracted_facts = _extract_facts(
        results,
        limit=_max_context_results_for_mode(),
    )
    final_domains = _unique_domains_from_candidates(results)
    final_required_field_status = _required_field_coverage(required_fields, results)
    final_research_objective_status = research_objective_coverage(research_objectives, results)
    final_missing_subquestions = _combine_missing_subquestions(
        list(_identify_missing_subquestions(subquestions, extracted_facts))
        + list(final_required_field_status.get("missing_subquestions", [])),
        final_domains,
    )
    final_missing_subquestions = _combine_missing_subquestions(
        final_missing_subquestions + list(final_research_objective_status.get("missing_subquestions", [])),
        final_domains,
    )
    final_missing_required_ids = list(final_required_field_status.get("missing_ids", []))
    final_missing_research_objective_ids = list(final_research_objective_status.get("missing_ids", []))
    final_coverage = float(final_required_field_status.get("coverage", 1.0) or 0.0)
    final_target_coverage_count = _target_domain_coverage_count(final_domains, target_domain_groups)
    final_field_evidence = _required_field_evidence_table(required_fields, results)
    emit_trace_event(
        "final_slot_coverage",
        {
            "required_field_coverage": round(final_coverage, 4),
            "required_fields_missing": final_missing_required_ids,
            "field_evidence": final_field_evidence[:12],
        },
    )
    final_verified = (
        (
            len(final_domains) >= _retrieval_min_unique_domains()
            and not final_missing_subquestions
            and not final_missing_required_ids
            and final_coverage >= coverage_target
            and (not enforce_target_domain_scope or final_target_coverage_count > 0)
        )
        if deep_mode
        else bool(results)
    )
    if (
        deep_mode
        and _required_field_rescue_enabled()
        and (final_missing_required_ids or final_missing_research_objective_ids)
    ):
        rescue_missing_fields = _required_fields_by_ids(required_fields, final_missing_required_ids)
        rescue_missing_objectives = _research_objectives_by_ids(
            research_objectives,
            final_missing_research_objective_ids,
        )
        rescue_queries = _build_required_field_queries(
            query,
            missing_required_fields=rescue_missing_fields,
            allowed_suffixes=allowed_suffixes,
            unique_domains=final_domains,
        )
        rescue_queries = _normalize_query_list(
            rescue_queries
            + _build_research_objective_queries(
                query,
                missing_objectives=rescue_missing_objectives,
                unique_domains=final_domains,
            ),
            limit=max(_required_field_rescue_max_queries(), _max_planner_queries() + 2),
        )
        rescue_queries = _next_queries_for_loop(
            rescue_queries,
            seen_queries,
            max_queries=_required_field_rescue_max_queries(),
        )
        if rescue_queries:
            emit_trace_event(
                "required_field_rescue_started",
                {
                    "queries": rescue_queries,
                    "missing_required_fields": final_missing_required_ids,
                    "missing_research_objectives": final_missing_research_objective_ids,
                },
            )
            executed_queries.extend(rescue_queries)
            rescue_search_started_at = time.perf_counter()
            try:
                rescue_payloads = await _asearch_payloads(rescue_queries, top_k=top_k)
            except Exception as exc:
                logger.warning("Required-field rescue search failed. %s", exc)
                rescue_payloads = []
            search_ms_total += _elapsed_ms(rescue_search_started_at)

            rescue_rows, rescue_relaxed = _collect_search_rows_with_domain_retry(
                rescue_payloads,
                rescue_queries,
                top_k=top_k,
                allowed_suffixes=allowed_suffixes,
                strict_official=strict_official_sources,
                target_domain_groups=target_domain_groups,
                enforce_target_domain_scope=enforce_target_domain_scope,
            )
            domain_filter_relaxed = domain_filter_relaxed or rescue_relaxed
            rescue_fetch_started_at = time.perf_counter()
            rescue_pages = await _afetch_organic_pages(rescue_rows) if rescue_rows else {}
            rescue_extracted_pages: dict[str, dict] = {}
            if _should_run_extract_for_step(
                rows=rescue_rows,
                page_data_by_url=rescue_pages,
                step=max_steps + 1,
                missing_required_fields=rescue_missing_fields,
                missing_research_objectives=rescue_missing_objectives,
            ):
                rescue_extracted_pages = await _atry_tavily_extract_rows(
                    rescue_rows,
                    query=query,
                    allowed_suffixes=allowed_suffixes,
                    strict_official=strict_official_sources,
                    target_domain_groups=target_domain_groups,
                    enforce_target_domain_scope=enforce_target_domain_scope,
                    max_urls=max(3, min(8, _max_pages_to_fetch_for_mode())),
                )
            if rescue_extracted_pages:
                for url, payload in rescue_extracted_pages.items():
                    existing = rescue_pages.get(url)
                    existing_content = (
                        " ".join(str((existing or {}).get("content", "")).split())
                        if isinstance(existing, dict)
                        else ""
                    )
                    new_content = " ".join(str((payload or {}).get("content", "")).split())
                    if not existing_content or len(new_content) > len(existing_content):
                        rescue_pages[url] = payload
            fetch_ms_total += _elapsed_ms(rescue_fetch_started_at)
            rescue_crawl_summary = {
                "enabled": False,
                "pages_fetched": 0,
                "discovered_urls": 0,
                "depth_reached": 0,
            }
            rescue_rows_for_candidates = list(rescue_rows)
            if deep_mode and rescue_rows:
                rescue_crawl_started_at = time.perf_counter()
                rescue_crawl_rows, rescue_crawl_pages, rescue_crawl_summary = await _acrawl_internal_pages(
                    seed_rows=rescue_rows,
                    seed_page_data_by_url=rescue_pages,
                    required_fields=rescue_missing_fields or required_fields,
                    allowed_suffixes=allowed_suffixes,
                    target_domain_groups=target_domain_groups,
                    enforce_target_domain_scope=enforce_target_domain_scope,
                )
                fetch_ms_total += _elapsed_ms(rescue_crawl_started_at)
                if rescue_crawl_pages:
                    rescue_pages.update(rescue_crawl_pages)
                if rescue_crawl_rows:
                    rescue_rows_for_candidates = _dedupe_rows(
                        rescue_rows + rescue_crawl_rows,
                        limit=max(
                            _max_context_results_for_mode() * 8,
                            len(rescue_rows) + len(rescue_crawl_rows),
                        ),
                    )
                emit_trace_event(
                    "required_field_rescue_internal_crawl_completed",
                    {
                        "enabled": bool(rescue_crawl_summary.get("enabled", False)),
                        "depth_reached": int(rescue_crawl_summary.get("depth_reached", 0) or 0),
                        "pages_fetched": int(rescue_crawl_summary.get("pages_fetched", 0) or 0),
                        "discovered_urls": int(rescue_crawl_summary.get("discovered_urls", 0) or 0),
                        "urls": [str(row.get("url", "")).strip() for row in rescue_crawl_rows[:8]],
                    },
                )
            emit_trace_event(
                "required_field_rescue_pages_read",
                {
                    "pages_fetched": len(rescue_pages),
                    "extract_fetched": len(rescue_extracted_pages),
                    "urls": [str(row.get("url", "")).strip() for row in rescue_rows[:8]],
                },
            )
            if rescue_rows:
                rescue_query_tokens = _query_tokens(" ".join(rescue_queries))
                rescue_candidates = _build_organic_candidates(
                    rows=rescue_rows_for_candidates,
                    page_data_by_url=rescue_pages,
                    query_tokens=rescue_query_tokens,
                    allowed_suffixes=allowed_suffixes,
                    strict_official=strict_official_sources,
                    target_domain_groups=target_domain_groups,
                    enforce_target_domain_scope=enforce_target_domain_scope,
                )
                rescue_ai_candidate = _ai_overview_candidate(rescue_payloads, allowed_suffixes)
                if rescue_ai_candidate:
                    rescue_candidates.append(rescue_ai_candidate)
                rescue_candidates = _apply_trust_scores(rescue_candidates, allowed_suffixes)
                all_candidates.extend(rescue_candidates)
                all_candidates = _apply_trust_scores(all_candidates, allowed_suffixes)
                all_facts = _extract_facts(
                    all_candidates,
                    limit=max(2, _max_context_results_for_mode() * 3),
                )
            results = _finalize_candidates(all_candidates)
            extracted_facts = _extract_facts(
                results,
                limit=_max_context_results_for_mode(),
            )
            final_domains = _unique_domains_from_candidates(results)
            final_required_field_status = _required_field_coverage(required_fields, results)
            final_research_objective_status = research_objective_coverage(research_objectives, results)
            final_missing_subquestions = _combine_missing_subquestions(
                list(_identify_missing_subquestions(subquestions, extracted_facts))
                + list(final_required_field_status.get("missing_subquestions", [])),
                final_domains,
            )
            final_missing_subquestions = _combine_missing_subquestions(
                final_missing_subquestions
                + list(final_research_objective_status.get("missing_subquestions", [])),
                final_domains,
            )
            final_missing_required_ids = list(final_required_field_status.get("missing_ids", []))
            final_missing_research_objective_ids = list(final_research_objective_status.get("missing_ids", []))
            final_coverage = float(final_required_field_status.get("coverage", 1.0) or 0.0)
            final_target_coverage_count = _target_domain_coverage_count(
                final_domains, target_domain_groups
            )
            final_field_evidence = _required_field_evidence_table(required_fields, results)
            final_verified = (
                (
                    len(final_domains) >= _retrieval_min_unique_domains()
                    and not final_missing_subquestions
                    and not final_missing_required_ids
                    and final_coverage >= coverage_target
                    and (not enforce_target_domain_scope or final_target_coverage_count > 0)
                )
                if deep_mode
                else bool(results)
            )
            gap_iterations.append(
                {
                    "step": "required_field_rescue",
                    "queries": rescue_queries,
                    "actions": (
                        ["search_web", "read_pages", "extract_evidence", "verify_coverage"]
                        + (
                            ["crawl_internal_links"]
                            if int(rescue_crawl_summary.get("pages_fetched", 0) or 0) > 0
                            else []
                        )
                    ),
                    "missing_subquestions": final_missing_subquestions,
                    "required_field_coverage": round(final_coverage, 4),
                    "required_fields_missing": final_missing_required_ids,
                    "research_objectives_missing": final_missing_research_objective_ids,
                    "unique_domains": final_domains,
                    "unique_domain_count": len(final_domains),
                    "target_domain_coverage_count": final_target_coverage_count,
                }
            )
            emit_trace_event(
                "required_field_rescue_completed",
                {
                    "result_count": len(results),
                    "required_field_coverage": round(final_coverage, 4),
                    "required_field_coverage_target": coverage_target,
                    "required_fields_missing": final_missing_required_ids,
                    "research_objectives_missing": final_missing_research_objective_ids,
                    "target_domain_coverage_count": final_target_coverage_count,
                },
            )
    emit_trace_event(
        "source_ranking_completed",
        {
            "result_count": len(results),
            "facts": extracted_facts,
            "unique_domain_count": len(final_domains),
            "unique_domains": final_domains[:8],
            "required_field_coverage": round(final_coverage, 4),
            "required_field_coverage_target": coverage_target,
            "required_fields_missing": final_missing_required_ids,
            "research_objectives_missing": final_missing_research_objective_ids,
            "target_domain_coverage_count": final_target_coverage_count,
            "field_evidence": final_field_evidence[:10],
            "urls": [
                str((item.get("metadata") or {}).get("url", "")).strip()
                for item in results[:8]
                if isinstance(item, dict)
            ],
        },
    )

    return {
        "query": query,
        "query_variants": executed_queries,
        "search_mode": normalized_mode,
        "query_plan": {
            "planner": str(query_plan.get("planner", "heuristic")),
            "llm_used": bool(query_plan.get("llm_used", False)),
            "subquestions": subquestions,
            "required_fields": [str(field.get("id", "")).strip() for field in required_fields],
            "research_objectives": [str(item.get("id", "")).strip() for item in research_objectives],
            "research_objective_mode": research_objective_mode,
        },
        "retrieval_loop": {
            "enabled": bool(deep_mode and _retrieval_loop_enabled()),
            "llm_used": loop_llm_used,
            "iterations": len(gap_iterations),
            "steps": gap_iterations,
        },
        "verification": {
            "min_unique_domains": _retrieval_min_unique_domains(),
            "unique_domains": final_domains,
            "unique_domain_count": len(final_domains),
            "missing_subquestions": final_missing_subquestions,
            "required_field_coverage": round(final_coverage, 4),
            "required_field_coverage_target": coverage_target,
            "required_fields": final_required_field_status.get("fields", []),
            "required_fields_missing": final_missing_required_ids,
            "required_field_labels_missing": final_required_field_status.get("missing_labels", []),
            "research_objective_coverage": round(
                float(final_research_objective_status.get("coverage", 1.0) or 0.0),
                4,
            ),
            "research_objectives": final_research_objective_status.get("fields", []),
            "research_objectives_missing": final_missing_research_objective_ids,
            "research_objective_labels_missing": final_research_objective_status.get(
                "missing_labels", []
            ),
            "verified": final_verified,
            "strict_official_sources": strict_official_sources,
            "target_domain_groups": target_domain_groups,
            "target_domain_scope_enforced": enforce_target_domain_scope,
            "target_domain_coverage_count": final_target_coverage_count,
            "field_evidence": final_field_evidence,
        },
        "facts": extracted_facts,
        "field_evidence": final_field_evidence,
        "retrieval_strategy": (
            "web_search_domain_relaxed" if domain_filter_relaxed else "web_search"
        ),
        "domain_filter_relaxed": domain_filter_relaxed,
        "timings_ms": {
            "search": search_ms_total,
            "page_fetch": fetch_ms_total,
            "total": _elapsed_ms(started_at),
        },
        "results": results,
    }


async def _aretrieve_web_chunks_impl_deterministic(
    query: str,
    *,
    top_k: int = 3,
    search_mode: str = "deep",
) -> dict:
    started_at = time.perf_counter()
    allowed_suffixes = _normalized_allowed_domain_suffixes()
    normalized_mode = _normalized_search_mode(search_mode)
    deep_mode = _is_deep_search_mode(normalized_mode)
    context_required_ids = _current_required_field_ids()
    if deep_mode:
        required_fields = _required_fields_from_query(query)
    elif context_required_ids:
        required_fields = _required_fields_from_query(query)
        required_fields = [
            field
            for field in required_fields
            if str(field.get("id", "")).strip() in context_required_ids
        ]
    else:
        required_fields = []
    strict_official_sources = bool(
        deep_mode
        and (
            _is_admissions_high_precision_query(query, required_fields)
            or _is_university_program_query(query)
        )
    )
    target_domain_groups = _target_domain_groups_for_query(query) if strict_official_sources else []
    enforce_target_domain_scope = bool(strict_official_sources and target_domain_groups)
    coverage_target = _required_field_coverage_target(query, required_fields) if deep_mode else 1.0

    research_objective_mode = _research_objective_mode_enabled(
        query=query,
        deep_mode=deep_mode,
        required_fields=required_fields,
    )
    research_plan = (
        build_research_plan(
            query,
            max_objectives=max(4, _max_planner_subquestions()),
            max_queries=max(6, _planner_query_limit_for_query(query)),
        )
        if research_objective_mode
        else {"planner": "disabled", "objectives": [], "subquestions": [], "queries": []}
    )
    research_objectives = (
        research_plan.get("objectives", [])
        if isinstance(research_plan.get("objectives", []), list)
        else []
    )

    if deep_mode:
        deep_min_queries = 3
        if _is_university_program_query(query):
            deep_min_queries = max(deep_min_queries, min(6, len(required_fields)))
        max_queries = max(_deep_search_max_queries(), deep_min_queries)
    else:
        max_queries = _standard_search_max_queries()
    base_queries = _build_query_variants(query, allowed_suffixes)
    deterministic_route_queries = (
        _deterministic_university_route_queries(query, required_fields)
        if _is_university_program_query(query)
        else []
    )
    routed_required_queries = _build_required_field_queries(
        query,
        missing_required_fields=required_fields,
        allowed_suffixes=allowed_suffixes,
        unique_domains=[],
    )
    research_queries = (
        _build_research_objective_queries(
            query,
            missing_objectives=research_objectives,
            unique_domains=[],
        )
        if research_objective_mode
        else []
    )
    query_variants = _normalize_query_list(
        deterministic_route_queries + routed_required_queries + base_queries + research_queries,
        limit=max_queries,
    )
    query_variants = _normalize_query_list(query_variants, limit=max_queries)

    search_ms_total = 0
    fetch_ms_total = 0
    extract_url_count = 0
    basic_calls = 0
    advanced_calls = 0
    fields_filled_by_round: list[int] = []
    coverage_deltas: list[float] = []
    stop_reason = "budget_cap"

    if not query_variants:
        results: list[dict] = []
        field_evidence = _required_field_evidence_table(required_fields, results)
        return {
            "query": query,
            "query_variants": [],
            "search_mode": normalized_mode,
            "query_plan": {
                "planner": "deterministic",
                "llm_used": False,
                "subquestions": _required_field_subquestions(required_fields),
                "required_fields": [str(field.get("id", "")).strip() for field in required_fields],
                "research_objectives": [str(item.get("id", "")).strip() for item in research_objectives],
                "research_objective_mode": research_objective_mode,
            },
            "retrieval_loop": {"enabled": False, "llm_used": False, "iterations": 0, "steps": []},
            "verification": {
                "min_unique_domains": _retrieval_min_unique_domains(),
                "unique_domains": [],
                "unique_domain_count": 0,
                "missing_subquestions": _required_field_subquestions(required_fields),
                "required_field_coverage": 0.0 if required_fields else 1.0,
                "required_field_coverage_target": coverage_target,
                "required_fields": [],
                "required_fields_missing": [str(field.get("id", "")).strip() for field in required_fields],
                "required_field_labels_missing": [str(field.get("label", "")).strip() for field in required_fields],
                "research_objective_coverage": 0.0 if research_objectives else 1.0,
                "research_objectives": [],
                "research_objectives_missing": [str(item.get("id", "")).strip() for item in research_objectives],
                "research_objective_labels_missing": [str(item.get("label", "")).strip() for item in research_objectives],
                "verified": False,
                "strict_official_sources": strict_official_sources,
                "target_domain_groups": target_domain_groups,
                "target_domain_scope_enforced": enforce_target_domain_scope,
                "target_domain_coverage_count": 0,
                "field_evidence": field_evidence,
                "source_policy": (
                    "official_first_with_discovery_fallback"
                    if strict_official_sources
                    else "mixed_trusted"
                ),
                "unresolved_fields": [str(field.get("id", "")).strip() for field in required_fields],
            },
            "facts": [],
            "field_evidence": field_evidence,
            "evidence_ledger": field_evidence,
            "coverage_summary": {
                "required_fields": [str(field.get("id", "")).strip() for field in required_fields],
                "unresolved_fields": [str(field.get("id", "")).strip() for field in required_fields],
                "required_field_coverage": 0.0 if required_fields else 1.0,
                "target_coverage": coverage_target,
                "source_policy": (
                    "official_first_with_discovery_fallback"
                    if strict_official_sources
                    else "mixed_trusted"
                ),
            },
            "retrieval_strategy": "web_search",
            "domain_filter_relaxed": False,
            "timings_ms": {"search": 0, "page_fetch": 0, "total": _elapsed_ms(started_at)},
            "metrics": {
                "tavily_calls_total": 0,
                "tavily_calls_basic": 0,
                "tavily_calls_advanced": 0,
                "extract_url_count": 0,
                "fields_filled_by_round": [],
                "coverage_deltas": [],
                "stop_reason": "budget_cap",
            },
            "results": results,
        }

    emit_trace_event(
        "query_plan_created",
        {
            "query": query[:220],
            "search_mode": normalized_mode,
            "planner": "deterministic",
            "llm_used": False,
            "queries": query_variants,
            "required_fields": [str(field.get("id", "")).strip() for field in required_fields],
            "research_objective_mode": research_objective_mode,
        },
    )
    emit_trace_event("search_started", {"step": 1, "queries": query_variants})

    search_started_at = time.perf_counter()
    payloads = await _asearch_payloads(query_variants, top_k=top_k)
    search_ms_total += _elapsed_ms(search_started_at)
    if _search_depth_for_mode() == "advanced":
        advanced_calls += len(query_variants)
    else:
        basic_calls += len(query_variants)

    rows, domain_filter_relaxed = _collect_search_rows_with_domain_retry(
        payloads,
        query_variants,
        top_k=top_k,
        allowed_suffixes=allowed_suffixes,
        strict_official=strict_official_sources,
        target_domain_groups=target_domain_groups,
        enforce_target_domain_scope=enforce_target_domain_scope,
    )

    fetch_started_at = time.perf_counter()
    try:
        page_data_by_url = await _afetch_organic_pages(
            rows,
            max_pages_to_fetch=_max_pages_to_fetch_for_mode(),
        )
    except TypeError as exc:
        if "max_pages_to_fetch" not in str(exc):
            raise
        page_data_by_url = await _afetch_organic_pages(rows)
    extracted_page_data: dict[str, dict] = {}
    if deep_mode and _deep_extract_max_urls() > 0 and rows:
        extracted_page_data = await _atry_tavily_extract_rows(
            rows,
            query=query,
            allowed_suffixes=allowed_suffixes,
            strict_official=strict_official_sources,
            target_domain_groups=target_domain_groups,
            enforce_target_domain_scope=enforce_target_domain_scope,
            max_urls=min(_deep_extract_max_urls(), max(1, _max_pages_to_fetch_for_mode())),
        )
        extract_url_count += len(extracted_page_data)
    if extracted_page_data:
        for url, payload in extracted_page_data.items():
            existing = page_data_by_url.get(url)
            existing_content = (
                " ".join(str((existing or {}).get("content", "")).split())
                if isinstance(existing, dict)
                else ""
            )
            new_content = " ".join(str((payload or {}).get("content", "")).split())
            if not existing_content or len(new_content) > len(existing_content):
                page_data_by_url[url] = payload
    fetch_ms_total += _elapsed_ms(fetch_started_at)

    query_tokens = _query_tokens(" ".join(query_variants))
    candidates = _build_organic_candidates(
        rows=rows,
        page_data_by_url=page_data_by_url,
        query_tokens=query_tokens,
        allowed_suffixes=allowed_suffixes,
        strict_official=strict_official_sources,
        target_domain_groups=target_domain_groups,
        enforce_target_domain_scope=enforce_target_domain_scope,
    )
    ai_candidate = _ai_overview_candidate(payloads, allowed_suffixes)
    if ai_candidate:
        candidates.append(ai_candidate)
    candidates = _apply_trust_scores(candidates, allowed_suffixes)
    candidates = _boost_pdf_scores(candidates)  # Boost PDFs after trust scoring
    results = _finalize_candidates(candidates)
    facts = _extract_facts(results, limit=_max_context_results_for_mode())

    required_status = _required_field_coverage(required_fields, results)
    objective_status = research_objective_coverage(research_objectives, results)
    final_coverage = float(required_status.get("coverage", 1.0) or 0.0)
    missing_required_ids = list(required_status.get("missing_ids", []))
    missing_research_objective_ids = list(objective_status.get("missing_ids", []))
    final_domains = _unique_domains_from_candidates(results)
    final_target_domain_coverage_count = _target_domain_coverage_count(
        final_domains,
        target_domain_groups,
    )
    field_evidence = _required_field_evidence_table(required_fields, results)
    fields_filled = sum(
        1 for row in field_evidence if str(row.get("status", "")).strip().lower() == "found"
    )
    fields_filled_by_round.append(fields_filled)
    coverage_deltas.append(round(final_coverage, 4))

    if (
        (not missing_required_ids)
        and final_coverage >= coverage_target
        and (
            not enforce_target_domain_scope
            or final_target_domain_coverage_count > 0
        )
    ):
        stop_reason = "coverage_reached"
    elif not results:
        stop_reason = "no_progress"
    elif _retrieval_no_progress_cutoff() <= 0:
        stop_reason = "budget_cap"
    else:
        stop_reason = "no_progress"

    emit_trace_event(
        "coverage_delta",
        {
            "step": 1,
            "required_field_coverage": round(final_coverage, 4),
            "delta": round(final_coverage, 4),
            "required_fields_missing": missing_required_ids,
        },
    )
    emit_trace_event(
        "retrieval_loop_stopped",
        {
            "stop_reason": stop_reason,
            "required_field_coverage": round(final_coverage, 4),
            "required_fields_missing": missing_required_ids,
            "search_calls": len(query_variants),
            "extract_url_count": extract_url_count,
        },
    )

    verified = (
        bool(results)
        and (not missing_required_ids)
        and final_coverage >= coverage_target
        and (
            not enforce_target_domain_scope
            or final_target_domain_coverage_count > 0
        )
    )
    source_policy = (
        "official_first_with_discovery_fallback" if strict_official_sources else "mixed_trusted"
    )
    unresolved_fields = [
        str(row.get("id", "")).strip()
        for row in field_evidence
        if str(row.get("status", "")).strip().lower() != "found"
    ]
    emit_trace_event(
        "final_slot_coverage",
        {
            "required_field_coverage": round(final_coverage, 4),
            "required_fields_missing": missing_required_ids,
            "field_evidence": field_evidence[:12],
        },
    )
    return {
        "query": query,
        "query_variants": query_variants,
        "search_mode": normalized_mode,
        "query_plan": {
            "planner": "deterministic",
            "llm_used": False,
            "subquestions": _required_field_subquestions(required_fields),
            "required_fields": [str(field.get("id", "")).strip() for field in required_fields],
            "research_objectives": [str(item.get("id", "")).strip() for item in research_objectives],
            "research_objective_mode": research_objective_mode,
        },
        "retrieval_loop": {
            "enabled": False,
            "llm_used": False,
            "iterations": 1,
            "steps": [
                {
                    "step": 1,
                    "queries": query_variants,
                    "actions": ["search_web", "read_pages", "extract_evidence", "verify_coverage"],
                    "required_field_coverage": round(final_coverage, 4),
                    "required_fields_missing": missing_required_ids,
                    "research_objectives_missing": missing_research_objective_ids,
                    "unique_domains": final_domains,
                    "unique_domain_count": len(final_domains),
                    "target_domain_coverage_count": final_target_domain_coverage_count,
                }
            ],
        },
        "verification": {
            "min_unique_domains": _retrieval_min_unique_domains(),
            "unique_domains": final_domains,
            "unique_domain_count": len(final_domains),
            "missing_subquestions": _combine_missing_subquestions(
                list(required_status.get("missing_subquestions", []))
                + list(objective_status.get("missing_subquestions", [])),
                final_domains,
            ),
            "required_field_coverage": round(final_coverage, 4),
            "required_field_coverage_target": coverage_target,
            "required_fields": required_status.get("fields", []),
            "required_fields_missing": missing_required_ids,
            "required_field_labels_missing": required_status.get("missing_labels", []),
            "research_objective_coverage": round(
                float(objective_status.get("coverage", 1.0) or 0.0),
                4,
            ),
            "research_objectives": objective_status.get("fields", []),
            "research_objectives_missing": missing_research_objective_ids,
            "research_objective_labels_missing": objective_status.get("missing_labels", []),
            "verified": verified,
            "strict_official_sources": strict_official_sources,
            "target_domain_groups": target_domain_groups,
            "target_domain_scope_enforced": enforce_target_domain_scope,
            "target_domain_coverage_count": final_target_domain_coverage_count,
            "field_evidence": field_evidence,
            "source_policy": source_policy,
            "unresolved_fields": unresolved_fields,
        },
        "facts": facts,
        "field_evidence": field_evidence,
        "evidence_ledger": field_evidence,
        "coverage_summary": {
            "required_fields": [str(field.get("id", "")).strip() for field in required_fields],
            "unresolved_fields": unresolved_fields,
            "required_field_coverage": round(final_coverage, 4),
            "target_coverage": coverage_target,
            "source_policy": source_policy,
        },
        "retrieval_strategy": "web_search",
        "domain_filter_relaxed": domain_filter_relaxed,
        "timings_ms": {
            "search": search_ms_total,
            "page_fetch": fetch_ms_total,
            "total": _elapsed_ms(started_at),
        },
        "metrics": {
            "tavily_calls_total": basic_calls + advanced_calls,
            "tavily_calls_basic": basic_calls,
            "tavily_calls_advanced": advanced_calls,
            "extract_url_count": extract_url_count,
            "fields_filled_by_round": fields_filled_by_round,
            "coverage_deltas": coverage_deltas,
            "stop_reason": stop_reason,
        },
        "results": results,
    }
