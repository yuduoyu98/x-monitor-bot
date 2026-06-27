"""Tests for configuration loading and validation."""

import pytest
import yaml

from src.config import AppConfig, ScweetConfig, TelegramConfig, TokenEntry, load_config, save_config


class TestAppConfig:
    def test_minimal_yaml(self, tmp_path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({"telegram": {"bot_token": "test_token"}}),
            encoding="utf-8",
        )
        config = load_config(str(config_file))
        assert config.telegram.bot_token == "test_token"

    def test_defaults(self, tmp_path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({"telegram": {"bot_token": "test"}}),
            encoding="utf-8",
        )
        config = load_config(str(config_file))
        assert config.fetcher.type == "nitter_fxtwitter"
        assert config.storage.cache_ttl_days == 7
        assert config.storage.db_path == "./state.db"

    def test_ignores_old_subscriptions(self, tmp_path) -> None:
        """Old config.yaml with subscriptions field should not break."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "telegram": {"bot_token": "test"},
                    "subscriptions": [{"account_id": "old_user"}],
                }
            ),
            encoding="utf-8",
        )
        config = load_config(str(config_file))
        assert config.telegram.bot_token == "test"

    def test_missing_config_file(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_config("nonexistent.yaml")

    def test_token_history_defaults_empty(self, tmp_path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"telegram": {"bot_token": "t"}}), encoding="utf-8")
        assert load_config(str(config_file)).scweet.token_history == []

    def test_token_history_round_trip(self, tmp_path) -> None:
        """保存的 token 历史(含备注)能完整读回。"""
        cfg = AppConfig(
            telegram=TelegramConfig(bot_token="t"),
            scweet=ScweetConfig(
                auth_token="current123",
                token_history=[
                    TokenEntry(label="主号", token="current123"),
                    TokenEntry(label="备用", token="old456"),
                ],
            ),
        )
        p = tmp_path / "config.yaml"
        save_config(cfg, str(p))

        loaded = load_config(str(p))
        assert loaded.scweet.auth_token == "current123"
        assert len(loaded.scweet.token_history) == 2
        assert loaded.scweet.token_history[0].label == "主号"
        assert loaded.scweet.token_history[0].token == "current123"
        assert loaded.scweet.token_history[1].token == "old456"
