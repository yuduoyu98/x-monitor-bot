"""Diagnostic: dump raw Nitter RSS feed for an account.

Usage:
    .venv/Scripts/python -m src.diagnose <account_id>
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import feedparser
import httpx

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.config import load_config


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m src.diagnose <account_id>")
        sys.exit(1)
    username = sys.argv[1]

    config = load_config()
    client = httpx.AsyncClient(
        timeout=10.0,
        follow_redirects=True,
        headers={"User-Agent": "x-monitor-bot/0.1"},
    )

    try:
        nitter = config.fetcher.nitter_instance.rstrip("/")
        rss_url = f"{nitter}/{username}/rss"
        print(f"Fetching {rss_url} ...\n")
        resp = await client.get(rss_url)
        print(f"Status: {resp.status_code}")
        print(f"Content-Type: {resp.headers.get('content-type', '?')}")
        print(f"Length: {len(resp.text)} bytes\n")

        feed = feedparser.parse(resp.text)
        print(f"Feed entries: {len(feed.entries)}")

        if not feed.entries:
            print("\nRaw response preview:")
            print(resp.text[:2000])
            return

        print()
        for i, entry in enumerate(feed.entries):
            print(f"--- Entry {i + 1} ---")
            for key, val in entry.items():
                s = str(val)[:200]
                print(f"  {key}: {s}")
            print()

    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
