"""Source 契约 + 纯逻辑(SP1)。

DiscoveredTweet = 已发现、未下载的中间态;Post = 已下载媒体的最终态。
filter_newer = 按水位线做增量过滤(契约的核心增量语义)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class MediaRef:
    """待下载的媒体引用(url + 类型)。"""

    url: str
    type: str  # "photo" | "video" | "animated_gif"


@dataclass
class DiscoveredTweet:
    """已发现、未下载的推文(Source 抓取后的中间态)。"""

    post_id: str
    timestamp: datetime
    text: str = ""
    is_retweet: bool = False
    display_name: str = ""
    media: list[MediaRef] = field(default_factory=list)


def filter_newer(items: list[DiscoveredTweet], watermark: datetime | None) -> list[DiscoveredTweet]:
    """返回严格晚于 watermark 的推(水位线 = 已处理到的点),newest-first。

    watermark=None 表示无游标(首次)→ 返回全部。仍按时间倒序。
    """
    chosen = items if watermark is None else [t for t in items if t.timestamp > watermark]
    return sorted(chosen, key=lambda t: t.timestamp, reverse=True)


def media_cache_path(
    cache_dir: Path,
    username: str,
    display_name: str,
    post_id: str,
    timestamp: datetime,
    media_type: str,
    index: int = 1,
) -> Path:
    """媒体在 cache 中的确定性路径(同输入 → 同路径 → 重试时已存在则跳过下载)。

    目录按 username(稳定键);文件名含 display_name 便于人读。
    photo → .jpg;video/animated_gif → .mp4。
    """
    ext = ".mp4" if media_type in ("video", "animated_gif") else ".jpg"
    name = display_name or username
    date_str = timestamp.strftime("%Y%m%d-%H%M%S")
    filename = f"twitter_{name}(@{username})_{date_str}_{post_id}_{media_type}_{index}{ext}"
    return cache_dir / username / filename


@dataclass
class MediaFile:
    """一个已下载到 cache 的媒体文件。"""

    path: Path
    type: str  # "photo" | "video" | "animated_gif"
    url: str = ""


@dataclass
class Post:
    """Source 的最终产物:推文 + 已下载好的媒体(直接能交给 Sink)。"""

    post_id: str
    username: str
    timestamp: datetime
    text: str
    media: list[MediaFile]
    is_retweet: bool = False
    url: str = ""
    display_name: str = ""


@runtime_checkable
class Source(Protocol):
    """可切换的媒体源契约。"""

    async def get_new_posts(
        self, account: str, watermark: datetime | None, *, limit: int = 20
    ) -> list[Post]:
        """返回严格晚于 watermark 的推(已下载媒体到 cache),newest-first。"""
        ...

    async def close(self) -> None:
        """释放资源。"""
        ...


@runtime_checkable
class Sink(Protocol):
    """可切换的下游契约(SP3)。"""

    async def post(self, post: Post) -> list[int]:
        """发送一条 Post(含媒体)→ 返回下游消息 id;失败抛异常。"""
        ...

    async def close(self) -> None:
        """释放资源。"""
        ...
