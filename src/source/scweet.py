"""ScweetSource:用 Scweet 直连 X GraphQL 取推 + 下载媒体。

外部系统 → 按 CLAUDE.md 测试策略,**live 测试,不 mock**(不写死 JSON 形状)。
需要 auth_token(专用号 cookie)+ 代理(国内:x.com 与 twimg CDN 都被墙,
curl_cffi/httpx 都不继承系统代理,必须显式传)。
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

from src.source.base import (
    DiscoveredTweet,
    MediaFile,
    MediaRef,
    Post,
    filter_newer,
    media_cache_path,
)

logger = logging.getLogger(__name__)

_TS_FMT = "%a %b %d %H:%M:%S %z %Y"


def _detect_proxy() -> str | None:
    """代理:env > Windows 系统代理注册表。"""
    for var in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        import urllib.request

        return urllib.request.getproxies().get("https")
    except Exception:
        return None


def _best_video_url(media: dict) -> str | None:
    """从 video_info.variants 选最高码率的 mp4。"""
    variants = (media.get("video_info") or {}).get("variants") or []
    mp4s = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")]
    if not mp4s:
        return None
    return max(mp4s, key=lambda v: v.get("bitrate") or 0)["url"]


def _parse_media(legacy: dict) -> list[MediaRef]:
    """从 legacy.extended_entities.media 解析全部媒体(photo + video/gif)。"""
    out: list[MediaRef] = []
    for m in (legacy.get("extended_entities") or {}).get("media") or []:
        mtype = m.get("type")
        if mtype == "photo":
            url = m.get("media_url_https") or ""
            if url:
                if "pbs.twimg.com" in url and "?name=" not in url:
                    url += "?name=orig"  # 原图
                out.append(MediaRef(url=url, type="photo"))
        elif mtype in ("video", "animated_gif"):
            url = _best_video_url(m)
            if url:
                out.append(MediaRef(url=url, type=mtype))
    return out


def parse_tweet(raw_tweet: dict) -> DiscoveredTweet | None:
    """scweet 推文字典 → DiscoveredTweet。解析失败返回 None。"""
    post_id = str(raw_tweet.get("tweet_id") or "")
    if not post_id:
        return None
    ts_raw = raw_tweet.get("timestamp")
    try:
        timestamp = datetime.strptime(ts_raw, _TS_FMT) if ts_raw else datetime.now(UTC)
    except ValueError:
        timestamp = datetime.now(UTC)
    raw = raw_tweet.get("raw") or {}
    legacy = raw.get("legacy") or {}
    user = raw_tweet.get("user") or {}
    return DiscoveredTweet(
        post_id=post_id,
        timestamp=timestamp,
        text=raw_tweet.get("text") or "",
        is_retweet=(
            "retweeted_status_result" in legacy or "quoted_status_id_str" in legacy
        ),  # 转推或引用
        display_name=user.get("name") or "",
        media=_parse_media(legacy),
    )


class ScweetSource:
    """X GraphQL 媒体源(Scweet)。实现 src.source.base.Source 契约。"""

    def __init__(
        self,
        auth_token: str,
        proxy: str | None = None,
        cache_dir: Path | str = Path("./cache"),
        db_path: str = "scweet_state.db",
    ) -> None:
        self._auth_token = auth_token
        self._proxy = proxy or _detect_proxy()
        self._cache_dir = Path(cache_dir)
        self._db_path = db_path
        self._client = None  # Scweet 实例,懒构造(重依赖 + 首次 bootstrap 联网)
        self._http: httpx.AsyncClient | None = None  # 媒体下载用

    def _ensure_client(self):
        if self._client is None:
            from Scweet import Scweet  # 懒导入:nitter-only 用户不必装 scweet

            kwargs = {"auth_token": self._auth_token, "db_path": self._db_path}
            if self._proxy:
                kwargs["proxy"] = self._proxy
            self._client = Scweet(**kwargs)
        return self._client

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            kwargs = {
                "timeout": 60.0,
                "follow_redirects": True,
                "headers": {"Referer": "https://x.com/", "User-Agent": "x-monitor-bot/0.1"},
            }
            if self._proxy:
                kwargs["proxy"] = self._proxy
            self._http = httpx.AsyncClient(**kwargs)
        return self._http

    async def get_new_posts(
        self, account: str, watermark: datetime | None, *, limit: int = 20, max_limit: int = 500
    ) -> list[Post]:
        """取 watermark 之后的全部增量。

        Scweet 单次调用内按 cursor 翻页(高效,不重复)。但公开 API 拿不到 cursor、
        回调无法终止分页、resume 不往前翻(均经源码+实测确认),故"遇 watermark 即停"做不到。
        折中:先取 `limit` 探 gap,若最老仍 > watermark → 再取一次到 `max_limit`
        (内部高效翻页;低活跃账号发完即停,不过取)。连续轮询 1 次;回填 2 次(首页重取 1 次)。
        """
        client = self._ensure_client()
        raw_list = await client.aget_profile_tweets([account], limit=limit)
        if watermark is not None and raw_list:
            parsed = [p for p in (parse_tweet(t) for t in raw_list) if p is not None]
            oldest = min((p.timestamp for p in parsed), default=None)
            if oldest is not None and oldest > watermark:
                raw_list = await client.aget_profile_tweets([account], limit=max_limit)
        discovered = [d for d in (parse_tweet(t) for t in raw_list) if d is not None]
        new = filter_newer(discovered, watermark)
        posts = [await self._to_post(d, account) for d in new]
        return [p for p in posts if p is not None]

    async def _to_post(self, dt: DiscoveredTweet, account: str) -> Post | None:
        """下载全部媒体 → Post。任一媒体下载失败 → 返回 None(整条跳过,下轮重发现重试,
        避免半发 + 重试重复)。"""
        http = self._ensure_http()
        files: list[MediaFile] = []
        for i, ref in enumerate(dt.media, 1):
            path = media_cache_path(
                self._cache_dir, account, dt.display_name, dt.post_id, dt.timestamp, ref.type, i
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                try:
                    resp = await http.get(ref.url)
                    resp.raise_for_status()
                    path.write_bytes(resp.content)
                    logger.info("downloaded %s (%d bytes)", path.name, len(resp.content))
                except Exception:
                    logger.warning("media download failed, skip post %s: %s", dt.post_id, ref.url)
                    return None
            files.append(MediaFile(path=path, type=ref.type, url=ref.url))
        return Post(
            post_id=dt.post_id,
            username=account,
            timestamp=dt.timestamp,
            text=dt.text,
            media=files,
            is_retweet=dt.is_retweet,
            url=f"https://x.com/{account}/status/{dt.post_id}",
            display_name=dt.display_name,
        )

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._client is not None:
            aclose = getattr(self._client, "aclose", None)
            if aclose is not None:
                await aclose()
            self._client = None
