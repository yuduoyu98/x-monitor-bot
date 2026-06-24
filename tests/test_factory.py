"""make_source 工厂:按 source_type 构造对应 Source;Scweet 缺 auth_token → raise。"""

from __future__ import annotations

import pytest

from src.config import AppConfig
from src.source.factory import make_source
from src.source.nitter import NitterSource


def _config(**overrides) -> AppConfig:
    base = {
        "telegram": {"bot_token": "x", "chat_id": "1"},
        "source_type": "scweet",
        "scweet": {"auth_token": "tok", "proxy": ""},
    }
    base.update(overrides)
    return AppConfig.model_validate(base)


def test_make_source_nitter():
    src = make_source(_config(source_type="nitter"))
    assert isinstance(src, NitterSource)


def test_make_source_scweet_with_token():
    from src.source.scweet import ScweetSource

    src = make_source(
        _config(
            source_type="scweet", scweet={"auth_token": "tok", "proxy": "http://127.0.0.1:7890"}
        )
    )
    assert isinstance(src, ScweetSource)


def test_make_source_scweet_missing_token_raises(monkeypatch):
    monkeypatch.delenv("SCWEET_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        make_source(_config(source_type="scweet", scweet={"auth_token": "", "proxy": ""}))
