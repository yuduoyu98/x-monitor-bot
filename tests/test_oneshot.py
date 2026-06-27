"""oneshot 管道测试:契约级,免网络(mock fetch_tweet + fake sink + httpx MockTransport)。"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from src.oneshot import _download_media, send_tweet_by_url
from src.source.base import MediaRef
from src.source.fxtwitter import FetchedTweet


class _FakeSink:
    def __init__(self, ids=None, post_exc=None):
        self.posts = []
        self._ids = ids if ids is not None else [42]
        self._post_exc = post_exc

    async def post(self, post):
        self.posts.append(post)
        if self._post_exc:
            raise self._post_exc
        return self._ids


def _text_only() -> FetchedTweet:
    return FetchedTweet(
        post_id="123",
        username="someone",
        display_name="Some One",
        timestamp=datetime(2026, 6, 21, 14, 9, 4, tzinfo=UTC),
        text="hello world",
        media=[],
    )


# --- send_tweet_by_url:编排 ---


async def test_send_text_only_tweet(monkeypatch, tmp_path) -> None:
    sink = _FakeSink(ids=[7, 8])
    monkeypatch.setattr("src.oneshot.fetch_tweet", lambda http, tid: _async_return(_text_only()))
    d = tmp_path / "oneshot_tmp"
    d.mkdir()
    monkeypatch.setattr("tempfile.mkdtemp", lambda **kw: str(d))

    res = await send_tweet_by_url("https://x.com/someone/status/123", sink)

    assert res.ok is True
    assert res.message_ids == [7, 8]
    assert len(sink.posts) == 1
    assert sink.posts[0].text == "hello world"
    assert sink.posts[0].media == []
    assert sink.posts[0].username == "someone"
    assert sink.posts[0].url == "https://x.com/someone/status/123"
    assert not d.exists()  # 临时目录已清


async def test_send_invalid_url(monkeypatch) -> None:
    sink = _FakeSink()
    res = await send_tweet_by_url("not a url", sink)
    assert res.ok is False
    assert "无法识别" in res.message
    assert sink.posts == []  # 没动 sink


async def test_send_fetch_returns_none(monkeypatch) -> None:
    sink = _FakeSink()
    monkeypatch.setattr("src.oneshot.fetch_tweet", lambda http, tid: _async_return(None))
    res = await send_tweet_by_url("https://x.com/u/status/999", sink)
    assert res.ok is False
    assert "取推失败" in res.message
    assert sink.posts == []


async def test_send_empty_content(monkeypatch, tmp_path) -> None:
    sink = _FakeSink()
    empty = FetchedTweet(
        post_id="1", username="u", display_name="", timestamp=datetime.now(UTC), text="", media=[]
    )
    monkeypatch.setattr("src.oneshot.fetch_tweet", lambda http, tid: _async_return(empty))
    monkeypatch.setattr("tempfile.mkdtemp", lambda **kw: str(tmp_path / "t"))
    res = await send_tweet_by_url("1", sink)
    assert res.ok is False
    assert "无内容" in res.message


async def test_send_sink_failure(monkeypatch, tmp_path) -> None:
    sink = _FakeSink(post_exc=RuntimeError("boom"))
    monkeypatch.setattr("src.oneshot.fetch_tweet", lambda http, tid: _async_return(_text_only()))
    d = tmp_path / "oneshot_tmp"
    d.mkdir()
    monkeypatch.setattr("tempfile.mkdtemp", lambda **kw: str(d))

    res = await send_tweet_by_url("https://x.com/someone/status/123", sink)

    assert res.ok is False
    assert "发送失败" in res.message
    assert not d.exists()  # 即便失败也清理


# --- _download_media:用 httpx.MockTransport 免网络 ---


def _ok_handler(request):
    return httpx.Response(200, content=b"\x00\x01\x02\x03")


async def test_download_media_writes_files(tmp_path) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler))
    media = [
        MediaRef(url="https://x.com/a.jpg", type="photo"),
        MediaRef(url="https://x.com/b.mp4", type="video"),
    ]
    try:
        files = await _download_media(media, tmp_path, http)
    finally:
        await http.aclose()
    assert files is not None
    assert len(files) == 2
    assert files[0].path.suffix == ".jpg"
    assert files[1].path.suffix == ".mp4"
    assert files[0].path.exists()
    assert files[0].path.stat().st_size == 4


async def test_download_media_failure_returns_none(tmp_path) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    try:
        files = await _download_media(
            [MediaRef(url="https://x.com/a.jpg", type="photo")], tmp_path, http
        )
    finally:
        await http.aclose()
    assert files is None


# --- helper ---


async def _async_return(value):
    """把同步值包成 awaitable(供 monkeypatch 替换 fetch_tweet 用)。"""
    return value
