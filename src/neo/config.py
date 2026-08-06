from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-5.6-sol",
    "openrouter": "anthropic/claude-sonnet-5",
    "google": "gemini-3.5-flash",
}
PROVIDERS = frozenset(DEFAULT_MODELS)


@dataclass(slots=True)
class Config:
    provider: str = "anthropic"
    model: str = ""
    openai_auth: str = "api_key"
    subagents: dict[str, str] = field(default_factory=dict)
    tool_approvals: list[str] = field(default_factory=list)
    context_window_tokens: int = 200_000
    agents_file: bool = True
    skills: bool = True
    prompt_caching: bool = True
    verbose: bool = False
    phases: dict[str, dict[str, str]] = field(default_factory=dict)
    source: str = "embedded"

    def validate(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError(f"unknown provider {self.provider!r}")
        if self.openai_auth not in {"api_key", "subscription"}:
            raise ValueError("openai_auth must be 'api_key' or 'subscription'")
        if not self.model:
            self.model = "gpt-5-codex" if self.provider == "openai" and self.openai_auth == "subscription" else DEFAULT_MODELS[self.provider]
        if self.context_window_tokens <= 0:
            raise ValueError("compaction.context_window_tokens must be positive")
        cleaned: list[str] = []
        for item in self.tool_approvals:
            item = str(item).strip()
            if not item:
                raise ValueError("tool_approvals entries must not be empty")
            if item not in cleaned:
                cleaned.append(item)
        self.tool_approvals = cleaned


def _bool(section: dict[str, Any], name: str, default: bool) -> bool:
    value = section.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def parse_config(data: dict[str, Any] | None, source: str = "embedded") -> Config:
    data = data or {}
    if "permissions" in data:
        raise ValueError("permissions has been removed; use tool_approvals for optional confirmations")
    features = data.get("features") or {}
    output = data.get("output") or {}
    compaction = data.get("compaction") or {}
    cfg = Config(
        provider=str(data.get("provider") or "anthropic").strip().lower(),
        model=str(data.get("model") or "").strip(),
        openai_auth=str(data.get("openai_auth") or "api_key").strip().lower(),
        subagents=dict(data.get("subagents") or {}),
        tool_approvals=list(data.get("tool_approvals") or []),
        context_window_tokens=int(compaction.get("context_window_tokens", 200_000)),
        agents_file=_bool(features, "agents_file", True),
        skills=_bool(features, "skills", True),
        prompt_caching=_bool(features, "prompt_caching", True),
        verbose=_bool(output, "verbose", False),
        phases=dict(data.get("phases") or {}),
        source=source,
    )
    cfg.validate()
    return cfg


def load_config(cwd: Path | None = None, home: Path | None = None) -> Config:
    cwd = (cwd or Path.cwd()).resolve()
    home = home or Path.home()
    candidates = (cwd / "neo.yaml", home / ".neo" / "config.yaml")
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
