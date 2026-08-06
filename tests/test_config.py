from pathlib import Path

import pytest

from neo.config import load_config, load_dotenv, parse_config


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


def test_dotenv_loads_values_without_overriding_environment(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / ".env"
    path.write_text("NEW_VALUE='from file'\nEXISTING=from-file\n", encoding="utf-8")
    monkeypatch.delenv("NEW_VALUE", raising=False)
    monkeypatch.setenv("EXISTING", "exported")
    load_dotenv(path)
    assert __import__("os").environ["NEW_VALUE"] == "from file"
    assert __import__("os").environ["EXISTING"] == "exported"


def test_named_provider_profile_accepts_camel_case_api_base() -> None:
    config = parse_config({
        "provider": "enterprise",
        "providers": [{
            "id": "enterprise", "name": "Example Enterprise GPT", "provider": "openai",
            "model": "example.organization.language-model.gpt-5",
            "apiBase": "https://example.test/openai/v1/",
            "api_key_env": "ENTERPRISE_LLM_TOKEN",
        }],
    })
    assert config.backend() == "openai"
    assert config.model == "example.organization.language-model.gpt-5"
    assert config.active_profile().display_name == "Example Enterprise GPT"
    assert config.active_profile().api_base == "https://example.test/openai/v1"


def test_inline_api_key_takes_precedence_over_environment(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-key")
    config = parse_config({"provider": "openrouter", "api_key": "inline-key"})
    assert config.credential() == "inline-key"
