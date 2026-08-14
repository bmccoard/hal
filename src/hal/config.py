from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .harness import RunBudgets, resolve_capability


DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-5.6-sol",
    "openrouter": "anthropic/claude-sonnet-5",
    "google": "gemini-3.5-flash",
}
PROVIDERS = frozenset(DEFAULT_MODELS)
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


@dataclass(slots=True)
class ProviderProfile:
    name: str
    display_name: str
    provider: str
    model: str
    api_base: str = ""
    api_base_env: str = ""
    api_key: str = ""
    api_key_env: str = ""
    protocol: str = "chat_completions"
    max_tokens_parameter: str = "max_tokens"


@dataclass(slots=True)
class Config:
    provider: str = "anthropic"
    model: str = ""
    openai_auth: str = "api_key"
    api_key: str = ""
    git_backend: str = "auto"
    subagents: dict[str, str] = field(default_factory=dict)
    tool_approvals: list[str] = field(default_factory=list)
    only_write_locally: bool = False
    bash_policy: str = "normal"
    extensions: list[str] = field(default_factory=list)
    extension_config: dict[str, dict[str, Any]] = field(default_factory=dict)
    context_window_tokens: int = 200_000
    agents_file: bool = True
    skills: bool = True
    prompt_caching: bool = True
    streaming: bool = True
    verbose: bool = False
    phases: dict[str, dict[str, str]] = field(default_factory=dict)
    providers: dict[str, ProviderProfile] = field(default_factory=dict)
    harness_budgets: RunBudgets | None = None
    default_capability: str = ""
    source: str = "embedded"

    def validate(self) -> None:
        if self.provider not in PROVIDERS and self.provider not in self.providers:
            raise ValueError(f"unknown provider {self.provider!r}")
        profile = self.providers.get(self.provider)
        backend = profile.provider if profile else self.provider
        if backend not in PROVIDERS:
            raise ValueError(f"provider profile {self.provider!r} has unknown backend {backend!r}")
        if profile and profile.protocol not in {"chat_completions", "responses"}:
            raise ValueError(f"provider profile {self.provider!r} has unsupported protocol {profile.protocol!r}")
        if profile and profile.max_tokens_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError(
                f"provider profile {self.provider!r} has unsupported "
                f"max_tokens_parameter {profile.max_tokens_parameter!r}"
            )
        if profile:
            for field_name, value in (
                ("api_base_env", profile.api_base_env),
                ("api_key_env", profile.api_key_env),
            ):
                if value and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                    raise ValueError(f"provider profile {profile.name!r} has invalid {field_name}")
        if self.openai_auth not in {"api_key", "subscription"}:
            raise ValueError("openai_auth must be 'api_key' or 'subscription'")
        if self.git_backend not in {"auto", "native", "dulwich"}:
            raise ValueError("git.backend must be 'auto', 'native', or 'dulwich'")
        if self.bash_policy not in {"normal", "approve", "deny"}:
            raise ValueError("bash_policy must be 'normal', 'approve', or 'deny'")
        if not self.model:
            if profile and profile.model:
                self.model = profile.model
            elif backend == "openai" and self.openai_auth == "subscription":
                self.model = "gpt-5-codex"
            elif backend == "openrouter":
                self.model = os.environ.get("OPENROUTER_MODEL", "").strip() or DEFAULT_MODELS[backend]
            else:
                self.model = DEFAULT_MODELS[backend]
        if self.context_window_tokens <= 0:
            raise ValueError("compaction.context_window_tokens must be positive")
        if self.harness_budgets is not None and not isinstance(
            self.harness_budgets, RunBudgets
        ):
            raise ValueError("harness.budgets must be a mapping or null")
        if self.default_capability:
            resolve_capability(self.default_capability)
        cleaned: list[str] = []
        for item in self.tool_approvals:
            item = str(item).strip()
            if not item:
                raise ValueError("tool_approvals entries must not be empty")
            if item not in cleaned:
                cleaned.append(item)
        self.tool_approvals = cleaned
        enabled: list[str] = []
        for item in self.extensions:
            name = str(item).strip()
            if not name:
                raise ValueError("extensions entries must not be empty")
            if name not in enabled:
                enabled.append(name)
        self.extensions = enabled
        for name, settings in self.extension_config.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("extension_config keys must be non-empty strings")
            if not isinstance(settings, dict):
                raise ValueError(f"extension_config.{name} must be a mapping")

    def active_profile(self) -> ProviderProfile | None:
        return self.providers.get(self.provider)

    def backend(self) -> str:
        profile = self.active_profile()
        return profile.provider if profile else self.provider

    def credential_env(self) -> str:
        profile = self.active_profile()
        if profile and profile.api_key_env:
            return profile.api_key_env
        return {
            "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY", "google": "GOOGLE_API_KEY",
        }[self.backend()]

    def credential(self) -> str:
        profile = self.active_profile()
        if profile and profile.api_key:
            return profile.api_key
        if self.api_key:
            return self.api_key
        return os.environ.get(self.credential_env(), "").strip()

    def api_base(self) -> str:
        profile = self.active_profile()
        if profile is None:
            return ""
        if profile.api_base_env:
            value = os.environ.get(profile.api_base_env, "").strip()
            if value:
                return value.rstrip("/")
        return profile.api_base.rstrip("/")


def _bool(section: dict[str, Any], name: str, default: bool) -> bool:
    value = section.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _harness(data: dict[str, Any]) -> tuple[RunBudgets | None, str]:
    if "harness" not in data or data["harness"] is None:
        return None, ""
    harness = data["harness"]
    if not isinstance(harness, dict):
        raise ValueError("harness must be a mapping")
    unknown_harness = set(harness) - {"budgets", "default_capability"}
    if unknown_harness:
        names = ", ".join(sorted(str(name) for name in unknown_harness))
        raise ValueError(f"unknown harness setting(s): {names}")
    raw_name = harness.get("default_capability", "")
    if raw_name is None:
        name = ""
    elif not isinstance(raw_name, str):
        raise ValueError("harness.default_capability must be a string or null")
    else:
        name = raw_name.strip().lower()
    if name:
        resolve_capability(name)
    parsed_budgets = None
    if "budgets" in harness and harness["budgets"] is not None:
        budgets = harness["budgets"]
        if not isinstance(budgets, dict):
            raise ValueError("harness.budgets must be a mapping or null")
        fields = {
            "provider_calls", "tool_calls", "elapsed_seconds",
            "input_tokens", "output_tokens",
        }
        unknown_budgets = set(budgets) - fields
        if unknown_budgets:
            names = ", ".join(sorted(str(item) for item in unknown_budgets))
            raise ValueError(f"unknown harness.budgets setting(s): {names}")
        try:
            parsed_budgets = RunBudgets(**budgets)
        except ValueError as exc:
            raise ValueError(f"harness.budgets: {exc}") from exc
    return parsed_budgets, name


def parse_config(data: dict[str, Any] | None, source: str = "embedded") -> Config:
    data = data or {}
    if "permissions" in data:
        raise ValueError("permissions has been removed; use tool_approvals for optional confirmations")
    features = data.get("features") or {}
    output = data.get("output") or {}
    compaction = data.get("compaction") or {}
    git = data.get("git") or {}
    harness_budgets, default_capability = _harness(data)
    if not isinstance(git, dict):
        raise ValueError("git must be a mapping")
    extensions = data.get("extensions", [])
    if extensions is None:
        extensions = []
    if not isinstance(extensions, list):
        raise ValueError("extensions must be a list")
    extension_config = data.get("extension_config", {})
    if extension_config is None:
        extension_config = {}
    if not isinstance(extension_config, dict):
        raise ValueError("extension_config must be a mapping")
    profiles: dict[str, ProviderProfile] = {}
    raw_profiles = data.get("providers") or []
    if isinstance(raw_profiles, dict):
        raw_profiles = [{"name": name, **(value or {})} for name, value in raw_profiles.items()]
    if not isinstance(raw_profiles, list):
        raise ValueError("providers must be a list or mapping")
    for raw in raw_profiles:
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            raise ValueError("each provider profile needs a name")
        display_name = str(raw["name"]).strip()
        name = str(raw.get("id") or re.sub(r"[^a-z0-9_-]+", "-", display_name.lower()).strip("-")).strip().lower()
        if not name:
            raise ValueError("each provider profile needs a usable id or name")
        if name in PROVIDERS:
            raise ValueError(f"provider profile name {name!r} is reserved")
        profiles[name] = ProviderProfile(
            name=name,
            display_name=display_name,
            provider=str(raw.get("provider") or "openai").strip().lower(),
            model=str(raw.get("model") or "").strip(),
            api_base=str(raw.get("api_base") or raw.get("apiBase") or "").strip().rstrip("/"),
            api_base_env=str(raw.get("api_base_env") or raw.get("apiBaseEnv") or "").strip(),
            api_key=str(raw.get("api_key") or raw.get("apiKey") or "").strip(),
            api_key_env=str(raw.get("api_key_env") or raw.get("apiKeyEnv") or "").strip(),
            protocol=str(raw.get("protocol") or "chat_completions").strip().lower(),
            max_tokens_parameter=str(
                raw.get("max_tokens_parameter") or raw.get("maxTokensParameter") or "max_tokens"
            ).strip(),
        )
    cfg = Config(
        provider=str(data.get("provider") or "anthropic").strip().lower(),
        model=str(data.get("model") or "").strip(),
        openai_auth=str(data.get("openai_auth") or "api_key").strip().lower(),
        api_key=str(data.get("api_key") or data.get("apiKey") or "").strip(),
        git_backend=str(git.get("backend") or "auto").strip().lower(),
        subagents=dict(data.get("subagents") or {}),
        tool_approvals=list(data.get("tool_approvals") or []),
        only_write_locally=_bool(data, "only_write_locally", False),
        bash_policy=str(data.get("bash_policy") or "normal").strip().lower(),
        extensions=list(extensions),
        extension_config=dict(extension_config),
        context_window_tokens=int(compaction.get("context_window_tokens", 200_000)),
        agents_file=_bool(features, "agents_file", True),
        skills=_bool(features, "skills", True),
        prompt_caching=_bool(features, "prompt_caching", True),
        streaming=_bool(features, "streaming", True),
        verbose=_bool(output, "verbose", False),
        phases=dict(data.get("phases") or {}),
        providers=profiles,
        harness_budgets=harness_budgets,
        default_capability=default_capability,
        source=source,
    )
    cfg.validate()
    return cfg


def load_dotenv(path: Path) -> None:
    """Load a simple dotenv file without replacing exported environment values."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"load {path}: {exc}") from exc
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            raise ValueError(f"load {path}: invalid assignment on line {number}")
        name, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


def load_config(cwd: Path | None = None, home: Path | None = None) -> Config:
    cwd = (cwd or Path.cwd()).resolve()
    home = home or Path.home()
    load_dotenv(cwd / ".env")
    candidates = (
        cwd / "hal.yaml", home / ".hal" / "config.yaml",
    )
    for path in candidates:
        if path.is_file():
            try:
                value = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                raise ValueError(f"load {path}: {exc}") from exc
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"load {path}: top-level YAML value must be a mapping")
            return parse_config(value, str(path))
    return parse_config({}, "embedded")
