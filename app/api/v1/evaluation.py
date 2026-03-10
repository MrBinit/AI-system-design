from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.dependencies.security import (
    authorize_admin_access,
    authorize_user_access,
    get_current_principal,
)
from app.schemas.auth_schema import Principal
from app.schemas.evaluation_schema import (
    EvaluationConversationLabelRequest,
    EvaluationConversationListResponse,
    EvaluationConversationResponse,
    EvaluationReportResponse,
    OfflineEvaluationReportResponse,
    OfflineEvaluationRunResponse,
    OfflineEvaluationStatusResponse,
)
from app.services.evaluation_service import (
    evaluate_trace,
    get_user_evaluation_report,
    label_chat_trace,
    list_chat_traces,
)
from app.services.offline_evaluation_service import (
    build_offline_eval_report,
    get_offline_eval_status,
    run_offline_eval,
)

router = APIRouter()


@router.get("/eval/conversations", response_model=EvaluationConversationListResponse)
async def get_eval_conversations(
    user_id: str = Query(min_length=3, max_length=128),
    limit: int = Query(default=20, ge=1, le=200),
    principal: Principal = Depends(get_current_principal),
):
    """List recent stored chat conversations available for evaluation."""
    authorize_user_access(principal, user_id)
    traces = list_chat_traces(user_id, limit=limit)
    conversations = []
    for trace in traces:
        metrics = evaluate_trace(trace)
        conversations.append(
            {
                "conversation_id": trace.get("conversation_id", ""),
                "created_at": trace.get("created_at", ""),
                "prompt": trace.get("prompt", ""),
                "answer": trace.get("answer", ""),
                "retrieval_strategy": trace.get("retrieval_strategy", ""),
                "retrieved_count": len(trace.get("retrieved_results", [])),
                "labels": trace.get("labels", {}),
                "metrics": metrics,
            }
        )
    return {
        "user_id": user_id,
        "total_conversations": len(conversations),
        "conversations": conversations,
    }


@router.post(
    "/eval/conversations/{conversation_id}/label",
    response_model=EvaluationConversationResponse,
)
async def label_eval_conversation(
    conversation_id: str,
    request: EvaluationConversationLabelRequest,
    principal: Principal = Depends(get_current_principal),
):
    """Attach expected answer and relevant chunks to a stored conversation."""
    authorize_user_access(principal, request.user_id)
    trace = label_chat_trace(
        user_id=request.user_id,
        conversation_id=conversation_id,
        expected_answer=request.expected_answer,
        relevant_chunk_ids=request.relevant_chunk_ids,
    )
    if not trace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found for this user.",
        )

    metrics = evaluate_trace(trace)
    return {
        "conversation": {
            "conversation_id": trace.get("conversation_id", ""),
            "created_at": trace.get("created_at", ""),
            "prompt": trace.get("prompt", ""),
            "answer": trace.get("answer", ""),
            "retrieval_strategy": trace.get("retrieval_strategy", ""),
            "retrieved_count": len(trace.get("retrieved_results", [])),
            "labels": trace.get("labels", {}),
            "metrics": metrics,
        }
    }


@router.get("/eval/report", response_model=EvaluationReportResponse)
async def get_eval_report(
    user_id: str = Query(min_length=3, max_length=128),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_current_principal),
):
    """Return aggregate retrieval and generation evaluation metrics for a user."""
    authorize_user_access(principal, user_id)
    return get_user_evaluation_report(user_id, limit=limit)


@router.get("/eval/offline/status", response_model=OfflineEvaluationStatusResponse)
async def get_eval_offline_status(
    principal: Principal = Depends(get_current_principal),
):
    """Return scheduler status for DynamoDB-based offline evaluation."""
    authorize_admin_access(principal)
    return get_offline_eval_status()


@router.post("/eval/offline/run", response_model=OfflineEvaluationRunResponse)
async def run_eval_offline(
    force: bool = Query(
        default=False,
        description="If true, run immediately regardless of interval/new-data guards.",
    ),
    limit: int | None = Query(default=None, ge=1, le=5000),
    principal: Principal = Depends(get_current_principal),
):
    """Run offline evaluator on demand."""
    authorize_admin_access(principal)
    return await run_offline_eval(limit=limit, force=force)


@router.get("/eval/offline/report", response_model=OfflineEvaluationReportResponse)
async def get_eval_offline_report(
    hours: int = Query(default=24, ge=1, le=720),
    top_bad: int = Query(default=10, ge=0, le=100),
    principal: Principal = Depends(get_current_principal),
):
    """Build and return an on-demand report from offline evaluations."""
    authorize_admin_access(principal)
    return build_offline_eval_report(hours=hours, top_bad=top_bad)
