"""Standalone X fetcher test — Nitter RSS + fxTwitter (no browser).

Usage:
    .venv/Scripts/python -m src.test_fetcher [x_username]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src import setup_logging
from src.config import load_config
from src.fetcher.nitter_fxtwitter import NitterFxTwitterFetcher


async def main() -> None:
    setup_logging()
    config = load_config()

    if len(sys.argv) >= 2:
        username = sys.argv[1]
    elif config.subscriptions:
        username = config.subscriptions[0].username
    else:
        print("Usage: python -m src.test_fetcher <x_username>")
        sys.exit(1)

    print(f"=== Testing Nitter + fxTwitter for @{username} ===")
    print(f"Nitter instance: {config.fetcher.nitter_instance}")
    print()

    fetcher = NitterFxTwitterFetcher(
        nitter_instance=config.fetcher.nitter_instance,
    )

    try:
        # Step 1: RSS with media detection
        print("Fetching Nitter RSS (with media detection)...")
        candidates = await fetcher._fetch_nitter_rss(username)
        total = len(candidates)
        media_ids = [tid for tid, has_media in candidates if has_media]
        text_ids = [tid for tid, has_media in candidates if not has_media]
        print(f"  Total: {total} tweets")
        print(f"  With media: {len(media_ids)}")
        print(f"  Text-only: {len(text_ids)} (skipped)\n")

        if media_ids:
            print(f"First 3 media tweet IDs: {media_ids[:3]}")

        # Step 2: Resolve media tweets concurrently
        print("\nResolving media tweets via fxTwitter (concurrent)...")
        posts = await fetcher.fetch_recent_posts(username)
        print(f"\nResolved {len(posts)} posts with media:\n")

        for p in posts:
            print(f"  [{p.post_id}] {p.text[:80]}")
            for m in p.media:
                print(f"    {m.type}: {m.url[:100]}")
            print()

        if posts:
            print("✓ Fetcher works!\n")
            # Download media to cache
            from src.downloader import MediaDownloader

            dl = MediaDownloader(cache_dir=config.storage.cache_dir)
            for p in posts[:3]:  # download first 3
                print(f"Downloading post {p.post_id}...")
                try:
                    paths = await dl.download_post_media(p)
                    for fp in paths:
                        size = fp.stat().st_size
                        print(f"  ✓ {fp}")
                        print(f"    Size: {size / 1024:.0f} KB")
                except Exception as e:
                    print(f"  ✗ Failed: {e}")
            await dl.close()
        else:
            print("⚠ No media posts found.")

    finally:
        await fetcher.close()


if __name__ == "__main__":
    asyncio.run(main())
