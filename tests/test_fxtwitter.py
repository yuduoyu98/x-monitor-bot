"""fxTwitter 取推(oneshot 专用)测试。

parse_tweet_url / parse_fxtwitter_tweet 是纯函数 → 单测;fetch_tweet 是外部系统 → live。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from src.source.fxtwitter import FetchedTweet, parse_fxtwitter_tweet, parse_tweet_url


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://x.com/DeadShe_/status/2068697713781428482", "2068697713781428482"),
        ("https://twitter.com/foo/status/1234567890", "1234567890"),
        ("https://x.com/i/status/2068697713781428482", "2068697713781428482"),
        ("https://nitter.net/foo/status/999999999999999?s=20", "999999999999999"),
        ("2068697713781428482", "2068697713781428482"),  # 裸 ID
        ("  https://x.com/a/status/11111  ", "11111"),  # 前后空白
        ("https://x.com/DeadShe_/status/2068697713781428482?s=20&t=abc", "2068697713781428482"),
        ("https://x.com/DeadShe_", None),  # 无 status
        ("not a url at all", None),
        ("", None),
        ("https://x.com/foo/status/", None),  # status 后无 ID
    ],
)
def test_parse_tweet_url(url: str, expected: str | None) -> None:
    assert parse_tweet_url(url) == expected


# --- parse_fxtwitter_tweet:fxTwitter JSON → FetchedTweet(纯函数,fixture 取自真实响应) ---


def _fx_photo_response() -> dict:
    """取自真实 fxTwitter 响应(tweet 2068697713781428482, @DeadShe_)。"""
    return {
        "code": "Success",
        "tweet": {
            "id": "2068697713781428482",
            "text": "私人电报已更新",
            "created_at": "Sun Jun 21 14:09:04 +0000 2026",
            "author": {"screen_name": "DeadShe_", "name": "_SHE_YE_"},
            "media": {
                "photos": [
                    {
                        "type": "photo",
                        "url": "https://pbs.twimg.com/media/HLV9mvmbkAAppsz.jpg?name=orig",
                        "width": 679,
                        "height": 485,
                    }
                ],
                "videos": [],
                "gifs": [],
            },
        },
    }


def test_parse_fxtwitter_tweet_photo() -> None:
    ft = parse_fxtwitter_tweet(_fx_photo_response())
    assert isinstance(ft, FetchedTweet)
    assert ft.post_id == "2068697713781428482"
    assert ft.username == "DeadShe_"
    assert ft.display_name == "_SHE_YE_"
    assert ft.text == "私人电报已更新"
    assert ft.timestamp == datetime(2026, 6, 21, 14, 9, 4, tzinfo=UTC)
    assert len(ft.media) == 1
    assert ft.media[0].type == "photo"
    assert ft.media[0].url == "https://pbs.twimg.com/media/HLV9mvmbkAAppsz.jpg?name=orig"


def test_parse_fxtwitter_tweet_adds_name_orig_when_missing() -> None:
    """photo url 若没带 ?name= → 补 ?name=orig(与 Scweet/Nitter 一致,要原图)。"""
    data = _fx_photo_response()
    data["tweet"]["media"]["photos"][0]["url"] = "https://pbs.twimg.com/media/abc.jpg"
    ft = parse_fxtwitter_tweet(data)
    assert ft is not None
    assert ft.media[0].url == "https://pbs.twimg.com/media/abc.jpg?name=orig"


def test_parse_fxtwitter_tweet_video_and_gif() -> None:
    """video/gif 取 .url(FixTweet 文档形状)。"""
    data = _fx_photo_response()
    data["tweet"]["media"] = {
        "photos": [],
        "videos": [{"type": "video", "url": "https://video.twimg.com/x.mp4?tag=12"}],
        "gifs": [{"type": "animated_gif", "url": "https://video.twimg.com/y.mp4"}],
    }
    ft = parse_fxtwitter_tweet(data)
    assert ft is not None
    assert [m.type for m in ft.media] == ["video", "animated_gif"]
    assert ft.media[0].url.endswith("x.mp4?tag=12")


def test_parse_fxtwitter_tweet_no_tweet_returns_none() -> None:
    assert parse_fxtwitter_tweet({}) is None
    assert parse_fxtwitter_tweet({"code": "NotFound", "message": "x"}) is None
    assert parse_fxtwitter_tweet({"tweet": {}}) is None  # 有 tweet 但无 id


# --- fetch_tweet:外部系统 → live(opt-in,不 mock;反剧场) ---


@pytest.mark.skipif(not os.environ.get("FX_LIVE"), reason="opt-in: FX_LIVE=1(需代理)")
async def test_fetch_tweet_live_returns_photo() -> None:
    """fxTwitter 真取一条已知公开推(@DeadShe_ 2068697713781428482,含 1 图)。"""
    import httpx as _httpx

    from src.source.fxtwitter import fetch_tweet

    async with _httpx.AsyncClient(timeout=30) as http:
        ft = await fetch_tweet(http, "2068697713781428482")
    assert ft is not None
    assert ft.post_id == "2068697713781428482"
    assert ft.username == "DeadShe_"
    assert len(ft.media) >= 1
    assert ft.media[0].type == "photo"
