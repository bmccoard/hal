from __future__ import annotations

from pathlib import Path

import pytest

from hal.extensions import ExtensionContext, load_extensions
from hal.config import Config
from hal.models import ToolSpec
from hal.tools import Registry, Tool


class ExtensionTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("example_search", "Search an example service.", {"type": "object"})

    def run(self, arguments, cancellation=None) -> str:
        return "result"


class FakeEntryPoint:
    def __init__(self, factory) -> None:
        self.factory = factory

    def load(self):
        return self.factory


def test_load_extensions_passes_context_and_registers_tools(tmp_path: Path, monkeypatch) -> None:
    seen = []

    def factory(context: ExtensionContext):
        seen.append(context)
        return [ExtensionTool()]

    monkeypatch.setattr(
        "hal.extensions.discover_extensions",
        lambda: {"example": FakeEntryPoint(factory)},
    )
    registry = Registry([])
    load_extensions(
        registry, ["example"], tmp_path, tmp_path,
        {"example": {"endpoint": "https://example.test"}},
    )

    assert [spec.name for spec in registry.specs] == ["example_search"]
    assert seen == [ExtensionContext(
        "example", tmp_path, tmp_path,
        {"endpoint": "https://example.test"},
    )]


def test_load_extensions_reports_missing_extension(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("hal.extensions.discover_extensions", lambda: {})
    with pytest.raises(ValueError, match="extension 'missing' is not installed"):
        load_extensions(Registry([]), ["missing"], tmp_path, tmp_path)


def test_load_extensions_rejects_non_tools_and_collisions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "hal.extensions.discover_extensions",
        lambda: {"bad": FakeEntryPoint(lambda _context: [object()])},
    )
    with pytest.raises(ValueError, match="not a Tool"):
        load_extensions(Registry([]), ["bad"], tmp_path, tmp_path)

    monkeypatch.setattr(
        "hal.extensions.discover_extensions",
        lambda: {"example": FakeEntryPoint(lambda _context: [ExtensionTool()])},
    )
    with pytest.raises(ValueError, match="duplicate tool name: example_search"):
        load_extensions(
            Registry([ExtensionTool()]), ["example"], tmp_path, tmp_path,
        )


def test_cli_agent_factory_loads_configured_extensions(tmp_path: Path, monkeypatch) -> None:
    from hal.cli import _make_agent

    class Provider:
        streaming_enabled = True

    registry = Registry([])
    calls = []
    monkeypatch.setattr("hal.cli.load_skills", lambda _cwd: [])
    monkeypatch.setattr("hal.cli.resolve_phases", lambda _config: {})
    monkeypatch.setattr("hal.cli.create_provider", lambda _config: Provider())
    monkeypatch.setattr("hal.cli.build_system", lambda *_args: "system")
    monkeypatch.setattr("hal.cli.workspace_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("hal.cli.default_registry", lambda *_args: registry)
    monkeypatch.setattr(
        "hal.cli.load_extensions",
        lambda *args: calls.append(args),
    )
    config = Config(
        model="test-model",
        extensions=["example"],
        extension_config={"example": {"value": 1}},
    )

    agent, _, _ = _make_agent(config, tmp_path)

    assert agent.tools is registry
    assert calls == [(
        registry, ["example"], tmp_path, tmp_path,
        {"example": {"value": 1}},
    )]
