"""Tests for the scheduler orchestration logic."""

from datetime import UTC, datetime
from pathlib import Path

import yaml

from src.config import load_config
from src.database import Database
from src.fetcher.base import MediaItem, Post

DT = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)


def _make_post(post_id: str = "123", username: str = "test") -> Post:
    return Post(
        post_id=post_id,
        username=username,
        timestamp=DT,
        text="hello world",
        url=f"https://x.com/{username}/status/{post_id}",
        media=[MediaItem(url="https://pbs.twimg.com/test.jpg", type="photo")],
    )


def _make_config(tmp_path: Path):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        yaml.dump(
            {
                "telegram": {"bot_token": "fake_token", "chat_id": "-100"},
                "scheduler": {"loop_interval_seconds": 9999},
            }
        ),
        encoding="utf-8",
    )
    return load_config(str(config_yaml))


def _mock_downloader(tmp_path: Path):
    class MockDownloader:
        def __init__(self, tmp: Path) -> None:
            self.tmp = tmp

        async def download_post_media(self, post):
            post_dir = self.tmp / post.username
            post_dir.mkdir(parents=True, exist_ok=True)
            filepath = post_dir / f"{post.post_id}_1.jpg"
            filepath.write_text("dummy")
            return [filepath]

        async def cleanup_old_files(self, ttl_days):
            return 0

    return MockDownloader(tmp_path)


class TestSchedulerIntegration:
    async def test_new_post_flows(self, tmp_path: Path) -> None:
        from src.scheduler import Scheduler

        config = _make_config(tmp_path)
        db = Database(":memory:")
        await db.init()
        await db.upsert_subscription("test_user", sync_mode="media_only")

        class MockFetcher:
            async def fetch_rss_candidates(self, username, skip_retweets=True, since=None):
                return ["999"]

            async def resolve_posts(self, tweet_ids, username, skip_retweets=True):
                return [_make_post("999", username)]

            async def close(self):
                pass

        class MockSender:
            def __init__(self):
                self.sent: list[str] = []

            async def send_post(self, chat_id, post, file_paths, include_text=True):
                self.sent.append(post.post_id)
                return [1]

            async def send_test_message(self, chat_id):
                return True

        scheduler = Scheduler(
            config=config,
            db=db,
            fetcher=MockFetcher(),
            sender=MockSender(),
            downloader=_mock_downloader(tmp_path),
        )
        await scheduler._tick()
        assert "999" in scheduler._sender.sent
        assert await db.is_post_known("999")
        await db.close()

    async def test_seen_post_is_skipped(self, tmp_path: Path) -> None:
        from src.scheduler import Scheduler

        config = _make_config(tmp_path)
        db = Database(":memory:")
        await db.init()
        await db.upsert_subscription("test_user", sync_mode="media_only")
        await db.insert_post("test_user", "999", DT.isoformat(), "hello", "url", 1)

        class MockFetcher:
            async def fetch_rss_candidates(self, username, skip_retweets=True, since=None):
                return ["999"]

            async def resolve_posts(self, tweet_ids, username, skip_retweets=True):
                return [_make_post("999", username)]

            async def close(self):
                pass

        class MockSender:
            def __init__(self):
                self.count = 0

            async def send_post(self, chat_id, post, file_paths, include_text=True):
                self.count += 1
                return [1]

            async def send_test_message(self, chat_id):
                return True

        scheduler = Scheduler(
            config=config,
            db=db,
            fetcher=MockFetcher(),
            sender=MockSender(),
            downloader=_mock_downloader(tmp_path),
        )
        await scheduler._tick()
        assert scheduler._sender.count == 0
        await db.close()
