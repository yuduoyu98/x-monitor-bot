"""Entry point for x-monitor-bot."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

# Allow running as script directly: python src/main.py
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src import setup_logging
from src.config import load_config
from src.database import Database
from src.downloader import MediaDownloader
from src.fetcher import create_fetcher
from src.scheduler import Scheduler
from src.telegram_bot import TelegramSender

logger = logging.getLogger(__name__)


async def main() -> None:
    """Load config, wire up components via factory, start scheduler."""
    setup_logging()
    logger.info("x-monitor-bot starting...")

    # Load config
    try:
        config = load_config("config.yaml")
    except FileNotFoundError:
        logger.error("config.yaml not found. Copy config.example.yaml to config.yaml and edit it.")
        sys.exit(1)
    except Exception:
        logger.exception("Failed to load config")
        sys.exit(1)

    # Wire up components
    db = Database(config.storage.db_path)
    await db.init()

    fetcher = create_fetcher(config.fetcher)
    sender = TelegramSender(config.telegram.bot_token)
    downloader = MediaDownloader(
        cache_dir=config.storage.cache_dir,
    )

    scheduler = Scheduler(
        config=config,
        db=db,
        fetcher=fetcher,
        sender=sender,
        downloader=downloader,
    )

    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        logger.info("Received shutdown signal, stopping...")
        scheduler.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown)

    try:
        await scheduler.run()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
    finally:
        logger.info("Shutting down...")
        await fetcher.close()
        await downloader.close()
        await db.close()
        logger.info("x-monitor-bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
