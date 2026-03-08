from pydantic import BaseModel


class AppConfig(BaseModel):
    name: str
    log_level: str
    docs_enabled: bool = True
    metrics_json_enabled: bool = True
    metrics_json_dir: str = "data/metrics"
