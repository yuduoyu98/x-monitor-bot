"""Data contract and abstract interface for X post fetchers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class MediaItem:
    url: str
    type: str  # "photo" | "video" | "animated_gif"
    width: int | None = None
    height: int | None = None
    bitrate: int | None = None


@dataclass
class Post:
    post_id: str
    username: str
    timestamp: datetime
    text: str
    url: str
    display_name: str = ""
    media: list[MediaItem] = field(default_factory=list)


class BaseFetcher(ABC):
    @abstractmethod
    async def fetch_rss_candidates(
        self,
        username: str,
        skip_retweets: bool = True,
        since: datetime | None = None,
    ) -> list[str]:
        """Return tweet IDs from RSS with media, filtered by optional since."""

    @abstractmethod
    async def resolve_posts(
        self,
        tweet_ids: list[str],
        username: str,
        skip_retweets: bool = True,
    ) -> list[Post]:
        """Resolve tweet IDs to Post objects with media URLs."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""
