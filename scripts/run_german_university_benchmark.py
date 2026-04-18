#!/usr/bin/env python3
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_QUESTIONS = [
    (
        "Compare TUM vs LMU for English-taught data science master's programs, "
        "including admission requirements and application deadlines."
    ),
    # (
    #     "What is the application deadline and required documents for TUM "
    #     "M.Sc. Data Engineering and Analytics for Winter Semester?"
    # ),
    # (
    #     "For RWTH Aachen M.Sc. Computer Science, what are tuition fees, "
    #     "semester contribution, and language requirements for international students?"
    # ),
    # (
    #     "List English-taught AI or Data-related master's programs at "
    #     "University of Hamburg and their key eligibility criteria."
    # ),
]

NO_RELEVANT_INFORMATION = "Sorry, no relevant information is found."
LOW_SIMILARITY_THRESHOLD = 0.45
MIN_SOURCE_DOMAIN_COUNT = 2


def _parse_iso(ts: str) -> float | None:
    if not ts:
        return None
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def _last_payload(events: list[dict], event_type: str) -> dict:
    for event in reversed(events):
        if str(event.get("type", "")).strip().lower() == event_type:
            payload = event.get("payload")
            return payload if isinstance(payload, dict) else {}
    return {}


def _all_payloads(events: list[dict], event_type: str) -> list[dict]:
    payloads: list[dict] = []
    for event in events:
        if str(event.get("type", "")).strip().lower() == event_type:
            payload = event.get("payload")
            if isinstance(payload, dict):
                payloads.append(payload)
    return payloads


def _extract_urls(text: str) -> list[str]:
    return sorted(set(re.findall(r"https?://[^\s)\]>'\"]+", text or "")))


def _host(url: str) -> str:
    try:
        hostname = (urlparse(url).hostname or "").lower()
        return hostname[4:] if hostname.startswith("www.") else hostname
    except Exception:
        return ""


def _avg(values: list[object]) -> float | None:
    nums = [float(value) for value in values if isinstance(value, (int, float))]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 4)


def _to_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return float(text)
        except Exception:
            return default
    return default


def _to_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(float(text))
        except Exception:
            return default
    return default


def _is_abstain_answer(answer: str) -> bool:
    normalized = " ".join(str(answer or "").split()).strip().lower()
    if not normalized:
        return False
    target = NO_RELEVANT_INFORMATION.lower()
    return normalized == target or normalized.startswith(target)


def _has_trace_event(events: list[dict], event_type: str) -> bool:
    target = event_type.strip().lower()
    for event in events:
        if str(event.get("type", "")).strip().lower() == target:
            return True
    return False


def _derive_abstain_reason(
    *,
    answer: str,
    finalized: dict,
    verification: dict,
    vector: dict,
    web: dict,
    domains: list[str],
    traces: list[dict],
) -> str:
    if not _is_abstain_answer(answer):
        return ""
    explicit = " ".join(str(finalized.get("abstain_reason", "")).split()).strip().lower()
    if explicit in {"no_web", "low_similarity", "insufficient_domains", "verifier_blocked"}:
        return explicit

    issues = verification.get("issues") if isinstance(verification.get("issues"), list) else []
    for issue in issues:
        lowered = str(issue).strip().lower()
        if lowered.startswith("verifier:") or lowered == "coverage_below_threshold":
            return "verifier_blocked"

    web_result_count = _to_int(web.get("result_count"), default=0)
    if _has_trace_event(traces, "web_fallback_started") and web_result_count <= 0:
        return "no_web"

    similarity = _to_float(vector.get("top_similarity"), default=-1.0)
    if similarity >= 0.0 and similarity < LOW_SIMILARITY_THRESHOLD:
        return "low_similarity"

    if len(domains) < MIN_SOURCE_DOMAIN_COUNT:
        return "insufficient_domains"

    return "verifier_blocked"


def _run_one(
    *,
    api_url: str,
    token: str,
    user_id: str,
    session_id: str,
    mode: str,
    question: str,
    idx: int,
) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "user_id": user_id,
        "session_id": session_id,
        "prompt": question,
        "mode": mode,
    }

    started_at = time.time()
    statuses: list[str] = []
    traces: list[dict] = []
    answer = ""
    error = ""
    job_id = ""

    with requests.post(
        api_url, json=payload, headers=headers, stream=True, timeout=(20, 180)
    ) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if not data:
                continue
            try:
                event = json.loads(data)
            except Exception:
                continue

            event_type = str(event.get("type", "")).strip().lower()
            if event_type == "queued":
                job_id = str(event.get("job_id", "")).strip()
            elif event_type == "status":
                statuses.append(str(event.get("status", "")).strip())
            elif event_type == "trace":
                trace = event.get("event")
                if isinstance(trace, dict):
                    traces.append(trace)
            elif event_type == "chunk":
                answer = str(event.get("text", ""))
            elif event_type == "error":
                error = str(event.get("detail", "")).strip()
            elif event_type == "done":
                break

    ended_at = time.time()

    finalized = _last_payload(traces, "answer_finalized")
    verification_payloads = _all_payloads(traces, "answer_verification_completed")
    verification = verification_payloads[-1] if verification_payloads else {}
    vector = _last_payload(traces, "retrieval_vector_completed")
    web = _last_payload(traces, "web_fallback_completed")
    selective = _last_payload(traces, "retrieval_selective_filter")
    trust = _last_payload(traces, "evidence_trust_scored")
    grounding = _last_payload(traces, "claim_grounding_evaluated")

    urls = finalized.get("source_urls") if isinstance(finalized.get("source_urls"), list) else []
    if not urls:
        urls = _extract_urls(answer)
    urls = [str(url).strip() for url in urls if str(url).strip()]
    domains = sorted(set(host for host in (_host(url) for url in urls) if host))

    ts_values = [_parse_iso(str(event.get("timestamp", ""))) for event in traces]
    ts_values = [value for value in ts_values if isinstance(value, (int, float))]
    trace_duration = (max(ts_values) - min(ts_values)) if len(ts_values) >= 2 else None

    issues = verification.get("issues") if isinstance(verification.get("issues"), list) else []
    abstained = _is_abstain_answer(answer)
    abstain_reason = _derive_abstain_reason(
        answer=answer,
        finalized=finalized,
        verification=verification,
        vector=vector,
        web=web,
        domains=domains,
        traces=traces,
    )
    confidence = _to_float(finalized.get("confidence", trust.get("confidence")), default=0.0)
    freshness = (
        " ".join(str(finalized.get("freshness", trust.get("freshness", "unknown"))).split()).strip()
        or "unknown"
    )
    authority_score = _to_float(
        finalized.get("authority_score", trust.get("authority_score")),
        default=0.0,
    )
    agreement_score = _to_float(
        finalized.get("agreement_score", trust.get("agreement_score")),
        default=0.0,
    )
    claim_citation_coverage = _to_float(finalized.get("claim_citation_coverage"), default=0.0)
    claim_snippet_grounding = _to_float(
        finalized.get("claim_snippet_grounding_coverage", grounding.get("coverage")),
        default=0.0,
    )
    claim_conflict_count = _to_int(
        finalized.get("claim_snippet_conflict_count", grounding.get("conflict_count")),
        default=0,
    )
    vector_result_count = _to_int(vector.get("result_count"), default=0)
    retrieval_top_similarity = _to_float(vector.get("top_similarity"), default=0.0)
    retrieval_query_count = _to_int(vector.get("query_count"), default=0)
    web_result_count = _to_int(web.get("result_count"), default=0)
    selective_before_count = _to_int(selective.get("before_count"), default=0)
    selective_after_count = _to_int(selective.get("after_count"), default=0)

    return {
        "id": idx,
        "question": question,
        "job_id": job_id,
        "status_path": statuses,
        "latency_seconds": round(ended_at - started_at, 3),
        "trace_duration_seconds": round(trace_duration, 3) if trace_duration is not None else 0.0,
        "answer_chars": len(answer or ""),
        "abstained": abstained,
        "abstain_reason": abstain_reason,
        "error": error,
        "trust_confidence": confidence,
        "trust_freshness": freshness,
        "trust_contradiction_flag": bool(
            finalized.get("contradiction_flag", trust.get("contradiction_flag", False))
        ),
        "authority_score": authority_score,
        "agreement_score": agreement_score,
        "claim_citation_coverage": claim_citation_coverage,
        "claim_snippet_grounding_coverage": claim_snippet_grounding,
        "claim_snippet_conflict_count": claim_conflict_count,
        "verification_issues": issues,
        "retrieval_result_count": vector_result_count,
        "retrieval_top_similarity": retrieval_top_similarity,
        "retrieval_query_count": retrieval_query_count,
        "web_result_count": web_result_count,
        "selective_before_count": selective_before_count,
        "selective_after_count": selective_after_count,
        "source_url_count": len(urls),
        "source_domains": domains,
        "source_domain_count": len(domains),
        "auto_escalated_to_deep": any(
            str(event.get("type", "")).strip().lower() == "mode_auto_escalation_completed"
            for event in traces
        ),
        "used_fast_refine": any(
            str(event.get("type", "")).strip().lower() == "fast_refine_started" for event in traces
        ),
        "answer_preview": (answer or "")[:420],
        "trace_event_count": len(traces),
    }


def _error_row(idx: int, question: str, exc: Exception) -> dict:
    return {
        "id": idx,
        "question": question,
        "job_id": "",
        "status_path": [],
        "latency_seconds": 0.0,
        "trace_duration_seconds": 0.0,
        "answer_chars": 0,
        "abstained": False,
        "abstain_reason": "",
        "error": f"{type(exc).__name__}: {exc}",
        "trust_confidence": 0.0,
        "trust_freshness": "unknown",
        "trust_contradiction_flag": False,
        "authority_score": 0.0,
        "agreement_score": 0.0,
        "claim_citation_coverage": 0.0,
        "claim_snippet_grounding_coverage": 0.0,
        "claim_snippet_conflict_count": 0,
        "verification_issues": [],
        "retrieval_result_count": 0,
        "retrieval_top_similarity": 0.0,
        "retrieval_query_count": 0,
        "web_result_count": 0,
        "selective_before_count": 0,
        "selective_after_count": 0,
        "source_url_count": 0,
        "source_domains": [],
        "source_domain_count": 0,
        "auto_escalated_to_deep": False,
        "used_fast_refine": False,
        "answer_preview": "",
        "trace_event_count": 0,
    }


def _resolve_token(args: argparse.Namespace) -> str:
    provided = (args.token or "").strip()
    if provided:
        return provided

    def _generate_from_local_config() -> str:
        from app.core.security import create_access_token

        roles = [role.strip() for role in args.jwt_roles.split(",") if role.strip()]
        return create_access_token(
            user_id=args.user_id,
            roles=roles or ["user"],
            expires_minutes=args.jwt_expires_minutes,
            audience=args.jwt_audience,
        )

    def _load_from_aws_secrets_manager(secret_id: str, region: str | None) -> int:
        import boto3

        client_kwargs = {"region_name": region} if region else {}
        client = boto3.client("secretsmanager", **client_kwargs)
        response = client.get_secret_value(SecretId=secret_id)
        secret_string = str(response.get("SecretString", "")).strip()
        if not secret_string:
            raise RuntimeError("SecretString is empty.")
        payload = json.loads(secret_string)
        if not isinstance(payload, dict):
            raise RuntimeError("Secret payload must be a JSON object.")

        loaded = 0
        for key in (
            "SECURITY_JWT_SECRET",
            "JWT_SECRET",
            "SECURITY_JWT_ISSUER",
            "SECURITY_JWT_AUDIENCE",
            "SECURITY_JWT_EXP_MINUTES",
        ):
            value = payload.get(key)
            if value is None:
                continue
            normalized = str(value).strip()
            if not normalized:
                continue
            os.environ[key] = normalized
            loaded += 1
        return loaded

    first_error: Exception | None = None
    try:
        return _generate_from_local_config()
    except Exception as exc:
        first_error = exc

    aws_secret_id = (
        str(args.aws_secret_id).strip()
        or os.getenv("AWS_SECRETS_MANAGER_SECRET_ID", "").strip()
        or "unigraph/prod/app"
    )
    aws_region = (
        str(args.aws_region).strip()
        or os.getenv("AWS_SECRETS_MANAGER_REGION", "").strip()
        or os.getenv("AWS_REGION", "").strip()
        or os.getenv("AWS_DEFAULT_REGION", "").strip()
        or "us-east-1"
    )

    try:
        loaded_keys = _load_from_aws_secrets_manager(aws_secret_id, aws_region)
        if loaded_keys > 0:
            return _generate_from_local_config()
    except Exception as aws_exc:
        if first_error is not None:
            raise RuntimeError(
                f"local_jwt_generation_failed=({type(first_error).__name__}: {first_error}); "
                f"aws_secret_fallback_failed=({type(aws_exc).__name__}: {aws_exc}); "
                f"aws_secret_id='{aws_secret_id}' aws_region='{aws_region}'"
            ) from aws_exc
        raise

    if first_error is not None:
        raise RuntimeError(
            f"local_jwt_generation_failed=({type(first_error).__name__}: {first_error}); "
            f"aws_secret_id='{aws_secret_id}' did not provide JWT fields"
        ) from first_error
    raise RuntimeError("Unable to resolve JWT token.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run 10-question German-university backend benchmark."
    )
    parser.add_argument(
        "--token",
        default="",
        help="Bearer token for /api/v1/chat/stream. If omitted, JWT is auto-generated.",
    )
    parser.add_argument(
        "--user-id",
        default="eval-user",
        help="User id used in request payload and JWT subject when auto-generating token.",
    )
    parser.add_argument(
        "--jwt-roles",
        default="user",
        help="Comma-separated JWT roles used when --token is omitted.",
    )
    parser.add_argument(
        "--jwt-expires-minutes",
        type=int,
        default=None,
        help="Optional JWT expiry in minutes used when --token is omitted.",
    )
    parser.add_argument(
        "--jwt-audience",
        default=None,
        help="Optional JWT audience override used when --token is omitted.",
    )
    parser.add_argument(
        "--aws-secret-id",
        default="",
        help="AWS Secrets Manager secret id for JWT fallback (default: env or unigraph/prod/app).",
    )
    parser.add_argument(
        "--aws-region",
        default="",
        help="AWS region for JWT fallback (default: env or us-east-1).",
    )
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:8000/api/v1/chat/stream",
        help="Chat stream API URL.",
    )
    parser.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "fast", "deep"],
        help="Execution mode for benchmark queries.",
    )
    parser.add_argument(
        "--session-id",
        default="eval-benchmark-2026-04-09",
        help="Session id used for all benchmark prompts.",
    )
    parser.add_argument(
        "--shared-session",
        action="store_true",
        help=(
            "Use one shared session id for all prompts. "
            "By default each question uses an isolated session id."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of benchmark prompts to run concurrently.",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=10,
        help="Limit number of questions from the default set (1-10).",
    )
    parser.add_argument(
        "--output",
        default="data/reports/german_university_backend_eval_2026-04-09.json",
        help="Output JSON report path.",
    )
    args = parser.parse_args()
    try:
        token = _resolve_token(args)
    except Exception as exc:
        raise SystemExit(
            "Failed to resolve JWT token. Provide --token explicitly or ensure "
            "local JWT config is valid. "
            f"Details: {type(exc).__name__}: {exc}"
        ) from exc

    if not args.token:
        print(
            f"[eval] generated JWT automatically for user_id='{args.user_id}'",
            flush=True,
        )

    max_questions = max(1, min(10, int(args.max_questions)))
    questions = DEFAULT_QUESTIONS[:max_questions]
    concurrency = max(1, int(args.concurrency))
    started_at = time.perf_counter()

    results: list[dict] = []
    if concurrency == 1:
        for idx, question in enumerate(questions, start=1):
            print(f"[eval] {idx}/{len(questions)}", flush=True)
            session_id = args.session_id if args.shared_session else f"{args.session_id}-q{idx}"
            try:
                row = _run_one(
                    api_url=args.api_url,
                    token=token,
                    user_id=args.user_id,
                    session_id=session_id,
                    mode=args.mode,
                    question=question,
                    idx=idx,
                )
            except Exception as exc:
                row = _error_row(idx, question, exc)
            results.append(row)
    else:
        print(
            f"[eval] submitting {len(questions)} prompts with concurrency={concurrency}",
            flush=True,
        )
        rows_by_id: dict[int, dict] = {}
        max_workers = min(concurrency, len(questions))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for idx, question in enumerate(questions, start=1):
                session_id = args.session_id if args.shared_session else f"{args.session_id}-q{idx}"
                future = pool.submit(
                    _run_one,
                    api_url=args.api_url,
                    token=token,
                    user_id=args.user_id,
                    session_id=session_id,
                    mode=args.mode,
                    question=question,
                    idx=idx,
                )
                futures[future] = (idx, question)
            completed = 0
            for future in as_completed(futures):
                idx, question = futures[future]
                completed += 1
                try:
                    row = future.result()
                except Exception as exc:
                    row = _error_row(idx, question, exc)
                rows_by_id[idx] = row
                print(f"[eval] completed {completed}/{len(questions)} (q{idx})", flush=True)
        results = [rows_by_id[index] for index in sorted(rows_by_id.keys())]

    abstain_reason_counts: dict[str, int] = {}
    for row in results:
        if not bool(row.get("abstained", False)):
            continue
        reason = " ".join(str(row.get("abstain_reason", "")).split()).strip().lower() or "unknown"
        abstain_reason_counts[reason] = abstain_reason_counts.get(reason, 0) + 1

    summary = {
        "run_at": datetime.utcnow().isoformat() + "Z",
        "mode": args.mode,
        "concurrency": concurrency,
        "shared_session": bool(args.shared_session),
        "total_questions": len(results),
        "errors": sum(1 for row in results if row.get("error")),
        "abstained": sum(1 for row in results if row.get("abstained")),
        "avg_latency_seconds": _avg([row.get("latency_seconds") for row in results]),
        "avg_trace_duration_seconds": _avg([row.get("trace_duration_seconds") for row in results]),
        "avg_source_url_count": _avg([row.get("source_url_count") for row in results]),
        "avg_source_domain_count": _avg([row.get("source_domain_count") for row in results]),
        "avg_trust_confidence": _avg([row.get("trust_confidence") for row in results]),
        "avg_claim_citation_coverage": _avg(
            [row.get("claim_citation_coverage") for row in results]
        ),
        "avg_claim_snippet_grounding_coverage": _avg(
            [row.get("claim_snippet_grounding_coverage") for row in results]
        ),
        "questions_with_verification_issues": sum(
            1 for row in results if row.get("verification_issues")
        ),
        "questions_with_contradiction_flag": sum(
            1 for row in results if row.get("trust_contradiction_flag")
        ),
        "questions_auto_escalated_to_deep": sum(
            1 for row in results if row.get("auto_escalated_to_deep")
        ),
        "questions_used_fast_refine": sum(1 for row in results if row.get("used_fast_refine")),
        "abstain_reason_counts": abstain_reason_counts,
        "total_wall_seconds": round(time.perf_counter() - started_at, 3),
    }

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"summary": summary, "results": results}
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[eval] wrote {output}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
