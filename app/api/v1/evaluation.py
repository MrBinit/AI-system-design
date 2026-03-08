from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.dependencies.security import authorize_user_access, get_current_principal
from app.schemas.auth_schema import Principal
from app.schemas.evaluation_schema import (
    EvaluationConversationLabelRequest,
    EvaluationConversationListResponse,
    EvaluationConversationResponse,
    EvaluationReportResponse,
)
from app.services.evaluation_service import (
    evaluate_trace,
    get_user_evaluation_report,
    label_chat_trace,
    list_chat_traces,
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
