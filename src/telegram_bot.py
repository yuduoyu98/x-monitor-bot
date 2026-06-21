"""Telegram bot sender for posting media to channels/groups.

Uses python-telegram-bot v21+. Sends media groups (albums) for posts
with multiple images/videos. Caption includes truncated post text,
author, and original link.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

from src.fetcher.base import Post

logger = logging.getLogger(__name__)

TELEGRAM_MAX_MEDIA_GROUP = 10
TELEGRAM_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
TELEGRAM_CAPTION_MAX_LENGTH = 1024
TELEGRAM_RATE_LIMIT_SLEEP = 3.0  # seconds between media groups


class TelegramSender:
    """Sends posts (media + text) to Telegram channels/groups."""

    def __init__(self, bot_token: str) -> None:
        self.bot = Bot(token=bot_token)

    async def send_post(
        self,
        chat_id: str,
        post: Post,
        file_paths: list[Path],
        include_text: bool = True,
    ) -> list[int] | None:
        """Send a post — media group if has files, plain text if not.

        Args:
            chat_id: TG chat ID.
            post: Post metadata.
            file_paths: Local media files (may be empty for text-only).
            include_text: Whether to send text (caption or standalone message).

        Returns:
            List of TG message IDs, or None if nothing was sent.
        """
        if file_paths:
            return await self._send_media_group(chat_id, post, file_paths)

        if include_text and post.text:
            caption = _build_caption(post)
            try:
                msg = await self.bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    parse_mode=ParseMode.HTML,
                )
                logger.info("Sent text to %s for post %s", chat_id, post.post_id)
                return [msg.message_id]
            except TelegramError:
                logger.exception("Failed to send text for post %s", post.post_id)
                return None

        return None

    async def _send_media_group(
        self, chat_id: str, post: Post, file_paths: list[Path]
    ) -> list[int] | None:
        """Send all media files from a post as a media group.

        Args:
            chat_id: Telegram chat ID (channel or group).
            post: The Post containing metadata for the caption.
            file_paths: Local paths to media files to upload.

        Returns:
            List of Telegram message IDs, or None if sending failed.
        """
        if not file_paths:
            logger.warning("No files to send for post %s", post.post_id)
            return None

        # Filter out files > 50MB (Telegram bot limit)
        valid_paths = []
        for fp in file_paths:
            try:
                size = fp.stat().st_size
            except OSError:
                logger.warning("Cannot stat file: %s", fp)
                continue
            if size > TELEGRAM_MAX_FILE_SIZE:
                logger.warning("Skipping file >50MB: %s (%d MB)", fp, size // (1024 * 1024))
                continue
            valid_paths.append(fp)

        if not valid_paths:
            return None

        # Build media group
        caption = _build_caption(post)
        media_group = []

        for i, fp in enumerate(valid_paths):
            cap = caption if i == 0 else None  # caption only on first item
            suffix = fp.suffix.lower()

            try:
                file_bytes = fp.read_bytes()
            except OSError:
                logger.exception("Failed to read file: %s", fp)
                continue

            if suffix in (".mp4", ".gif"):
                media_group.append(
                    InputMediaVideo(
                        media=file_bytes,
                        caption=cap,
                        parse_mode=ParseMode.HTML,
                    )
                )
            else:
                media_group.append(
                    InputMediaPhoto(
                        media=file_bytes,
                        caption=cap,
                        parse_mode=ParseMode.HTML,
                    )
                )

            # TG limits media groups to 10 items
            if len(media_group) >= TELEGRAM_MAX_MEDIA_GROUP:
                logger.warning(
                    "Media group capped at %d items (post %s has %d)",
                    TELEGRAM_MAX_MEDIA_GROUP,
                    post.post_id,
                    len(valid_paths),
                )
                break

        if not media_group:
            return None

        for _attempt in range(3):
            try:
                messages = await self.bot.send_media_group(chat_id=chat_id, media=media_group)
                message_ids = [m.message_id for m in messages]
                logger.info(
                    "Sent %d media to chat %s for post %s",
                    len(message_ids),
                    chat_id,
                    post.post_id,
                )
                return message_ids
            except RetryAfter as e:
                wait = e.retry_after + 1
                logger.warning("TG rate limit, waiting %ds...", wait)
                await asyncio.sleep(wait)
            except TelegramError:
                logger.exception("Failed to send media group for post %s", post.post_id)
                return None
        return None

    async def send_test_message(self, chat_id: str) -> bool:
        """Send a test message to verify the bot is configured correctly.

        Args:
            chat_id: Telegram chat ID to send to.

        Returns:
            True if the message was sent successfully.
        """
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text="🤖 <b>x-monitor-bot</b> is online!\n\nMonitoring X accounts for new media.",
                parse_mode=ParseMode.HTML,
            )
            return True
        except TelegramError:
            logger.exception("Failed to send test message")
            return False


def _build_caption(post: Post) -> str:
    """Build a caption for the media group from post metadata.

    Format:
        <truncated post text>...

        💬 <post text>

        <display_name>(@<username>)
        🆔 <post_id>
        🔗 https://x.com/username/status/post_id
    """
    text = post.text.replace("<", "&lt;").replace(">", "&gt;")
    if len(text) > TELEGRAM_CAPTION_MAX_LENGTH:
        text = text[: TELEGRAM_CAPTION_MAX_LENGTH - 3] + "..."

    time_str = post.timestamp.strftime("%Y-%m-%d %H:%M UTC")
    author = f"#{post.display_name} #{post.username}" if post.display_name else f"#{post.username}"
    return f"📅 {time_str}\n💬 {text}\n\n🆔 {author}\n🔗 {post.url}"
