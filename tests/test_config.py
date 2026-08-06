from pathlib import Path

import pytest

from neo.config import load_config, parse_config


def test_config_uses_first_hit_and_provider_default(tmp_path: Path) -> None:
    home = tmp_path / "home"; cwd = tmp_path / "repo"; home.mkdir(); cwd.mkdir()
    (home / ".neo").mkdir(); (home / ".neo" / "config.yaml").write_text("provider: google\n", encoding="utf-8")
    (cwd / "neo.yaml").write_text("provider: openai\n", encoding="utf-8")
    config = load_config(cwd, home)
    assert config.provider == "openai"
    assert config.model == "gpt-5.6-sol"
    assert config.source == str(cwd / "neo.yaml")


def test_removed_permissions_are_rejected() -> None:
    with pytest.raises(ValueError, match="permissions has been removed"):
        parse_config({"permissions": {"mode": "full"}})

