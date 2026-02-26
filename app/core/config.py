import yaml
from pathlib import Path
from functools import lru_cache
from app.schemas.settings_schema import Settings

@lru_cache()
def get_settings() -> Settings:
    config_path = Path("config/settings.yaml")

    with open(config_path, "r") as f:
        data = yaml.safe_load(f)

    return Settings(**data)

@lru_cache()
def get_prompts() -> dict:
    prompt_path = Path("config/prompt.yaml")

    with open(prompt_path, "r") as f:
        return yaml.safe_load(f)