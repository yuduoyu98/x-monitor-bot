"""Entry point:wire Database + Source + Sink → SyncEngine loop。

    python -m src.main

需要环境变量 SCWEET_AUTH_TOKEN(专用号 cookie);config.yaml 提供 telegram/storage/scheduler。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
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
from src.source.scweet import ScweetSource
from src.sync_engine import run_loop
from src.telegram_bot import TelegramSink

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    logger.info("x-monitor-bot starting...")

    config = load_config("config.yaml")
    auth_token = os.environ.get("SCWEET_AUTH_TOKEN")
    if not auth_token:
        logger.error("SCWEET_AUTH_TOKEN env var required (专用号 auth_token cookie)")
        sys.exit(1)

    db = Database(config.storage.db_path)
    await db.init()
    source = ScweetSource(auth_token=auth_token, cache_dir=config.storage.cache_dir)
    sink = TelegramSink(bot_token=config.telegram.bot_token, chat_id=config.telegram.chat_id)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        logger.info("shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown)

    try:
        await run_loop(
            db,
            source,
            sink,
            loop_interval=config.scheduler.loop_interval_seconds,
            stop_event=stop_event,
        )
    finally:
        logger.info("shutting down...")
        await source.close()
        await sink.close()
        await db.close()
        logger.info("stopped.")


if __name__ == "__main__":
    asyncio.run(main())
