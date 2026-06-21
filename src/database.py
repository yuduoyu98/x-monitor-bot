"""SQLite state management: subscriptions, posts, sync log."""

import contextlib
import json
import logging
from datetime import datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS subscriptions (
    account_id TEXT PRIMARY KEY,
    sync_mode TEXT NOT NULL DEFAULT 'media_only',
    sync_retweets INTEGER NOT NULL DEFAULT 0,
    poll_interval_minutes INTEGER NOT NULL DEFAULT 60,
    initialize INTEGER NOT NULL DEFAULT 1,
    remark TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_post_at TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    post_id TEXT NOT NULL UNIQUE,
    post_time TEXT NOT NULL,
    post_content TEXT NOT NULL DEFAULT '',
    post_url TEXT NOT NULL DEFAULT '',
    media_count INTEGER NOT NULL DEFAULT 0,
    detected_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    file_paths TEXT NOT NULL DEFAULT '[]',
    telegram_message_ids TEXT DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('synced','failed')),
    error_message TEXT,
    synced_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_posts_account ON posts(account_id);
CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(post_time);
CREATE INDEX IF NOT EXISTS idx_sync_post ON sync_log(post_id);
CREATE INDEX IF NOT EXISTS idx_sync_status ON sync_log(status);
"""


class Database:
    """Async SQLite database for subscriptions, discovered posts, and sync log."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_SQL)
        # Auto-migrate: add new columns that may not exist yet
        for col, col_def in [
            ("initialize", "INTEGER NOT NULL DEFAULT 1"),
            ("remark", "TEXT NOT NULL DEFAULT ''"),
            ("enabled", "INTEGER NOT NULL DEFAULT 1"),
        ]:
            with contextlib.suppress(Exception):
                await self._conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {col} {col_def}")
        await self._conn.commit()
        logger.info("Database initialized at %s", self._db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # --- subscriptions ---

    async def upsert_subscription(
        self,
        account_id: str,
        sync_mode: str = "media_only",
        sync_retweets: bool = False,
        poll_interval_minutes: int | None = None,
        initialize: bool = True,
        remark: str = "",
    ) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO subscriptions "
            "(account_id, sync_mode, sync_retweets, poll_interval_minutes, initialize, remark) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                account_id,
                sync_mode,
                int(sync_retweets),
                poll_interval_minutes,
                int(initialize),
                remark,
            ),
        )
        await self._conn.commit()

    async def get_subscriptions(self) -> list[dict]:
        cursor = await self._conn.execute("SELECT * FROM subscriptions")
        return [dict(row) for row in await cursor.fetchall()]

    async def toggle_enabled(self, account_id: str) -> bool:
        """Toggle enabled state, return new state."""
        await self._conn.execute(
            "UPDATE subscriptions SET enabled = 1 - enabled WHERE account_id = ?",
            (account_id,),
        )
        await self._conn.commit()
        cursor = await self._conn.execute(
            "SELECT enabled FROM subscriptions WHERE account_id = ?", (account_id,)
        )
        row = await cursor.fetchone()
        return bool(row[0]) if row else False

    async def delete_subscription(self, account_id: str) -> None:
        await self._conn.execute("DELETE FROM subscriptions WHERE account_id = ?", (account_id,))
        await self._conn.commit()

    async def get_last_post_at(self, account_id: str) -> datetime | None:
        """Get the latest post timestamp (watermark) for an account."""
        cursor = await self._conn.execute(
            "SELECT last_post_at FROM subscriptions WHERE account_id = ?", (account_id,)
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return datetime.fromisoformat(row[0])
        return None

    async def set_last_post_at(self, account_id: str, post_time: str | None) -> None:
        """Update the watermark. Pass empty string to clear (reset to full sync)."""
        await self._conn.execute(
            "UPDATE subscriptions SET last_post_at = ? WHERE account_id = ?",
            (post_time or None, account_id),
        )
        await self._conn.commit()

    # --- posts ---

    async def filter_known_ids(self, candidate_ids: list[str]) -> set[str]:
        """Return which of the given candidate IDs are already in the posts table."""
        if not candidate_ids:
            return set()
        placeholders = ",".join("?" * len(candidate_ids))
        cursor = await self._conn.execute(
            f"SELECT post_id FROM posts WHERE post_id IN ({placeholders})",
            candidate_ids,
        )
        return {row[0] for row in await cursor.fetchall()}

    async def is_post_known(self, post_id: str) -> bool:
        cursor = await self._conn.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,))
        return await cursor.fetchone() is not None

    async def insert_post(
        self,
        account_id: str,
        post_id: str,
        post_time: str,
        post_content: str,
        post_url: str,
        media_count: int,
    ) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO posts "
            "(account_id, post_id, post_time, post_content, post_url, media_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (account_id, post_id, post_time, post_content, post_url, media_count),
        )
        await self._conn.commit()

    # --- sync_log ---

    async def log_sync(
        self,
        post_id: str,
        account_id: str,
        file_paths: list[str],
        status: str,
        telegram_message_ids: list[str] | None = None,
        error_message: str | None = None,
    ) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO sync_log "
            "(post_id, account_id, file_paths, telegram_message_ids, status, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                post_id,
                account_id,
                json.dumps(file_paths),
                json.dumps(telegram_message_ids or []),
                status,
                error_message,
            ),
        )
        await self._conn.commit()

    async def get_sync_log(self, limit: int = 50) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM sync_log ORDER BY synced_at DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in await cursor.fetchall()]
