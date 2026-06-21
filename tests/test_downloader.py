"""Tests for the media downloader."""

import os
import time
from datetime import UTC, datetime
from pathlib import Path

from src.downloader import MediaDownloader

DT = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)


class TestMediaDownloader:
    def test_cache_dir_created(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "custom_cache"
        MediaDownloader(cache_dir=cache_dir)
        assert cache_dir.exists()

    async def test_cleanup_removes_old_files(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        downloader = MediaDownloader(cache_dir=cache_dir)

        # Create a file in the flat account directory
        account_dir = cache_dir / "test"
        account_dir.mkdir(parents=True)
        file_path = account_dir / "123_1.jpg"
        file_path.write_text("data")

        # Set file mtime to 10 days ago so TTL=1 will remove it
        old_time = time.time() - 10 * 86400
        os.utime(file_path, (old_time, old_time))
        os.utime(account_dir, (old_time, old_time))

        # Cleanup with TTL=1 should remove files older than 1 day
        removed = await downloader.cleanup_old_files(ttl_days=1)
        assert removed >= 1
        assert not file_path.exists()
