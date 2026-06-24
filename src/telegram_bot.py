"""TelegramSink:把 Post 发到 Telegram 频道(SP3)。

实现 src.source.base.Sink 契约:post(post) -> 消息 id 列表;失败抛异常
(交由 SyncEngine 重试 / 转 dead_letter,绝不静默丢)。
"""

from __future__ import annotations

import asyncio
import logging
import re

from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

from src import CN_TZ
from src.source.base import MediaFile, Post

logger = logging.getLogger(__name__)

TELEGRAM_MAX_MEDIA_GROUP = 10
TELEGRAM_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB(bot API 上传上限)
TELEGRAM_CAPTION_MAX = 1024


class MediaTooLargeError(Exception):
    """媒体超过 TG bot API 50MB 上限 → TelegramSink 发文本降级(caption + 警告 + 链接)。"""


def _build_caption(post: Post) -> str:
    text = post.text.replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\s*https://t\.co/\S+", "", text)  # 去掉 X 自动加的 t.co 媒体短链
    if len(text) > TELEGRAM_CAPTION_MAX:
        text = text[: TELEGRAM_CAPTION_MAX - 3] + "..."
    time_str = post.timestamp.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M")
    author = f"#{post.display_name} #{post.username}" if post.display_name else f"#{post.username}"
    return f"📅 {time_str}\n💬 {text}\n\n🆔 {author}\n🔗 {post.url}"


def _is_video(media: MediaFile) -> bool:
    return media.type in ("video", "animated_gif") or media.path.suffix.lower() in (".mp4", ".gif")


class TelegramSink:
    """发 Post 到 Telegram(media group 或纯文本)。实现 Sink 契约。"""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        from telegram.request import HTTPXRequest

        # ptb 默认 write_timeout ~15s,大视频走代理上传会超时 → 客户端报错但 TG 服务端已收到。
        # 加大:上传(write)120s、读响应(read)60s。
        self.bot = Bot(
            token=bot_token,
            request=HTTPXRequest(
                connect_timeout=10, read_timeout=60, write_timeout=120, pool_timeout=10
            ),
        )
        self.chat_id = chat_id

    async def post(self, post: Post) -> list[int]:
        if post.media:
            try:
                return await self._send_media_group(post)
            except MediaTooLargeError as e:
                logger.warning("post %s 媒体>50MB,发文本降级: %s", post.post_id, e)
                return await self._send_text_fallback(post)
        if post.text:
            msg = await self.bot.send_message(
                chat_id=self.chat_id, text=_build_caption(post), parse_mode=ParseMode.HTML
            )
            return [msg.message_id]
        return []

    async def _send_text_fallback(self, post: Post) -> list[int]:
        """媒体>50MB(TG 发不了)→ 发文本:caption + ⚠️ 警告 + 原链接。标为已发,不重试。"""
        text = _build_caption(post) + "\n\n⚠️ 媒体文件过大(>50MB),请点击原链接查看 👆"
        msg = await self.bot.send_message(
            chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML
        )
        logger.info("sent text fallback (media>50MB) for %s", post.post_id)
        return [msg.message_id]

    async def _send_media_group(self, post: Post) -> list[int]:
        # 任一文件 >50MB → raise(不静默丢;SyncEngine 会重试/转 dead)
        for f in post.media:
            size = f.path.stat().st_size
            if size > TELEGRAM_MAX_FILE_SIZE:
                raise MediaTooLargeError(f"{f.path.name} ({size // (1024 * 1024)}MB) > 50MB")

        group = []
        for i, f in enumerate(post.media[:TELEGRAM_MAX_MEDIA_GROUP]):
            cap = _build_caption(post) if i == 0 else None
            data = f.path.read_bytes()
            if _is_video(f):
                group.append(InputMediaVideo(media=data, caption=cap, parse_mode=ParseMode.HTML))
            else:
                group.append(InputMediaPhoto(media=data, caption=cap, parse_mode=ParseMode.HTML))
        if not group:
            return []

        # RetryAfter 退避重试;其他 TelegramError 直接抛(交 SyncEngine 处理)
        last_err: Exception | None = None
        for _ in range(3):
            try:
                messages = await self.bot.send_media_group(chat_id=self.chat_id, media=group)
                ids = [m.message_id for m in messages]
                logger.info("sent %d media to %s for %s", len(ids), self.chat_id, post.post_id)
                return ids
            except RetryAfter as e:
                logger.warning("TG rate limit, waiting %ds", e.retry_after + 1)
                await asyncio.sleep(e.retry_after + 1)
            except TelegramError as e:
                last_err = e
                raise
        raise TelegramError(f"media group failed after retries: {last_err}")

    async def close(self) -> None:
        await self.bot.shutdown()
