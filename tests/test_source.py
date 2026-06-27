"""SP1 Source 层测试:水位线过滤、缓存路径、契约类型。

测试策略(CLAUDE.md):契约内纯逻辑用单测;外部系统(Scweet/Nitter)用 live verify。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.source.base import (
    DiscoveredTweet,
    MediaFile,
    Post,
    Source,
    filter_newer,
    media_cache_path,
)
from src.source.scweet import parse_tweet


def _tweet(post_id: str, iso_ts: str) -> DiscoveredTweet:
    """构造一个最小 DiscoveredTweet(只关心 id + 时间)。"""
    return DiscoveredTweet(post_id=post_id, timestamp=datetime.fromisoformat(iso_ts))


def test_filter_newer_keeps_only_strictly_after_watermark():
    """watermark 是"已处理到的点"——等于它的已处理,只留严格之后的。"""
    items = [
        _tweet("old", "2026-06-01T00:00:00+00:00"),
        _tweet("equal", "2026-06-10T00:00:00+00:00"),  # == watermark → 已处理,排除
        _tweet("new", "2026-06-20T00:00:00+00:00"),
    ]
    watermark = datetime(2026, 6, 10, tzinfo=UTC)

    result = filter_newer(items, watermark)

    assert [t.post_id for t in result] == ["new"]


def test_filter_newer_returns_newest_first():
    """契约要求 get_new_posts 返回 newest-first;过滤后也要保持倒序。"""
    items = [
        _tweet("a", "2026-06-01T00:00:00+00:00"),
        _tweet("b", "2026-06-20T00:00:00+00:00"),
        _tweet("c", "2026-06-10T00:00:00+00:00"),
    ]
    watermark = datetime(2026, 5, 1, tzinfo=UTC)  # 都比 watermark 新

    result = filter_newer(items, watermark)

    assert [t.post_id for t in result] == ["b", "c", "a"]


def test_filter_newer_none_watermark_returns_all_newest_first():
    """watermark=None 表示首次/无游标 → 返回全部,仍倒序。"""
    items = [
        _tweet("a", "2026-06-01T00:00:00+00:00"),
        _tweet("b", "2026-06-20T00:00:00+00:00"),
    ]

    result = filter_newer(items, None)

    assert [t.post_id for t in result] == ["b", "a"]


# --- media_cache_path:确定性缓存路径(重试时媒体已存在则跳过下载) ---


def test_media_cache_path_photo_format():
    """确定性:固定输入 → 固定路径;photo → .jpg;index 进文件名。"""
    ts = datetime(2026, 6, 20, 11, 49, 24, tzinfo=UTC)

    p = media_cache_path(
        cache_dir=Path("/cache"),
        username="chipsinblack",
        display_name="hihichips",
        post_id="2068300180542345490",
        timestamp=ts,
        media_type="photo",
        index=1,
    )

    assert p == Path(
        "/cache/chipsinblack/"
        "twitter_hihichips(@chipsinblack)_20260620-114924_2068300180542345490_photo_1.jpg"
    )


def test_media_cache_path_video_and_gif_are_mp4():
    ts = datetime(2026, 6, 20, 11, 49, 24, tzinfo=UTC)

    video = media_cache_path(Path("/c"), "u", "n", "123", ts, "video", 1)
    gif = media_cache_path(Path("/c"), "u", "n", "123", ts, "animated_gif", 1)

    assert video.suffix == ".mp4"
    assert gif.suffix == ".mp4"


def test_media_cache_path_distinct_index_distinct_file():
    """同一条推多个媒体,index 不同 → 文件名不同(不互相覆盖)。"""
    ts = datetime(2026, 6, 20, 11, 49, 24, tzinfo=UTC)

    first = media_cache_path(Path("/c"), "u", "n", "123", ts, "photo", 1)
    second = media_cache_path(Path("/c"), "u", "n", "123", ts, "photo", 2)

    assert first != second


# --- 契约类型:Post(已下载媒体)/ MediaFile / Source Protocol ---


def test_post_holds_downloaded_media_files():
    """Post.media 是已下载到本地的 MediaFile(调用方不再下载)。"""
    p = Post(
        post_id="1",
        username="chipsinblack",
        timestamp=datetime(2026, 6, 20, tzinfo=UTC),
        text="hi",
        media=[MediaFile(path=Path("/c/1.jpg"), type="photo", url="http://x/1.jpg")],
    )

    assert p.media[0].path == Path("/c/1.jpg")
    assert p.media[0].type == "photo"


def test_source_protocol_is_structural():
    """任何实现 get_new_posts + close 的对象都满足 Source 契约(鸭子类型)。"""

    class FakeSource:
        async def get_new_posts(self, account, watermark, *, limit=20):
            return []

        async def close(self): ...

    assert isinstance(FakeSource(), Source)


# --- parse_tweet:Scweet raw → DiscoveredTweet(纯函数,单测) ---
# Scweet 把 raw 设成「未解包」的 tweet_result_raw(api_engine.py:2543);
# TweetWithVisibilityResults(回复受限/可见性)的 legacy 嵌在 raw["tweet"]["legacy"],
# parse_tweet 必须先解包,否则媒体/转推判定全空 → media=0 被 media_only 误跳过。


def _photo_media(url: str = "https://pbs.twimg.com/media/abc.jpg?name=orig") -> dict:
    return {"type": "photo", "media_url_https": url}


def test_parse_tweet_detects_photo_in_visibility_wrapped_tweet():
    """TweetWithVisibilityResults:legacy 在 raw.tweet.legacy → 仍要解出媒体(本次 bug 回归)。"""
    raw_tweet = {
        "tweet_id": "2068697713781428482",
        "timestamp": "Sun Jun 21 06:09:00 +0000 2026",
        "text": "私人电报已更新",
        "user": {"name": "DeadShe"},
        "raw": {  # = Scweet 的 tweet_result_raw(未解包)
            "__typename": "TweetWithVisibilityResults",
            "rest_id": "2068697713781428482",
            "tweet": {
                "legacy": {"extended_entities": {"media": [_photo_media()]}},
            },
        },
    }
    dt = parse_tweet(raw_tweet)
    assert dt is not None
    assert dt.is_retweet is False
    assert len(dt.media) == 1
    assert dt.media[0].type == "photo"
    assert dt.media[0].url.startswith("https://pbs.twimg.com/media/abc.jpg")


def test_parse_tweet_detects_photo_in_normal_tweet():
    """普通 Tweet:legacy 在 raw.legacy → 解出媒体(解包逻辑不能破坏正常路径)。"""
    raw_tweet = {
        "tweet_id": "1",
        "timestamp": "Sun Jun 21 06:09:00 +0000 2026",
        "text": "hi",
        "user": {},
        "raw": {
            "__typename": "Tweet",
            "legacy": {
                "extended_entities": {
                    "media": [_photo_media("https://pbs.twimg.com/media/zzz.jpg")]
                }
            },
        },
    }
    dt = parse_tweet(raw_tweet)
    assert dt is not None
    assert len(dt.media) == 1
    assert dt.media[0].url.startswith("https://pbs.twimg.com/media/zzz.jpg")


# --- ScweetSource 活体测试(外部系统:按策略 live,不 mock) ---


@pytest.mark.skipif(
    not os.environ.get("SCWEET_AUTH_TOKEN"), reason="needs SCWEET_AUTH_TOKEN + proxy"
)
async def test_scweet_source_live_returns_posts_with_downloaded_media(tmp_path):
    """ScweetSource 真取推、真下媒体到 cache:返回 newest-first 的 Post,媒体文件落盘且非空。"""
    from src.source.scweet import ScweetSource

    src = ScweetSource(auth_token=os.environ["SCWEET_AUTH_TOKEN"], cache_dir=tmp_path)
    try:
        posts = await src.get_new_posts("chipsinblack", watermark=None, limit=4)
    finally:
        await src.close()

    assert len(posts) > 0
    # newest-first
    timestamps = [p.timestamp for p in posts]
    assert timestamps == sorted(timestamps, reverse=True)
    # 至少一条带媒体;媒体文件已下到磁盘、非空
    media_posts = [p for p in posts if p.media]
    assert media_posts, "no post had media"
    for f in (f for p in media_posts for f in p.media):
        assert f.path.exists(), f"{f.path} not downloaded"
        assert f.path.stat().st_size > 0


@pytest.mark.skipif(not os.environ.get("NITTER_LIVE"), reason="opt-in: NITTER_LIVE=1")
async def test_nitter_source_live(tmp_path):
    """NitterSource 活体:Nitter RSS + fxTwitter。实例可能挂 → 不强断言非空,跑通即可。"""
    from src.source.nitter import NitterSource

    src = NitterSource(cache_dir=tmp_path)
    try:
        posts = await src.get_new_posts("chipsinblack", watermark=None, limit=5)
    finally:
        await src.close()

    print(f"\n[nitter] {len(posts)} posts(实例状态见 warning 日志)")
    if posts:
        timestamps = [p.timestamp for p in posts]
        assert timestamps == sorted(timestamps, reverse=True)
        for p in posts:
            for f in p.media:
                assert f.path.exists()
