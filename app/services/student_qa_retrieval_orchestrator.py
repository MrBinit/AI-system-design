from app.services.canonical_knowledge_service import fetch_canonical_slot_facts
from app.services.coverage_ledger_service import (
    build_coverage_ledger,
    coverage_score_from_ledger,
    unresolved_slots_from_ledger,
)
from app.services.source_policy_engine import build_source_policy_decisions
from app.services.student_entity_resolver import resolve_student_entities
from app.services.student_qa_schema_registry import resolve_question_schema


def _normalize_field_evidence_rows(result: dict) -> list[dict]:
    rows = result.get("evidence_ledger", result.get("field_evidence", []))
    rows = rows if isinstance(rows, list) else []
    normalized: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized.append(dict(row))
    if normalized:
        return normalized
    verification = result.get("verification")
    verification = verification if isinstance(verification, dict) else {}
    rows = verification.get("field_evidence", [])
    rows = rows if isinstance(rows, list) else []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _retrieval_budget_usage(result: dict) -> dict:
    query_variants = result.get("query_variants", [])
    query_variants = query_variants if isinstance(query_variants, list) else []
    timings = result.get("timings_ms", {})
    timings = timings if isinstance(timings, dict) else {}
    metrics = result.get("metrics", {})
    metrics = metrics if isinstance(metrics, dict) else {}
    retrieval_loop = result.get("retrieval_loop", {})
    retrieval_loop = retrieval_loop if isinstance(retrieval_loop, dict) else {}
    verification = result.get("verification", {})
    verification = verification if isinstance(verification, dict) else {}
    return {
        "query_count": len(query_variants),
        "pages_fetched": int(timings.get("page_fetch", 0) or 0),
        "search_ms": int(timings.get("search", 0) or 0),
        "total_ms": int(timings.get("total", 0) or 0),
        "extract_url_count": int(metrics.get("extract_url_count", 0) or 0),
        "planner_calls": 1 if result.get("query_plan") else 0,
        "loop_iterations": int(retrieval_loop.get("iterations", 0) or 0),
        "unique_domain_count": int(verification.get("unique_domain_count", 0) or 0),
    }


def augment_retrieval_result_with_student_contract(query: str, result: dict) -> dict:
    if not isinstance(result, dict):
        return {}
    schema = resolve_question_schema(query)
    required_slots = schema.get("required_slots", [])
    entity_context = resolve_student_entities(query)
    canonical_facts = fetch_canonical_slot_facts(
        query=query,
        required_slots=required_slots,
        entity_context=entity_context,
    )
    field_evidence = _normalize_field_evidence_rows(result)
    candidates = result.get("results", [])
    candidates = candidates if isinstance(candidates, list) else []
    coverage_ledger = build_coverage_ledger(
        required_slots=required_slots,
        field_evidence=field_evidence,
        canonical_facts=canonical_facts,
    )
    unresolved_slots = unresolved_slots_from_ledger(coverage_ledger)
    source_policy_decisions = build_source_policy_decisions(
        required_slots=required_slots,
        candidates=candidates,
        canonical_facts=canonical_facts,
    )
    budget = _retrieval_budget_usage(result)
    merged = dict(result)
    merged["question_schema_id"] = str(schema.get("schema_id", "student_general"))
    merged["required_slots"] = [dict(slot) for slot in required_slots if isinstance(slot, dict)]
    merged["coverage_ledger"] = coverage_ledger
    merged["unresolved_slots"] = unresolved_slots
    merged["source_policy_decisions"] = source_policy_decisions
    merged["retrieval_budget_usage"] = budget
    merged["canonical_slot_facts"] = canonical_facts
    merged["entity_resolution"] = entity_context
    # Keep backward-compatible aliases.
    merged["evidence_ledger"] = coverage_ledger
    merged["field_evidence"] = coverage_ledger

    verification = merged.get("verification")
    verification = verification if isinstance(verification, dict) else {}
    verification = dict(verification)
    verification["field_evidence"] = coverage_ledger
    verification["required_field_coverage"] = coverage_score_from_ledger(coverage_ledger)
    verification["required_fields_missing"] = unresolved_slots
    verification["unresolved_slots"] = unresolved_slots
    merged["verification"] = verification

    coverage_summary = merged.get("coverage_summary")
    coverage_summary = coverage_summary if isinstance(coverage_summary, dict) else {}
    coverage_summary = dict(coverage_summary)
    coverage_summary["required_field_coverage"] = coverage_score_from_ledger(coverage_ledger)
    coverage_summary["unresolved_fields"] = unresolved_slots
    merged["coverage_summary"] = coverage_summary
    return merged
