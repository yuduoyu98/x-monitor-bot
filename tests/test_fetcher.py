"""Tests for fetcher data contract, tweet ID extraction, and fxTwitter parsing."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx

from src.fetcher.base import MediaItem, Post
from src.fetcher.nitter_fxtwitter import NitterFxTwitterFetcher, _extract_tweet_id


class TestMediaItem:
    def test_photo(self) -> None:
        item = MediaItem(url="https://pbs.twimg.com/media/x.jpg", type="photo")
        assert item.type == "photo"

    def test_video(self) -> None:
        item = MediaItem(url="https://video.twimg.com/x.mp4", type="video", bitrate=2176000)
        assert item.type == "video"
        assert item.bitrate == 2176000


class TestPost:
    def test_minimal_post(self) -> None:
        post = Post(
            post_id="123",
            username="test",
            timestamp=datetime.now(UTC),
            text="hello",
            url="https://x.com/test/status/123",
        )
        assert post.post_id == "123"
        assert post.media == []


class TestExtractTweetId:
    def test_pure_id(self) -> None:
        assert _extract_tweet_id("1850123456789012345") == "1850123456789012345"

    def test_twitter_url(self) -> None:
        assert _extract_tweet_id("https://twitter.com/user/status/12345") == "12345"

    def test_x_url(self) -> None:
        assert _extract_tweet_id("https://x.com/user/status/12345") == "12345"

    def test_non_match(self) -> None:
        assert _extract_tweet_id("not a tweet") is None


class _FakeResponse:
    def __init__(self, status: int, json_data: dict) -> None:
        self.status_code = status
        self._json = json_data

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]


class TestFxTwitterResolution:
    def _mock_client(self, status: int, json_data: dict) -> httpx.AsyncClient:
        mock_resp = _FakeResponse(status, json_data)
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=mock_resp)
        client.aclose = AsyncMock()
        return client

    async def test_resolve_photo(self) -> None:
        fetcher = NitterFxTwitterFetcher()
        client = self._mock_client(
            200,
            {
                "code": 200,
                "tweet": {
                    "id": "111",
                    "url": "https://x.com/t/status/111",
                    "text": "photo!",
                    "created_at": "Wed Oct 05 18:40:30 +0000 2022",
                    "media": {"photos": [{"url": "https://pbs.twimg.com/media/abc.jpg"}]},
                },
            },
        )
        post = await fetcher._resolve_via_fxtwitter(client, "111", "test")
        assert post is not None
        assert post.post_id == "111"
        assert len(post.media) == 1
        assert post.media[0].type == "photo"
        assert "?name=orig" in post.media[0].url

    async def test_resolve_video(self) -> None:
        fetcher = NitterFxTwitterFetcher()
        client = self._mock_client(
            200,
            {
                "code": 200,
                "tweet": {
                    "id": "222",
                    "url": "https://x.com/t/status/222",
                    "text": "video!",
                    "created_at": "Fri Jan 05 12:00:00 +0000 2024",
                    "media": {"videos": [{"url": "https://video.twimg.com/v.mp4"}]},
                },
            },
        )
        post = await fetcher._resolve_via_fxtwitter(client, "222", "test")
        assert post is not None
        assert post.media[0].type == "video"

    async def test_resolve_no_media(self) -> None:
        fetcher = NitterFxTwitterFetcher()
        client = self._mock_client(
            200,
            {
                "code": 200,
                "tweet": {
                    "id": "333",
                    "url": "https://x.com/t/status/333",
                    "text": "text only",
                    "created_at": "Mon Jan 08 00:00:00 +0000 2024",
                    "media": {},
                },
            },
        )
        post = await fetcher._resolve_via_fxtwitter(client, "333", "test")
        assert post is not None
        assert post.media == []

    async def test_api_error_returns_none(self) -> None:
        fetcher = NitterFxTwitterFetcher()
        client = self._mock_client(500, {"error": "internal"})
        post = await fetcher._resolve_via_fxtwitter(client, "999", "test")
        assert post is None
