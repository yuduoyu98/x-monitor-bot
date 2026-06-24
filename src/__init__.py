"""x-monitor-bot: X (Twitter) media monitor that syncs to Telegram."""

import logging
from pathlib import Path
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")  # 北京时间(东八区):GUI/Sink 时间显示用


def setup_logging(level: int = logging.INFO) -> None:
    """Initialize logging to both console and file.

    Args:
        level: Logging level (default INFO).
    """
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "bot.log", encoding="utf-8"),
        ],
    )
