"""Tests for the Telegram sender."""

from datetime import UTC, datetime

from src.fetcher.base import MediaItem, Post
from src.telegram_bot import _build_caption

DT = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)


def _make_post(text: str = "Hello world!") -> Post:
    return Post(
        post_id="123",
        username="test_user",
        timestamp=DT,
        text=text,
        url="https://x.com/test_user/status/123",
        media=[MediaItem(url="https://pbs.twimg.com/x.jpg", type="photo")],
    )


class TestBuildCaption:
    def test_includes_author_and_link(self) -> None:
        post = _make_post("Check this out!")
        caption = _build_caption(post)
        assert "Check this out!" in caption
        assert "#test_user" in caption
        assert "https://x.com/test_user/status/123" in caption

    def test_truncates_long_text(self) -> None:
        long_text = "A" * 2000
        post = _make_post(long_text)
        caption = _build_caption(post)
        assert len(caption.encode("utf-8")) < 3000  # reasonable cap

    def test_escapes_html(self) -> None:
        post = _make_post("<script>alert('xss')</script>")
        caption = _build_caption(post)
        assert "<script>" not in caption
        assert "&lt;script&gt;" in caption
