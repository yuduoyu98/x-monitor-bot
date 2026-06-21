"""Main orchestration loop: polls X accounts, downloads media, sends to Telegram."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from src.config import AppConfig
from src.database import Database
from src.downloader import MediaDownloader
from src.fetcher.base import BaseFetcher
from src.telegram_bot import TelegramSender

logger = logging.getLogger(__name__)


class Scheduler:
    """Orchestrates fetch → download → send → record pipeline."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        fetcher: BaseFetcher,
        sender: TelegramSender,
        downloader: MediaDownloader,
    ) -> None:
        self._config = config
        self._db = db
        self._fetcher = fetcher
        self._sender = sender
        self._downloader = downloader
        self._chat_id = config.telegram.chat_id
        self._running = False

    async def run(self) -> None:
        self._running = True
        subs = await self._db.get_subscriptions()
        logger.info(
            "Scheduler started (interval=%ds, accounts=%d)",
            self._config.scheduler.loop_interval_seconds,
            len(subs),
        )
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("Scheduler tick failed, will retry")
            await asyncio.sleep(self._config.scheduler.loop_interval_seconds)

    async def _tick(self) -> None:
        subs = await self._db.get_subscriptions()
        for sub in subs:
            await self._process_account(sub)
        await self._cleanup_cache()

    async def _process_account(self, sub: dict) -> None:
        account_id = sub["account_id"]
        if not sub.get("enabled", 1):
            return
        try:
            poll_interval = sub.get("poll_interval_minutes")
            if poll_interval:
                last = await self._db.get_last_post_at(account_id)
                if last is not None:
                    elapsed = (datetime.now(UTC) - last.replace(tzinfo=UTC)).total_seconds()
                    if elapsed < poll_interval * 60:
                        return

            logger.debug("Polling @%s...", account_id)
            since = await self._db.get_last_post_at(account_id)
            if since is None and not sub.get("initialize", True):
                await self._db.set_last_post_at(account_id, datetime.now(UTC).isoformat())
                return

            # Step 1: RSS discovery (uses published timestamps to filter by watermark)
            candidates = await self._fetcher.fetch_rss_candidates(account_id, since=since)
            if not candidates:
                return

            # Step 2: Filter out already-known IDs (DB index lookup, constant time)
            known = await self._db.filter_known_ids(candidates)
            new_ids = [c for c in candidates if c not in known]
            if not new_ids:
                return

            # Step 3: fxTwitter resolution (expensive, only for truly new posts)
            posts = await self._fetcher.resolve_posts(new_ids, account_id)

            # RSS returns newest first; send oldest first for timeline order
            for post in reversed(posts):
                await self._process_post(post, sub)
                await asyncio.sleep(3)  # avoid TG rate limit

            if posts:
                newest = max(p.timestamp for p in posts)
                await self._db.set_last_post_at(account_id, newest.isoformat())

        except Exception:
            logger.exception("Failed to process account @%s", account_id)

    async def _process_post(self, post, sub: dict) -> None:
        if await self._db.is_post_known(post.post_id):
            return

        has_media = bool(post.media)
        sync_mode = sub.get("sync_mode", "media_only")

        if sync_mode == "media_only" and not has_media:
            return

        include_text = sync_mode == "all"

        try:
            file_paths = await self._downloader.download_post_media(post) if has_media else []
            msg_ids = await self._sender.send_post(
                self._chat_id,
                post,
                file_paths=file_paths,
                include_text=include_text,
            )
            await self._db.log_sync(
                post.post_id,
                post.username,
                file_paths=[str(p) for p in file_paths],
                status="synced" if msg_ids else "failed",
                telegram_message_ids=[str(m) for m in msg_ids] if msg_ids else None,
            )
            if msg_ids:
                # Mark as known only after successful send
                await self._db.insert_post(
                    account_id=post.username,
                    post_id=post.post_id,
                    post_time=post.timestamp.isoformat(),
                    post_content=post.text,
                    post_url=post.url,
                    media_count=len(post.media),
                )
                logger.info("✓ @%s: %s", post.username, post.post_id)

        except Exception:
            logger.exception("Failed post %s", post.post_id)
            await self._db.log_sync(
                post.post_id,
                post.username,
                file_paths=[],
                status="failed",
                error_message="Exception during processing",
            )

    async def _cleanup_cache(self) -> None:
        try:
            removed = await self._downloader.cleanup_old_files(self._config.storage.cache_ttl_days)
            if removed > 0:
                logger.info("Cache cleanup: removed %d files", removed)
        except Exception:
            logger.exception("Cache cleanup failed")

    def stop(self) -> None:
        self._running = False
