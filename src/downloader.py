"""Simple async HTTP media downloader.

Downloads media files from CDN URLs to local cache. Uses httpx.AsyncClient.
No gallery-dl dependency — we receive direct CDN URLs from the fetcher.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from src.fetcher.base import Post

logger = logging.getLogger(__name__)


class MediaDownloader:
    """Downloads post media from CDN URLs to a local cache directory.

    Directory structure:
        cache/{username}/twitter_{username}_{YYYYMMDD-HHMMSS}_{post_id}_{type}.{ext}
    """

    def __init__(
        self,
        cache_dir: str | Path = "./cache",
        max_concurrent: int = 3,
        timeout: float = 60.0,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_connections=max_concurrent),
            headers={"Referer": "https://x.com/"},
            follow_redirects=True,
        )

    async def download_post_media(self, post: Post) -> list[Path]:
        """Download all media files from a single post.

        Args:
            post: Post object with media URLs to download.

        Returns:
            List of local file paths for the downloaded media.
        """
        if post.display_name:
            folder_name = f"{post.display_name}(@{post.username})"
        else:
            folder_name = post.username
        account_dir = self.cache_dir / folder_name
        account_dir.mkdir(parents=True, exist_ok=True)

        paths: list[Path] = []
        for i, media in enumerate(post.media, 1):
            ext = ".mp4" if media.type in ("video", "animated_gif") else ".jpg"
            date_str = post.timestamp.strftime("%Y%m%d-%H%M%S")
            media_type = media.type if media.type in ("video", "animated_gif") else "photo"
            name = post.display_name or post.username
            suffix = f"-{i}" if len(post.media) > 1 else ""
            filename = (
                f"twitter_{name}(@{post.username})_{date_str}"
                f"_{post.post_id}_{media_type}{suffix}{ext}"
            )
            filepath = account_dir / filename

            if filepath.exists():
                logger.debug("File already cached: %s", filepath)
                paths.append(filepath)
                continue

            try:
                logger.debug("Downloading %s -> %s", media.url, filepath)
                resp = await self._client.get(media.url)
                resp.raise_for_status()
                filepath.write_bytes(resp.content)
                logger.info("Downloaded %s (%d bytes)", filepath, len(resp.content))
            except Exception:
                logger.exception("Failed to download %s", media.url)
                raise

            paths.append(filepath)

        return paths

    async def cleanup_old_files(self, ttl_days: int) -> int:
        """Delete cached files older than the specified TTL.

        Args:
            ttl_days: Maximum age in days for cached files.
                      Use 0 or negative to skip cleanup (keep forever).

        Returns:
            Number of directories removed.
        """
        if ttl_days <= 0:
            return 0

        import time

        cutoff = time.time() - (ttl_days * 86400)
        removed = 0

        if not self.cache_dir.exists():
            return 0

        for user_dir in self.cache_dir.iterdir():
            if not user_dir.is_dir():
                continue
            for f in user_dir.iterdir():
                if not f.is_file():
                    continue
                try:
                    mtime = f.stat().st_mtime
                    if mtime < cutoff:
                        f.unlink()
                        removed += 1
                        logger.debug("Cleaned up expired cache: %s", f)
                except OSError:
                    logger.warning("Failed to clean up %s", f)

        return removed

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
