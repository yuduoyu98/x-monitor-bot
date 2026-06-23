"""Discovery contract: turn an X account into its recent tweets.

Discovery is deliberately separated from media resolution. A Discoverer only
answers "what tweets did this account post recently?" — it returns tweet IDs
and metadata, never media URLs. This module does NOT call fxTwitter or any
resolver; resolution is a separate concern.

The contract is source-agnostic: Nitter today, token/syndication later — each
implements Discoverer and runs through check_discovery() unchanged.

"不重" (no duplicates) is verified automatically. "不漏" (no misses) and
"are these the right account's IDs" cannot be proven without ground truth —
check_discovery() prints a table (id / time / text / has-media) for manual
comparison against the X profile.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import feedparser
import httpx

logger = logging.getLogger(__name__)

DEFAULT_NITTER_INSTANCE = "https://nitter.net"
_NITTER_INSTANCES = [
    DEFAULT_NITTER_INSTANCE,
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
]
_TWEET_ID_RE = re.compile(r"^\d{15,}$")
_RETWEET_RE = re.compile(r"^(RT\s|♻️|🔁)", re.IGNORECASE)
_HAS_MEDIA_RE = re.compile(r"<img|<video", re.IGNORECASE)
_ID_FROM_URL_RE = re.compile(r"/status/(\d+)")
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class DiscoveredTweet:
    """A single recently-discovered tweet. Source-agnostic."""

    tweet_id: str
    timestamp: datetime
    text: str = ""
    has_media: bool | None = None
    is_retweet: bool = False


class Discoverer(Protocol):
    """Source-agnostic discovery contract. Any backend implements this."""

    async def discover(self, account_id: str, *, limit: int = 20) -> list[DiscoveredTweet]:
        """Return up to `limit` most-recent tweets for account_id, newest first."""
        ...

    async def close(self) -> None:
        """Release resources."""
        ...


# ---------------------------------------------------------------------------
# Nitter implementation
# ---------------------------------------------------------------------------


def _extract_id(entry: dict) -> str | None:
    raw = entry.get("id", "") or entry.get("guid", "") or ""
    if raw.isdigit():
        return raw
    link = entry.get("link", "") or ""
    m = _ID_FROM_URL_RE.search(link) or _ID_FROM_URL_RE.search(raw)
    return m.group(1) if m else None


def _strip_html(s: str) -> str:
    return _TAG_RE.sub(" ", s).strip()


class NitterDiscoverer:
    """Discovers recent tweets via Nitter RSS, with multi-instance fallback."""

    def __init__(self, nitter_instance: str | None = None) -> None:
        primary = (nitter_instance or DEFAULT_NITTER_INSTANCE).rstrip("/")
        seen: set[str] = set()
        self._instances: list[str] = []
        for url in [primary, *_NITTER_INSTANCES]:
            if url not in seen:
                seen.add(url)
                self._instances.append(url)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"User-Agent": "x-monitor-bot/0.1"},
            follow_redirects=True,
        )

    async def discover(self, account_id: str, *, limit: int = 20) -> list[DiscoveredTweet]:
        """Fetch RSS across instances; first one with usable entries wins."""
        for base in self._instances:
            try:
                resp = await self._client.get(f"{base}/{account_id}/rss")
                resp.raise_for_status()
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as e:
                logger.warning("Nitter RSS failed (%s): %s", base, e)
                continue
            except Exception:
                logger.warning("Nitter RSS failed (%s)", base, exc_info=True)
                continue

            tweets = self._parse(resp.text)
            if tweets is None:
                # No entries: instance may have returned an error page as 200 —
                # try the next instance rather than trusting an empty result.
                logger.warning("Nitter %s returned no usable entries for @%s", base, account_id)
                continue
            logger.info("Nitter %s: %d items for @%s", base, len(tweets), account_id)
            # RSS entries aren't guaranteed to be in published-time order — sort
            # newest-first so the contract holds and [:limit] truly takes the
            # most recent N (otherwise a jumbled feed could skip newer tweets).
            tweets.sort(key=lambda t: t.timestamp, reverse=True)
            return tweets[:limit]

        logger.warning("All Nitter instances failed for @%s", account_id)
        return []

    @staticmethod
    def _parse(rss_text: str) -> list[DiscoveredTweet] | None:
        """Parse RSS into tweets. Return None if the feed looks unusable."""
        feed = feedparser.parse(rss_text)
        if not feed.entries:
            return None
        out: list[DiscoveredTweet] = []
        for entry in feed.entries:
            tid = _extract_id(entry)
            if not tid:
                continue
            published = None
            parsed = entry.get("published_parsed")
            if parsed:
                with contextlib.suppress(Exception):
                    published = datetime(*parsed[:6], tzinfo=UTC)
            if published is None:
                published = datetime.now(UTC)
            title = entry.get("title", "") or ""
            summary = entry.get("summary", "") or ""
            snippet = _strip_html(title) or _strip_html(summary)[:120]
            out.append(
                DiscoveredTweet(
                    tweet_id=tid,
                    timestamp=published,
                    text=snippet,
                    has_media=bool(_HAS_MEDIA_RE.search(summary)),
                    is_retweet=bool(_RETWEET_RE.search(title)),
                )
            )
        return out

    async def close(self) -> None:
        await self._client.aclose()


class ScweetDiscoverer:
    """Discovers recent tweets via Scweet (X GraphQL + cookie auth).

    Far more robust than Nitter (real X session, not a dying proxy) but needs:
      - an ``auth_token`` cookie from a logged-in *dedicated* X account
      - a proxy to reach x.com from networks where it's blocked (e.g. mainland CN)

    Scweet is imported lazily so nitter-only users don't need it installed.
    """

    _TS_FMT = "%a %b %d %H:%M:%S %z %Y"

    def __init__(
        self, auth_token: str, proxy: str | None = None, db_path: str = "scweet_state.db"
    ) -> None:
        self._auth_token = auth_token
        self._proxy = proxy
        self._db_path = db_path
        self._client = None  # lazily built: heavy import + one-time network bootstrap

    def _ensure(self):
        if self._client is None:
            from Scweet import Scweet  # lazy: don't force the dep on nitter-only users

            kwargs = {"auth_token": self._auth_token, "db_path": self._db_path}
            if self._proxy:
                kwargs["proxy"] = self._proxy
            self._client = Scweet(**kwargs)
        return self._client

    async def discover(self, account_id: str, *, limit: int = 20) -> list[DiscoveredTweet]:
        client = self._ensure()
        raw = await client.aget_profile_tweets([account_id], limit=limit)
        return [self._convert(t) for t in raw]

    @staticmethod
    def _convert(t: dict) -> DiscoveredTweet:
        ts = t.get("timestamp")
        try:
            timestamp = datetime.strptime(ts, ScweetDiscoverer._TS_FMT) if ts else datetime.now(UTC)
        except ValueError:
            timestamp = datetime.now(UTC)
        media = t.get("media") or {}
        legacy = (t.get("raw") or {}).get("legacy") or {}
        return DiscoveredTweet(
            tweet_id=str(t.get("tweet_id") or ""),
            timestamp=timestamp,
            text=t.get("text") or "",
            has_media=bool(media.get("image_links")),
            # a retweet carries retweeted_status_result inside legacy; originals don't.
            is_retweet="retweeted_status_result" in legacy,
        )

    async def close(self) -> None:
        pass  # Scweet manages its own HTTP session


# ---------------------------------------------------------------------------
# Verification: structural invariants + human-readable report
# ---------------------------------------------------------------------------


@dataclass
class AccountReport:
    """Result of verifying one account's discovery."""

    account_id: str
    count: int
    tweets: list[DiscoveredTweet] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)
    out_of_order: bool = False
    invalid_ids: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Got tweets, no dups, ordered newest-first, all IDs valid.

        Note: this does NOT verify the IDs belong to this account — that needs
        a resolver (fxTwitter) or a human eyeballing the printed table.
        """
        return (
            self.count > 0
            and not self.duplicate_ids
            and not self.out_of_order
            and not self.invalid_ids
        )


async def check_discovery(
    discoverer: Discoverer, account_ids: list[str], *, limit: int = 20
) -> list[AccountReport]:
    """Discover + verify each account. Returns one report per account."""
    reports: list[AccountReport] = []
    for account in account_ids:
        reports.append(await _check_one(discoverer, account, limit))
    return reports


async def _check_one(discoverer: Discoverer, account: str, limit: int) -> AccountReport:
    tweets = await discoverer.discover(account, limit=limit)
    rep = AccountReport(account_id=account, count=len(tweets), tweets=tweets)

    seen: set[str] = set()
    for tweet in tweets:
        if tweet.tweet_id in seen:
            rep.duplicate_ids.append(tweet.tweet_id)
        seen.add(tweet.tweet_id)

    timestamps = [t.timestamp for t in tweets]
    rep.out_of_order = any(a < b for a, b in zip(timestamps, timestamps[1:], strict=False))

    for tweet in tweets:
        if not _TWEET_ID_RE.match(tweet.tweet_id):
            rep.invalid_ids.append(tweet.tweet_id)

    return rep


def format_report(reports: list[AccountReport]) -> str:
    """Render reports as a table for manual comparison against the X profile."""
    lines: list[str] = []
    for rep in reports:
        flag = "PASS" if rep.ok else "FAIL"
        lines.append(f"\n{'=' * 72}")
        lines.append(f"@{rep.account_id}  [{flag}]  {rep.count} tweets")
        if rep.count == 0:
            lines.append("  !! discovery returned nothing — source may be down")
        if rep.duplicate_ids:
            lines.append(f"  duplicates: {rep.duplicate_ids}")
        if rep.out_of_order:
            lines.append("  timestamps NOT newest-first (possible gap/reorder)")
        if rep.invalid_ids:
            lines.append(f"  invalid ids: {rep.invalid_ids}")
        lines.append("")
        lines.append(f"  {'#':>2}  {'time':<17}  {'tweet_id':<20}  med  rt  text")
        lines.append(f"  {'--':>2}  {'---':<17}  {'---':<20}  ---  --  ----")
        for i, tweet in enumerate(rep.tweets, 1):
            time_str = tweet.timestamp.strftime("%Y-%m-%d %H:%M")
            med = "M" if tweet.has_media else "-"
            rt = "RT" if tweet.is_retweet else "-"
            text = (tweet.text or "").replace("\n", " ").strip()[:60]
            lines.append(
                f"  {i:>2}  {time_str:<17}  {tweet.tweet_id:<20}  {med:>3}  {rt:>2}  {text}"
            )
    return "\n".join(lines)
