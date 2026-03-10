from pydantic import BaseModel, Field


class EvaluationRuntimeConfig(BaseModel):
    enabled: bool = False
    dynamodb_table: str = "unigraph-chat-evaluations"
    judge_model_id: str = "us.amazon.nova-2-lite-v1:0"
    batch_size: int = Field(default=25, ge=1, le=200)
    max_items_per_run: int = Field(default=200, ge=1, le=5000)
    lookback_hours: int = Field(default=24, ge=1, le=720)
    ttl_days: int = Field(default=30, ge=0, le=3650)
    schedule_enabled: bool = True
    schedule_interval_hours: int = Field(default=24, ge=1, le=168)
