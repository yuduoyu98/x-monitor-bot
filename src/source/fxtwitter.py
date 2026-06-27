"""fxTwitter 取推(oneshot 专用):parse_tweet_url + fetch_tweet。

oneshot 管道用:公开 API、免认证、免代理(国内未墙)。不依赖项目的 source 契约
(不用 DiscoveredTweet/download_post),fetch_tweet 返回自己的 FetchedTweet。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from src.source.base import MediaRef

logger = logging.getLogger(__name__)

_TS_FMT = "%a %b %d %H:%M:%S %z %Y"  # fxTwitter created_at 与 X 一致
_FXTWITTER_API = "https://api.fxtwitter.com"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/127.0.0.0 Safari/537.36"


@dataclass
class FetchedTweet:
    """fxTwitter 取回的单条推(未下载媒体)。oneshot 专用,不走 DiscoveredTweet。"""

    post_id: str
    username: str
    display_name: str
    timestamp: datetime
    text: str
    media: list[MediaRef] = field(default_factory=list)


def parse_tweet_url(url: str) -> str | None:
    """从推文链接或裸 ID 抽出 tweet_id;无法识别 → None。

    接受 x.com/{u}/status/{id}、twitter.com/…、x.com/i/status/{id}、裸数字 ID、
    nitter 实例 …/{u}/status/{id};容忍前后空白与查询串。
    """
    s = url.strip()
    if not s:
        return None
    m = re.search(r"/status/(\d+)", s)
    if m:
        return m.group(1)
    if s.isdigit():  # 裸 ID
        return s
    return None


def parse_fxtwitter_tweet(data: dict) -> FetchedTweet | None:
    """fxTwitter 响应 JSON → FetchedTweet;无 tweet / 无 id → None。"""
    tw = data.get("tweet") if isinstance(data, dict) else None
    if not isinstance(tw, dict) or not tw:
        return None
    post_id = str(tw.get("id") or "").strip()
    if not post_id:
        return None
    author = tw.get("author") if isinstance(tw.get("author"), dict) else {}
    media_block = tw.get("media") if isinstance(tw.get("media"), dict) else {}
    return FetchedTweet(
        post_id=post_id,
        username=(author.get("screen_name") or "").strip(),
        display_name=author.get("name") or "",
        timestamp=_parse_created_at(tw.get("created_at"), tw.get("created_timestamp")),
        text=tw.get("text") or "",
        media=_parse_media(media_block),
    )


def _parse_media(media: dict) -> list[MediaRef]:
    """fxTwitter media.{photos,videos,gifs}[].url → MediaRef(photo 补 ?name=orig)。"""
    out: list[MediaRef] = []

    def _url(item) -> str:
        return (item.get("url") or "").strip() if isinstance(item, dict) else ""

    for p in media.get("photos") or []:
        url = _url(p)
        if url:
            if "pbs.twimg.com" in url and "?name=" not in url:
                url += "?name=orig"
            out.append(MediaRef(url=url, type="photo"))
    for v in media.get("videos") or []:
        url = _url(v)
        if url:
            out.append(MediaRef(url=url, type="video"))
    for g in media.get("gifs") or []:
        url = _url(g)
        if url:
            out.append(MediaRef(url=url, type="animated_gif"))
    return out


def _parse_created_at(created_at, created_timestamp) -> datetime:
    """优先 X 格式 created_at;失败退 created_timestamp(unix);都无 → now(UTC)。"""
    if created_at:
        try:
            return datetime.strptime(str(created_at), _TS_FMT)
        except ValueError:
            pass
    if created_timestamp:
        try:
            return datetime.fromtimestamp(int(created_timestamp), tz=UTC)
        except (TypeError, ValueError, OSError):
            pass
    return datetime.now(UTC)


async def fetch_tweet(http: httpx.AsyncClient, tweet_id: str) -> FetchedTweet | None:
    """GET fxTwitter /status/{id} → FetchedTweet;取不到/异常 → None。"""
    try:
        resp = await http.get(f"{_FXTWITTER_API}/status/{tweet_id}", headers={"User-Agent": _UA})
        if resp.status_code != 200:
            logger.warning("fxTwitter %s status=%s", tweet_id, resp.status_code)
            return None
        return parse_fxtwitter_tweet(resp.json())
    except Exception as exc:
        logger.warning("fxTwitter fetch %s failed: %s", tweet_id, exc)
        return None
