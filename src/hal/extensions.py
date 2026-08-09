"""Discovery and loading for explicitly enabled HAL tool extensions."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

from .tools import Registry, Tool


ENTRY_POINT_GROUP = "hal.tools"


@dataclass(frozen=True, slots=True)
class ExtensionContext:
    """Runtime information passed to a tool extension factory."""

    name: str
    cwd: Path
    root: Path
    settings: dict[str, Any] = field(default_factory=dict)


ToolFactory = Callable[[ExtensionContext], Iterable[Tool]]


def discover_extensions() -> dict[str, metadata.EntryPoint]:
    """Return installed HAL tool entry points indexed by their public name."""
    entries = metadata.entry_points()
    selected = entries.select(group=ENTRY_POINT_GROUP) if hasattr(entries, "select") else entries.get(ENTRY_POINT_GROUP, ())
    discovered: dict[str, metadata.EntryPoint] = {}
    for entry in selected:
        if entry.name in discovered:
            raise ValueError(f"multiple installed HAL extensions are named {entry.name!r}")
        discovered[entry.name] = entry
    return discovered


def load_extensions(
    registry: Registry,
    enabled: list[str],
    cwd: Path,
    root: Path,
    extension_config: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Load enabled extension factories and add their tools to ``registry``."""
    if not enabled:
        return
    discovered = discover_extensions()
    extension_config = extension_config or {}
    for name in enabled:
        entry = discovered.get(name)
        if entry is None:
            available = ", ".join(sorted(discovered)) or "none"
            raise ValueError(
                f"HAL extension {name!r} is not installed (available: {available})"
            )
        try:
            factory = entry.load()
            tools = list(factory(ExtensionContext(
                name=name,
                cwd=cwd,
                root=root,
                settings=dict(extension_config.get(name, {})),
            )))
        except Exception as exc:
            raise ValueError(f"load HAL extension {name!r}: {exc}") from exc
        if not all(isinstance(tool, Tool) for tool in tools):
            raise ValueError(f"HAL extension {name!r} returned an object that is not a Tool")
        try:
            registry.extend(tools)
        except ValueError as exc:
            raise ValueError(f"load HAL extension {name!r}: {exc}") from exc
