from pathlib import Path

import pytest

from hal.config import load_config, load_dotenv, parse_config
from hal.harness import RunBudgets


def test_config_uses_first_hit_and_provider_default(tmp_path: Path) -> None:
    home = tmp_path / "home"; cwd = tmp_path / "repo"; home.mkdir(); cwd.mkdir()
    (home / ".hal").mkdir(); (home / ".hal" / "config.yaml").write_text("provider: google\n", encoding="utf-8")
    (cwd / "hal.yaml").write_text("provider: openai\n", encoding="utf-8")
    config = load_config(cwd, home)
    assert config.provider == "openai"
    assert config.model == "gpt-5.6-sol"
    assert config.source == str(cwd / "hal.yaml")

def test_removed_permissions_are_rejected() -> None:
    with pytest.raises(ValueError, match="permissions has been removed"):
        parse_config({"permissions": {"mode": "full"}})


def test_git_backend_defaults_to_auto_and_accepts_dulwich() -> None:
    assert parse_config({}).git_backend == "auto"
    assert parse_config({"git": {"backend": "dulwich"}}).git_backend == "dulwich"


def test_git_backend_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="git.backend"):
        parse_config({"git": {"backend": "portable-magic"}})
    with pytest.raises(ValueError, match="git must be a mapping"):
        parse_config({"git": "dulwich"})


def test_local_write_and_bash_policies_are_validated() -> None:
    config = parse_config({
        "only_write_locally": True,
        "bash_policy": "approve",
    })
    assert config.only_write_locally is True
    assert config.bash_policy == "approve"
    with pytest.raises(ValueError, match="bash_policy"):
        parse_config({"bash_policy": "guess"})
    with pytest.raises(ValueError, match="only_write_locally"):
        parse_config({"only_write_locally": "yes"})


def test_streaming_defaults_on_and_can_be_disabled() -> None:
    assert parse_config({}).streaming is True
    assert parse_config({"features": {"streaming": False}}).streaming is False


def test_harness_budgets_are_opt_in_and_parse_all_limits() -> None:
    assert parse_config({}).harness_budgets is None
    config = parse_config({
        "harness": {
            "budgets": {
                "provider_calls": 12,
                "tool_calls": 34,
                "elapsed_seconds": 56.5,
                "input_tokens": 7_000,
                "output_tokens": 2_000,
            },
        },
    })

    assert config.harness_budgets == RunBudgets(
        provider_calls=12,
        tool_calls=34,
        elapsed_seconds=56.5,
        input_tokens=7_000,
        output_tokens=2_000,
    )


def test_default_capability_is_optional_and_validated() -> None:
    assert parse_config({}).default_capability == ""
    assert parse_config({
        "harness": {"default_capability": " INSPECT "},
    }).default_capability == "inspect"

    with pytest.raises(ValueError, match="unknown capability"):
        parse_config({"harness": {"default_capability": "deploy"}})
    with pytest.raises(ValueError, match="must be a string"):
        parse_config({"harness": {"default_capability": 4}})


def test_harness_budget_defaults_and_null_limits() -> None:
    assert parse_config({"harness": {"budgets": {}}}).harness_budgets == RunBudgets()
    assert parse_config({
        "harness": {"budgets": {
            "provider_calls": None,
            "tool_calls": None,
            "elapsed_seconds": None,
        }},
    }).harness_budgets == RunBudgets(
        provider_calls=None,
        tool_calls=None,
        elapsed_seconds=None,
    )


@pytest.mark.parametrize("data,message", [
    ({"harness": "bounded"}, "harness must be a mapping"),
    ({"harness": {"budgets": 10}}, "harness.budgets must be a mapping"),
    ({"harness": {"mode": "strict"}}, "unknown harness setting"),
    ({"harness": {"budgets": {"requests": 10}}}, "unknown harness.budgets setting"),
    ({"harness": {"budgets": {"tool_calls": 0}}}, "tool_calls must be a positive"),
])
def test_harness_configuration_rejects_unsafe_or_unknown_values(data, message) -> None:
    with pytest.raises(ValueError, match=message):
        parse_config(data)


def test_extensions_are_opt_in_deduplicated_and_have_settings() -> None:
    config = parse_config({
        "extensions": ["jellyfin", "jellyfin"],
        "extension_config": {"jellyfin": {"url": "http://media.test"}},
    })
    assert config.extensions == ["jellyfin"]
    assert config.extension_config == {
        "jellyfin": {"url": "http://media.test"},
    }


def test_extensions_require_structured_configuration() -> None:
    with pytest.raises(ValueError, match="extensions must be a list"):
        parse_config({"extensions": "jellyfin"})
    with pytest.raises(ValueError, match="extension_config must be a mapping"):
        parse_config({"extension_config": []})
    with pytest.raises(ValueError, match=r"extension_config\.jellyfin must be a mapping"):
        parse_config({"extension_config": {"jellyfin": "bad"}})


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


def test_named_provider_profile_resolves_api_base_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("ENTERPRISE_LLM_BASE_URL", "https://private.example.test/v1/")
    config = parse_config({
        "provider": "enterprise",
        "providers": [{
            "id": "enterprise", "name": "Enterprise", "provider": "openai",
            "model": "internal-model", "apiBase": "https://fallback.example.test/v1",
            "apiBaseEnv": "ENTERPRISE_LLM_BASE_URL",
            "api_key_env": "ENTERPRISE_LLM_TOKEN",
        }],
    })

    assert config.active_profile().api_base_env == "ENTERPRISE_LLM_BASE_URL"
    assert config.api_base() == "https://private.example.test/v1"


def test_inline_api_key_takes_precedence_over_environment(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-key")
    config = parse_config({"provider": "openrouter", "api_key": "inline-key"})
    assert config.credential() == "inline-key"


def test_profile_accepts_max_completion_tokens_parameter() -> None:
    config = parse_config({
        "provider": "enterprise",
        "providers": [{
            "id": "enterprise", "name": "Enterprise", "provider": "openai",
            "model": "gpt-5.1", "apiBase": "https://example.test/v1",
            "api_key": "placeholder", "max_tokens_parameter": "max_completion_tokens",
        }],
    })
    assert config.active_profile().max_tokens_parameter == "max_completion_tokens"


def test_profile_rejects_unknown_max_tokens_parameter() -> None:
    with pytest.raises(ValueError, match="unsupported max_tokens_parameter"):
        parse_config({
            "provider": "enterprise",
            "providers": [{
                "id": "enterprise", "name": "Enterprise", "provider": "openai",
                "model": "model", "apiBase": "https://example.test/v1",
                "max_tokens_parameter": "token_budget",
            }],
        })
