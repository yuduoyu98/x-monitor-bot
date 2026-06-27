"""oneshot:链接 → TG 一次性管道(零 DB / 零过滤 / 一次尝试 / 无持久缓存)。

绕开 SyncEngine:parse_tweet_url → fetch_tweet(fxTwitter)→ 媒体下到临时目录
→ 拼 Post → sink.post → 清理。不写 watermark / outbox / cache。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from src.source.base import MediaFile, MediaRef, Post
from src.source.fxtwitter import fetch_tweet, parse_tweet_url

logger = logging.getLogger(__name__)


@dataclass
class OneShotResult:
    """给 GUI 的结果。"""

    ok: bool
    message: str
    media_count: int = 0
    message_ids: list[int] = field(default_factory=list)


async def send_tweet_by_url(url: str, sink) -> OneShotResult:
    """链接 → fxTwitter 取推 → 临时下载 → sink 发送 → 清理。零 DB / 零过滤 / 一次尝试。"""
    tweet_id = parse_tweet_url(url)
    if tweet_id is None:
        return OneShotResult(ok=False, message="无法识别推文链接")

    tmp_dir = tempfile.mkdtemp(prefix="xmon_oneshot_")
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
            ft = await fetch_tweet(http, tweet_id)
            if ft is None:
                return OneShotResult(ok=False, message="取推失败(可能私密/被删/fxTwitter 无缓存)")
            if not ft.text and not ft.media:
                return OneShotResult(ok=False, message="该推文无内容")
            files = await _download_media(ft.media, Path(tmp_dir), http)

        if files is None:
            return OneShotResult(ok=False, message="媒体下载失败")

        post = Post(
            post_id=ft.post_id,
            username=ft.username,
            timestamp=ft.timestamp,
            text=ft.text,
            media=files,
            url=f"https://x.com/{ft.username}/status/{ft.post_id}",
            display_name=ft.display_name,
        )
        try:
            ids = await sink.post(post)
        except Exception as exc:
            return OneShotResult(ok=False, message=f"发送失败: {exc}")
        return OneShotResult(
            ok=True,
            message=f"已发送({len(files)} 媒体)",
            media_count=len(files),
            message_ids=list(ids or []),
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _download_media(
    media: list[MediaRef], tmp_dir: Path, http: httpx.AsyncClient
) -> list[MediaFile] | None:
    """媒体 URL 逐个下到 tmp_dir → MediaFile;任一失败 → None(整条不发,避免半发)。"""
    files: list[MediaFile] = []
    for i, ref in enumerate(media, 1):
        ext = ".mp4" if ref.type in ("video", "animated_gif") else ".jpg"
        path = tmp_dir / f"{i:02d}{ext}"
        try:
            resp = await http.get(ref.url)
            resp.raise_for_status()
            path.write_bytes(resp.content)
        except Exception as exc:
            logger.warning("oneshot media download failed %s: %s", ref.url, exc)
            return None
        files.append(MediaFile(path=path, type=ref.type, url=ref.url))
    return files
