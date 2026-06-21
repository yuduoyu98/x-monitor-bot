"""Quick Telegram integration test — sends test media to your channel.

Usage:
    .venv/Scripts/python -m src.test_telegram <media_dir>
    .venv/Scripts/python src/test_telegram.py <media_dir>

Reads all image/video files from <media_dir> and sends them as a media group
to the Telegram chat configured in config.yaml.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

# Allow running as script directly
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src import setup_logging
from src.config import load_config
from src.fetcher.base import MediaItem, Post
from src.telegram_bot import TelegramSender

VIDEO_EXTS = {".mp4", ".gif", ".webm", ".mov"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _guess_media_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    return "photo"


def _scan_media(media_dir: Path) -> list[MediaItem]:
    items: list[MediaItem] = []
    for f in sorted(media_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTS | VIDEO_EXTS:
            items.append(
                MediaItem(
                    url=str(f.resolve()),  # local path instead of URL
                    type=_guess_media_type(f),
                )
            )
    return items


async def main() -> None:
    setup_logging()

    if len(sys.argv) < 2:
        print("Usage: python -m src.test_telegram <media_dir>")
        print("  media_dir: directory containing images/videos to send")
        sys.exit(1)

    media_dir = Path(sys.argv[1])
    if not media_dir.is_dir():
        print(f"Error: '{media_dir}' is not a directory")
        sys.exit(1)

    # Load config
    config = load_config()

    if not config.telegram.bot_token or config.telegram.bot_token.startswith("请填写"):
        print("Error: telegram.bot_token not configured in config.yaml")
        sys.exit(1)
    if not config.telegram.chat_id or config.telegram.chat_id.startswith("请填写"):
        print("Error: telegram.chat_id not configured in config.yaml")
        sys.exit(1)

    # Scan media files
    media_items = _scan_media(media_dir)
    if not media_items:
        print(f"No image/video files found in {media_dir}")
        sys.exit(1)

    print(f"Found {len(media_items)} media file(s):")
    for m in media_items:
        name = Path(m.url).name
        print(f"  [{m.type}] {name}")

    # Send as if it's a post
    sender = TelegramSender(bot_token=config.telegram.bot_token)

    # Create a dummy Post for caption
    post = Post(
        post_id="test_000",
        username="test",
        timestamp=datetime.now(UTC),
        text="🧪 Telegram integration test",
        url="",
        media=media_items,
    )

    print(f"\nSending to chat {config.telegram.chat_id}...")
    msg_ids = await sender.send_post_media(
        chat_id=config.telegram.chat_id,
        post=post,
        file_paths=[Path(m.url) for m in media_items],
    )

    if msg_ids:
        print(f"✓ Sent! Message IDs: {msg_ids}")
    else:
        print("✗ Failed to send. Check bot token and chat ID, and ensure bot is a channel admin.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
