from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
APP_CONFIG_DIR = APP_DIR / "config"


def resolve_project_path(path_value: str | Path) -> Path:
    """Resolve a path against the project root unless it is already absolute."""
    path = Path(path_value)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()
