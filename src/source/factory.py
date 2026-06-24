"""Source 工厂:按 config.source_type 构造 Scweet / Nitter 源。

main.py(主循环)和 admin_gui.py(手动采集)共用,避免两份构造逻辑漂移。
缺 auth_token / 配置不全 → raise RuntimeError(调用方决定:main 退出进程,GUI 弹错)。
"""

from __future__ import annotations

import logging
import os

from src.config import AppConfig
from src.source.base import Source

logger = logging.getLogger(__name__)


def make_source(config: AppConfig) -> Source:
    """按 config.source_type 构造 Source。Scweet 缺 auth_token → raise RuntimeError。"""
    cache_dir = config.storage.cache_dir
    if config.source_type == "nitter":
        from src.source.nitter import NitterSource

        return NitterSource(nitter_instance=config.fetcher.nitter_instance, cache_dir=cache_dir)

    auth_token = config.scweet.auth_token or os.environ.get("SCWEET_AUTH_TOKEN", "")
    if not auth_token:
        raise RuntimeError(
            "Scweet auth_token 未配置。在 config.yaml 的 scweet.auth_token 里填,"
            "或设环境变量 SCWEET_AUTH_TOKEN。"
        )
    from src.source.scweet import ScweetSource

    return ScweetSource(
        auth_token=auth_token, proxy=config.scweet.proxy or None, cache_dir=cache_dir
    )
