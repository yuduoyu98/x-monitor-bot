"""Fetcher factory and backend implementations."""

from src.config import FetcherConfig
from src.fetcher.base import BaseFetcher, MediaItem, Post
from src.fetcher.nitter_fxtwitter import NitterFxTwitterFetcher

__all__ = [
    "BaseFetcher",
    "MediaItem",
    "Post",
    "NitterFxTwitterFetcher",
    "create_fetcher",
]


def create_fetcher(cfg: FetcherConfig) -> BaseFetcher:
    """Create a fetcher instance based on configuration.

    To add a new fetcher backend:
    1. Create a new module in src/fetcher/
    2. Implement the BaseFetcher interface
    3. Add an elif branch here
    """
    if cfg.type == "nitter_fxtwitter":
        return NitterFxTwitterFetcher(nitter_instance=cfg.nitter_instance)
    raise ValueError(f"Unknown fetcher type: {cfg.type}")
