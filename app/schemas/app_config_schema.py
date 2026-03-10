from pydantic import BaseModel


class AppConfig(BaseModel):
    name: str
    log_level: str
    docs_enabled: bool = True
    metrics_json_enabled: bool = True
    metrics_json_dir: str = "data/metrics"
    metrics_dynamodb_enabled: bool = False
    metrics_dynamodb_requests_table: str = ""
    metrics_dynamodb_aggregate_table: str = ""
    metrics_dynamodb_ttl_days: int = 0
