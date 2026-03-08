from pydantic import BaseModel, ConfigDict, Field


class EvaluationConversationLabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:@\-]+$",
    )
    expected_answer: str | None = Field(default=None, min_length=1, max_length=12000)
    relevant_chunk_ids: list[str] | None = Field(default=None, max_length=200)


class EvaluationConversationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    created_at: str
    prompt: str
    answer: str
    retrieval_strategy: str = ""
    retrieved_count: int = Field(ge=0)
    labels: dict = Field(default_factory=dict)
    metrics: dict = Field(default_factory=dict)


class EvaluationConversationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    total_conversations: int = Field(ge=0)
    conversations: list[EvaluationConversationItem] = Field(default_factory=list)


class EvaluationConversationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation: EvaluationConversationItem


class EvaluationReportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    total_conversations: int = Field(ge=0)
    labeled_conversations: int = Field(ge=0)
    retrieval_metrics: dict = Field(default_factory=dict)
    generation_metrics: dict = Field(default_factory=dict)
    conversations: list[EvaluationConversationItem] = Field(default_factory=list)
