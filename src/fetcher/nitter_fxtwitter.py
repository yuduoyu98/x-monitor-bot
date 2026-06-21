"""Nitter RSS + fxTwitter API fetcher — no browser, no login required.

RSS provides published timestamps — watermark filtering happens BEFORE fxTwitter calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import UTC, datetime

import feedparser
import httpx

from src.fetcher.base import BaseFetcher, MediaItem, Post

logger = logging.getLogger(__name__)

_NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
]
_FXTWITTER_API = "https://api.fxtwitter.com"
_MAX_CONCURRENT = 2
_HAS_MEDIA_RE = re.compile(r"<img|<video|#m", re.IGNORECASE)
_RETWEET_RE = re.compile(r"^(RT\s|♻️|🔁)", re.IGNORECASE)


def _extract_tweet_id(url_or_id: str) -> str | None:
    if url_or_id.isdigit():
        return url_or_id
    m = re.search(r"/status/(\d+)", url_or_id)
    if m:
        return m.group(1)
    return None


def _is_retweet_in_rss(entry: dict) -> bool:
    title = entry.get("title", "")
    summary = entry.get("summary", "")
    return bool(_RETWEET_RE.search(title) or _RETWEET_RE.search(summary))


def _has_media_in_rss(entry: dict) -> bool:
    return bool(_HAS_MEDIA_RE.search(entry.get("summary", "")))


def _make_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(8.0, connect=5.0),
        headers={"User-Agent": "x-monitor-bot/0.1"},
        follow_redirects=True,
        limits=httpx.Limits(max_keepalive_connections=1, max_connections=2),
    )


class NitterFxTwitterFetcher(BaseFetcher):
    def __init__(self, nitter_instance: str = "https://nitter.net") -> None:
        self._nitter_instance = nitter_instance.rstrip("/")
        self._nitter_urls = [self._nitter_instance] + [
            u for u in _NITTER_INSTANCES if u != self._nitter_instance
        ]

    # -- Step 1: RSS discovery with timestamp-based filtering --

    async def fetch_rss_candidates(
        self,
        username: str,
        skip_retweets: bool = True,
        since: datetime | None = None,
    ) -> list[str]:
        """Return tweet IDs from RSS with media, filtered by optional since."""
        candidates = await self._do_rss(username, skip_retweets=skip_retweets)
        result = []
        for tid, has_media, pub_dt in candidates:
            if not has_media:
                continue
            if since and pub_dt and pub_dt <= since:
                continue
            result.append(tid)
        return result

    # -- Step 2: fxTwitter resolution (expensive, only for new posts) --

    async def resolve_posts(
        self,
        tweet_ids: list[str],
        username: str,
        skip_retweets: bool = True,
    ) -> list[Post]:
        if not tweet_ids:
            return []
        client = _make_http_client()
        try:
            semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

            async def _resolve_one(tid: str) -> Post | None:
                async with semaphore:
                    return await self._resolve_via_fxtwitter(client, tid, username, skip_retweets)

            results = await asyncio.gather(
                *(_resolve_one(tid) for tid in tweet_ids),
                return_exceptions=True,
            )
            posts: list[Post] = []
            for result in results:
                if isinstance(result, Exception) or result is None:
                    continue
                if result.media:
                    posts.append(result)
            return posts
        finally:
            await client.aclose()

    # -- RSS --

    async def _do_rss(
        self,
        username: str,
        skip_retweets: bool = True,
    ) -> list[tuple[str, bool, datetime | None]]:
        client = _make_http_client()
        try:
            for nitter_url in self._nitter_urls:
                rss_url = f"{nitter_url}/{username}/rss"
                try:
                    resp = await client.get(rss_url)
                    resp.raise_for_status()
                    feed = feedparser.parse(resp.text)
                    items: list[tuple[str, bool, datetime | None]] = []
                    for entry in feed.entries:
                        tweet_id = entry.get("id", "") or entry.get("guid", "")
                        if not (tweet_id and tweet_id.isdigit()):
                            t_id = _extract_tweet_id(entry.get("link", ""))
                            if t_id:
                                tweet_id = t_id
                            else:
                                continue
                        if skip_retweets and _is_retweet_in_rss(entry):
                            continue
                        pub_dt = None
                        pub_parsed = entry.get("published_parsed")
                        if pub_parsed:
                            with contextlib.suppress(Exception):
                                pub_dt = datetime(*pub_parsed[:6], tzinfo=UTC)
                        items.append((tweet_id, _has_media_in_rss(entry), pub_dt))
                    if items:
                        logger.info(
                            "Nitter RSS (%s): %d items for @%s",
                            nitter_url,
                            len(items),
                            username,
                        )
                        return items
                except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as e:
                    logger.warning("Nitter RSS failed (%s): %s", nitter_url, e)
                except Exception:
                    logger.warning("Nitter RSS failed (%s)", nitter_url, exc_info=True)
            return []
        finally:
            await client.aclose()

    # -- fxTwitter --

    async def _resolve_via_fxtwitter(
        self,
        client: httpx.AsyncClient,
        tweet_id: str,
        username: str,
        skip_retweets: bool = True,
    ) -> Post | None:
        api_url = f"{_FXTWITTER_API}/{username}/status/{tweet_id}"
        try:
            resp = await client.get(api_url)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.debug("fxTwitter failed for %s", tweet_id)
            return None

        tweet = data.get("tweet", {})
        if not tweet:
            return None

        if skip_retweets:
            if "retweet" in tweet or "retweeted" in tweet:
                return None
            if "quote" in tweet:
                return None
            if _RETWEET_RE.match(tweet.get("text", "")):
                return None

        created = tweet.get("created_at", "")
        try:
            timestamp = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            timestamp = datetime.now()

        author = tweet.get("author") or tweet.get("user") or {}
        display_name = author.get("name") or author.get("screen_name") or ""
        media_items: list[MediaItem] = []
        media_data = tweet.get("media", {})

        for photo in media_data.get("photos", []):
            url = photo.get("url", "")
            if url:
                if "?name=" not in url and "pbs.twimg.com" in url:
                    url += "?name=orig"
                media_items.append(MediaItem(url=url, type="photo"))

        for video in media_data.get("videos", []):
            url = video.get("url", "")
            if url:
                media_items.append(MediaItem(url=url, type="video"))

        for gif in media_data.get("gifs", []):
            url = gif.get("url", "")
            if url:
                media_items.append(MediaItem(url=url, type="animated_gif"))

        return Post(
            post_id=tweet_id,
            username=username,
            display_name=display_name,
            timestamp=timestamp,
            text=tweet.get("text", ""),
            url=f"https://x.com/{username}/status/{tweet_id}",
            media=media_items,
        )

    async def close(self) -> None:
        pass
