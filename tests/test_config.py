"""Tests for configuration loading and validation."""

import pytest
import yaml

from src.config import load_config


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
