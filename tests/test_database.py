"""Tests for SQLite database operations."""

import pytest

from src.database import Database


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.init()
    yield database
    await database.close()


class TestSubscriptions:
    async def test_upsert_and_get(self, db: Database) -> None:
        await db.upsert_subscription("test_user", sync_mode="media_only")
        subs = await db.get_subscriptions()
        assert len(subs) == 1
        assert subs[0]["account_id"] == "test_user"

    async def test_upsert_replaces(self, db: Database) -> None:
        await db.upsert_subscription("test_user", sync_mode="media_only")
        await db.upsert_subscription("test_user", sync_mode="all", sync_retweets=True)
        subs = await db.get_subscriptions()
        assert len(subs) == 1

    async def test_last_post_at(self, db: Database) -> None:
        await db.upsert_subscription("test_user")
        result = await db.get_last_post_at("test_user")
        assert result is None
        await db.set_last_post_at("test_user", "2024-01-15T12:00:00")
        result = await db.get_last_post_at("test_user")
        assert result is not None


class TestPosts:
    async def test_is_post_known_false_initially(self, db: Database) -> None:
        assert not await db.is_post_known("12345")

    async def test_insert_and_check(self, db: Database) -> None:
        await db.insert_post("test_user", "12345", "2024-01-15T12:00:00", "hello", "url", 1)
        assert await db.is_post_known("12345")

    async def test_insert_same_post_twice_no_error(self, db: Database) -> None:
        await db.insert_post("test_user", "12345", "2024-01-15T12:00:00", "hello", "url", 1)
        await db.insert_post("test_user", "12345", "2024-01-15T12:00:00", "hello", "url", 1)
        assert await db.is_post_known("12345")


class TestSyncLog:
    async def test_log_and_retrieve(self, db: Database) -> None:
        await db.log_sync("12345", "test_user", file_paths=["/cache/1.jpg"], status="synced")
        logs = await db.get_sync_log(limit=10)
        assert len(logs) == 1
        assert logs[0]["post_id"] == "12345"
        assert logs[0]["status"] == "synced"

    async def test_log_failed_with_error(self, db: Database) -> None:
        await db.log_sync(
            "12345",
            "test_user",
            file_paths=[],
            status="failed",
            error_message="Timeout",
        )
        logs = await db.get_sync_log()
        assert logs[0]["status"] == "failed"
        assert logs[0]["error_message"] == "Timeout"
