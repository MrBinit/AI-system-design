import asyncio
import os
from datetime import datetime, timezone
import boto3
from boto3.dynamodb.types import TypeDeserializer
from app.core.config import get_settings
from app.scripts.eval_daily_report import _build_report, _load_eval_rows
from app.scripts.eval_dynamodb_worker import run as run_eval_worker

settings = get_settings()
_deserializer = TypeDeserializer()
_scheduler_task: asyncio.Task | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _region_name() -> str | None:
    return (
        os.getenv("AWS_REGION", "").strip()
        or os.getenv("AWS_DEFAULT_REGION", "").strip()
        or os.getenv("AWS_SECRETS_MANAGER_REGION", "").strip()
        or None
    )


def _dynamodb_client():
    kwargs = {"region_name": _region_name()} if _region_name() else {}
    return boto3.client("dynamodb", **kwargs)


def _deserialize(item: dict) -> dict:
    return {key: _deserializer.deserialize(value) for key, value in item.items()}


def _latest_timestamp_from_table(
    table_name: str, include_outcome_filter: bool = False
) -> datetime | None:
    if not table_name:
        return None

    ddb = _dynamodb_client()
    latest: datetime | None = None
    last_key = None
    while True:
        kwargs = {
            "TableName": table_name,
            "ProjectionExpression": "#ts,outcome",
            "ExpressionAttributeNames": {"#ts": "timestamp"},
            "Limit": 200,
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        response = ddb.scan(**kwargs)
        for raw in response.get("Items", []):
            row = _deserialize(raw)
            if include_outcome_filter and str(row.get("outcome", "")) != "success":
                continue
            parsed = _parse_iso(str(row.get("timestamp", "")))
            if parsed and (latest is None or parsed > latest):
                latest = parsed
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
    return latest


def get_offline_eval_status() -> dict:
    """Return readiness and scheduling status for offline DynamoDB evaluations."""
    if not settings.evaluation.enabled:
        return {
            "enabled": False,
            "schedule_enabled": settings.evaluation.schedule_enabled,
            "interval_hours": settings.evaluation.schedule_interval_hours,
            "has_new_requests": False,
            "due_by_interval": False,
            "should_auto_run": False,
            "last_request_timestamp": "",
            "last_evaluated_timestamp": "",
            "reason": "evaluation disabled",
        }

    requests_table = settings.app.metrics_dynamodb_requests_table.strip()
    eval_table = settings.evaluation.dynamodb_table.strip()
    if not requests_table or not eval_table:
        return {
            "enabled": True,
            "schedule_enabled": settings.evaluation.schedule_enabled,
            "interval_hours": settings.evaluation.schedule_interval_hours,
            "has_new_requests": False,
            "due_by_interval": False,
            "should_auto_run": False,
            "last_request_timestamp": "",
            "last_evaluated_timestamp": "",
            "reason": "missing DynamoDB table configuration",
        }

    last_request_ts = _latest_timestamp_from_table(requests_table, include_outcome_filter=True)
    last_eval_ts = _latest_timestamp_from_table(eval_table, include_outcome_filter=False)
    has_new_requests = bool(
        last_request_ts and (not last_eval_ts or last_request_ts > last_eval_ts)
    )

    now = _utc_now()
    interval_seconds = settings.evaluation.schedule_interval_hours * 3600
    if last_eval_ts is None:
        due = True
    else:
        due = (now - last_eval_ts).total_seconds() >= interval_seconds

    should_auto_run = (
        settings.evaluation.schedule_enabled
        and due
        and has_new_requests
        and settings.evaluation.enabled
    )
    return {
        "enabled": True,
        "schedule_enabled": settings.evaluation.schedule_enabled,
        "interval_hours": settings.evaluation.schedule_interval_hours,
        "has_new_requests": has_new_requests,
        "due_by_interval": due,
        "should_auto_run": should_auto_run,
        "last_request_timestamp": last_request_ts.isoformat() if last_request_ts else "",
        "last_evaluated_timestamp": last_eval_ts.isoformat() if last_eval_ts else "",
        "reason": "ok",
    }


async def run_offline_eval(limit: int | None = None, force: bool = False) -> dict:
    """Run offline evaluation now or skip based on schedule/new-data gates."""
    status = get_offline_eval_status()
    if not settings.evaluation.enabled:
        return {
            "ran": False,
            "reason": "evaluation disabled",
            "result": {"evaluated": 0, "skipped": 0},
        }

    if not force and not status.get("should_auto_run", False):
        if not status.get("has_new_requests", False):
            reason = "no new successful requests since last evaluation"
        elif not status.get("due_by_interval", False):
            reason = "interval not reached yet"
        else:
            reason = "schedule disabled"
        return {
            "ran": False,
            "reason": reason,
            "result": {"evaluated": 0, "skipped": 0},
            "status": status,
        }

    result = await run_eval_worker(limit=limit)
    updated_status = get_offline_eval_status()
    return {"ran": True, "reason": "ok", "result": result, "status": updated_status}


def build_offline_eval_report(hours: int, top_bad: int) -> dict:
    """Build an on-demand evaluation report directly from the evaluation DynamoDB table."""
    rows = _load_eval_rows(hours=hours)
    return _build_report(rows=rows, top_bad=top_bad, window_hours=hours)


async def _scheduler_loop():
    interval_seconds = max(300, settings.evaluation.schedule_interval_hours * 3600)
    while True:
        try:
            await run_offline_eval(limit=settings.evaluation.batch_size, force=False)
        except Exception:
            # Scheduler is best-effort and should not crash the app.
            pass
        await asyncio.sleep(interval_seconds)


def start_offline_eval_scheduler() -> None:
    """Start background scheduler for periodic offline evaluations."""
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    if not settings.evaluation.enabled or not settings.evaluation.schedule_enabled:
        return
    _scheduler_task = asyncio.create_task(_scheduler_loop())


async def stop_offline_eval_scheduler() -> None:
    """Stop the background offline evaluation scheduler."""
    global _scheduler_task
    if _scheduler_task is None:
        return
    _scheduler_task.cancel()
    try:
        await _scheduler_task
    except asyncio.CancelledError:
        pass
    _scheduler_task = None
