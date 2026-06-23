"""NitterSource:Nitter RSS 发现 + fxTwitter 解析媒体 + 下载(降级源)。

无需登录、无需代理(Nitter / fxTwitter 没被墙,与 Scweet 的 x.com 不同)。
Nitter 实例不稳定 → 多实例 fallback;fxTwitter 给媒体 CDN 直链。
实现 src.source.base.Source 契约。Nitter RSS 是固定窗口(~20),不支持任意深度回填(回填靠 Scweet)。
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import feedparser
import httpx

from src.source.base import DiscoveredTweet, MediaRef, Post, filter_newer
from src.source.download import download_post

logger = logging.getLogger(__name__)

_NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
]
_FXTWITTER_API = "https://api.fxtwitter.com"
_HAS_MEDIA_RE = re.compile(r"<img|<video", re.IGNORECASE)
_RETWEET_RE = re.compile(r"^(RT\s|♻️|🔁)", re.IGNORECASE)
_ID_FROM_URL_RE = re.compile(r"/status/(\d+)")
_TAG_RE = re.compile(r"<[^>]+>")


def _extract_id(entry: dict) -> str | None:
    raw = entry.get("id", "") or entry.get("guid", "") or ""
    if raw.isdigit():
        return raw
    link = entry.get("link", "") or ""
    m = _ID_FROM_URL_RE.search(link) or _ID_FROM_URL_RE.search(raw)
    return m.group(1) if m else None


class NitterSource:
    """Nitter RSS + fxTwitter 媒体源(降级)。实现 Source 契约。"""

    def __init__(
        self,
        nitter_instance: str | None = None,
        cache_dir: Path | str = Path("./cache"),
    ) -> None:
        primary = (nitter_instance or _NITTER_INSTANCES[0]).rstrip("/")
        seen: set[str] = set()
        self._instances: list[str] = []
        for url in [primary, *_NITTER_INSTANCES]:
            if url not in seen:
                seen.add(url)
                self._instances.append(url)
        self._cache_dir = Path(cache_dir)
        self._http: httpx.AsyncClient | None = None

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                headers={"User-Agent": "x-monitor-bot/0.1"},
                follow_redirects=True,
            )
        return self._http

    async def get_new_posts(
        self, account: str, watermark: datetime | None, *, limit: int = 20
    ) -> list[Post]:
        http = self._ensure_http()
        entries = await self._fetch_rss(http, account)
        if not entries:
            return []
        discovered: list[DiscoveredTweet] = []
        for entry in entries[:limit]:
            dt = await self._to_discovered(http, account, entry)
            if dt is not None:
                discovered.append(dt)
        new = filter_newer(discovered, watermark)
        posts = [await download_post(d, account, self._cache_dir, http) for d in new]
        return [p for p in posts if p is not None]

    async def _fetch_rss(self, http: httpx.AsyncClient, account: str) -> list[dict]:
        for base in self._instances:
            try:
                resp = await http.get(f"{base}/{account}/rss")
                resp.raise_for_status()
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as e:
                logger.warning("Nitter RSS failed (%s): %s", base, e)
                continue
            except Exception:
                logger.warning("Nitter RSS failed (%s)", base, exc_info=True)
                continue
            feed = feedparser.parse(resp.text)
            if not feed.entries:
                logger.warning("Nitter %s no usable entries for @%s", base, account)
                continue
            logger.info("Nitter %s: %d items for @%s", base, len(feed.entries), account)
            return feed.entries
        logger.warning("All Nitter instances failed for @%s", account)
        return []

    async def _to_discovered(
        self, http: httpx.AsyncClient, account: str, entry: dict
    ) -> DiscoveredTweet | None:
        tid = _extract_id(entry)
        if not tid:
            return None
        published = None
        parsed = entry.get("published_parsed")
        if parsed:
            with contextlib.suppress(Exception):
                published = datetime(*parsed[:6], tzinfo=UTC)
        if published is None:
            published = datetime.now(UTC)
        title = entry.get("title", "") or ""
        summary = entry.get("summary", "") or ""
        is_rt = bool(_RETWEET_RE.search(title))
        has_media = bool(_HAS_MEDIA_RE.search(summary))
        # 转推不发(下游 skip_retweets 会跳)→ 不浪费 fxTwitter 调用
        media = await self._resolve_fxtwitter(http, account, tid) if has_media and not is_rt else []
        return DiscoveredTweet(
            post_id=tid,
            timestamp=published,
            text=_TAG_RE.sub(" ", title).strip(),
            is_retweet=is_rt,
            media=media,
        )

    async def _resolve_fxtwitter(
        self, http: httpx.AsyncClient, account: str, tweet_id: str
    ) -> list[MediaRef]:
        try:
            resp = await http.get(f"{_FXTWITTER_API}/{account}/status/{tweet_id}")
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.debug("fxTwitter failed for %s", tweet_id)
            return []
        media_data = (data.get("tweet") or {}).get("media") or {}
        out: list[MediaRef] = []
        for photo in media_data.get("photos", []):
            url = photo.get("url", "")
            if url:
                if "pbs.twimg.com" in url and "?name=" not in url:
                    url += "?name=orig"
                out.append(MediaRef(url=url, type="photo"))
        for video in media_data.get("videos", []):
            url = video.get("url", "")
            if url:
                out.append(MediaRef(url=url, type="video"))
        for gif in media_data.get("gifs", []):
            url = gif.get("url", "")
            if url:
                out.append(MediaRef(url=url, type="animated_gif"))
        return out

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
