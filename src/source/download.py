"""共享下载:DiscoveredTweet(媒体 URL)→ Post(媒体下到 cache)。

ScweetSource / NitterSource 共用。任一媒体下载失败 → 返回 None(整条跳过,下轮重发现重试,
避免半发 + 重试重复)。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from src.source.base import DiscoveredTweet, MediaFile, Post, media_cache_path

logger = logging.getLogger(__name__)


async def download_post(
    dt: DiscoveredTweet, account: str, cache_dir: Path, http: httpx.AsyncClient
) -> Post | None:
    """下载 dt 的全部媒体到 cache → Post。任一失败 → None。"""
    files: list[MediaFile] = []
    for i, ref in enumerate(dt.media, 1):
        path = media_cache_path(
            cache_dir, account, dt.display_name, dt.post_id, dt.timestamp, ref.type, i
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            try:
                resp = await http.get(ref.url)
                resp.raise_for_status()
                path.write_bytes(resp.content)
                logger.info("downloaded %s (%d bytes)", path.name, len(resp.content))
            except Exception as exc:
                logger.warning(
                    "media download failed, skip post %s: %s (%s)", dt.post_id, ref.url, exc
                )
                return None
        files.append(MediaFile(path=path, type=ref.type, url=ref.url))
    return Post(
        post_id=dt.post_id,
        username=account,
        timestamp=dt.timestamp,
        text=dt.text,
        media=files,
        is_retweet=dt.is_retweet,
        url=f"https://x.com/{account}/status/{dt.post_id}",
        display_name=dt.display_name,
    )
