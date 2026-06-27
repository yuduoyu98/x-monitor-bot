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
    MediaRef,
    Post,
    filter_newer,
)
from src.source.download import download_post

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
    # Scweet 的 raw 是「未解包」的 tweet_result_raw(api_engine.py: raw=tweet_result_raw)。
    # TweetWithVisibilityResults(回复受限/可见性,如本例 @DeadShe_ 限定回复的推)的 legacy
    # 嵌在 raw["tweet"]["legacy"],需先解包(对齐 Scweet 自己取 legacy 的方式),
    # 否则媒体/转推判定全空 → media=0 被 media_only 误跳过(真实有图也漏采)。
    if isinstance(raw.get("tweet"), dict):
        raw = raw["tweet"]
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
        self._auth_checked = False

    def _ensure_client(self):
        if self._client is None:
            from Scweet import Scweet  # 懒导入:nitter-only 用户不必装 scweet

            kwargs = {"auth_token": self._auth_token, "db_path": self._db_path}
            if self._proxy:
                kwargs["proxy"] = self._proxy
            self._client = Scweet(**kwargs)
            # 禁用 X-Client-Transaction-Id 反爬头生成:x_client_transaction 的正则跟不上
            # X 首页改版,bootstrap 每次必崩(崩前已白打一次 GET https://x.com 首页 +
            # 刷 warning),且失败不缓存 → 每翻一页重试一次(500 回填 ≈ 25 次)。
            # 实测 X 对带 cookie 的 GraphQL 不强制此头(status 200 正常),禁用后功能
            # 等价(本来就没发成功),只是不再反复失败。要重新启用删此段即可。
            provider = getattr(self._client, "_transaction_id_provider", None)
            if provider is not None:
                provider.enabled = False
        return self._client

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            kwargs = {
                "timeout": 120.0,
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
        if not self._auth_checked:
            # 坏 token 时 Scweet 静默返回空(bootstrap 失败 → 账号 unusable → 无 eligible)。
            # 查 db 的 eligible 账号:没有 → 显式 raise,别把"认证挂了"当"没新推"。
            # 注意:持久 db 里上一次好 token 的 eligible 账号会掩盖本次坏 token —— 换 token/测试
            # 时应传独立 db_path 避免串。
            if not client.db.list_accounts(eligible_only=True):
                raise RuntimeError(
                    "Scweet auth failed — no eligible account; check SCWEET_AUTH_TOKEN / proxy"
                )
            self._auth_checked = True
            logger.info("[scweet] @%s auth ok", account)
        raw_list = await client.aget_profile_tweets([account], limit=limit)
        logger.info("[scweet] @%s 首次取 limit=%d → 返回 %d 条", account, limit, len(raw_list))
        if watermark is not None and raw_list:
            parsed = [p for p in (parse_tweet(t) for t in raw_list) if p is not None]
            oldest = min((p.timestamp for p in parsed), default=None)
            if oldest is not None and oldest > watermark:
                logger.info(
                    "[scweet] @%s gap 大(最老 %s > 水位线 %s)→ 扩取 limit=%d",
                    account,
                    oldest.isoformat(),
                    watermark.isoformat(),
                    max_limit,
                )
                expanded = await client.aget_profile_tweets([account], limit=max_limit)
                logger.info(
                    "[scweet] @%s 扩取 limit=%d → 返回 %d 条", account, max_limit, len(expanded)
                )
                if expanded:
                    raw_list = expanded
                else:
                    logger.warning(
                        "[scweet] @%s 扩取返回空(疑似限流),沿用首次取到的 %d 条",
                        account,
                        len(raw_list),
                    )
        discovered = [d for d in (parse_tweet(t) for t in raw_list) if d is not None]
        new = filter_newer(discovered, watermark)
        logger.info(
            "[scweet] @%s 原始 %d 条 → 解析 %d 条 → %d 在 watermark 之后",
            account,
            len(raw_list),
            len(discovered),
            len(new),
        )
        posts = [await self._to_post(d, account) for d in new]
        ok = [p for p in posts if p is not None]
        dl_failed = len(posts) - len(ok)
        logger.info("[scweet] @%s 下载完成: 成功 %d 条, 失败 %d 条", account, len(ok), dl_failed)
        return ok

    async def _to_post(self, dt: DiscoveredTweet, account: str) -> Post | None:
        """下载全部媒体 → Post(共享 download_post)。任一媒体失败 → None。"""
        return await download_post(dt, account, self._cache_dir, self._ensure_http())

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._client is not None:
            aclose = getattr(self._client, "aclose", None)
            if aclose is not None:
                await aclose()
            self._client = None
