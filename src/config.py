"""Configuration loading and validation via YAML + Pydantic."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


class TelegramConfig(BaseModel):
    """Telegram bot settings."""

    bot_token: str
    chat_id: str = ""


class FetcherConfig(BaseModel):
    """Fetcher backend selection and backend-specific settings."""

    type: Literal["nitter_fxtwitter"] = "nitter_fxtwitter"
    nitter_instance: str = "https://nitter.net"


class ScweetConfig(BaseModel):
    """Scweet (X GraphQL) source settings."""

    auth_token: str = ""
    proxy: str = ""


class StorageConfig(BaseModel):
    """Local storage settings."""

    cache_dir: str = "./cache"
    db_path: str = "./state.db"
    cache_ttl_days: int = 7


class SchedulerConfig(BaseModel):
    """Scheduler settings."""

    loop_interval_seconds: int = 300


class AppConfig(BaseModel):
    """Top-level application configuration (connections only, no subscriptions)."""

    model_config = {"extra": "ignore"}

    telegram: TelegramConfig
    fetcher: FetcherConfig = FetcherConfig()
    scweet: ScweetConfig = ScweetConfig()
    storage: StorageConfig = StorageConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    source_type: Literal["scweet", "nitter"] = "scweet"


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load and validate configuration from a YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            f"复制 config.example.yaml → config.yaml 并填入实际值(结构见该模板)。"
        )
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw)


def save_config(config: AppConfig, path: str | Path = "config.yaml") -> None:
    """Save an AppConfig back to a YAML file, preserving structure."""
    data = config.model_dump(exclude_none=True, exclude_defaults=False)
    ordered = {
        "telegram": data.pop("telegram", {}),
        "fetcher": data.pop("fetcher", {}),
        "scweet": data.pop("scweet", {}),
        "storage": data.pop("storage", {}),
        "scheduler": data.pop("scheduler", {}),
        "source_type": data.pop("source_type", "scweet"),
    }
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(ordered, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
