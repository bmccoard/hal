"""Packaged, inert workflow templates for explicit repository initialization."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

from .workflow_schema import (
    MAX_WORKFLOW_SOURCE_BYTES,
    WorkflowDefinition,
    WorkflowSource,
    parse_workflow_definition,
    workflow_directory,
)


TEMPLATE_DIRECTORY = Path(__file__).resolve().parent / "workflow_template_assets"
REQUIRED_TEMPLATE_NAMES = frozenset({
    "project-setup", "reviewed-change", "simple-change",
})


@dataclass(frozen=True, slots=True)
class WorkflowTemplate:
    name: str
    description: str
    digest: str
    path: Path
    content: str
    definition: WorkflowDefinition


def discover_workflow_templates() -> Mapping[str, WorkflowTemplate]:
    """Strictly load packaged templates without making them runnable workflows."""
    templates: dict[str, WorkflowTemplate] = {}
    for path in sorted(TEMPLATE_DIRECTORY.glob("*.yaml"), key=lambda item: item.name):
        raw = path.read_bytes()
        if len(raw) > MAX_WORKFLOW_SOURCE_BYTES:
            raise ValueError(f"workflow template {path.name!r} exceeds the source limit")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"workflow template {path.name!r} must be UTF-8") from exc
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid workflow template {path.name!r}: {exc}") from exc
        digest = sha256(raw).hexdigest()
        source = WorkflowSource(TEMPLATE_DIRECTORY, path, path.name, digest)
        definition = parse_workflow_definition(data, source)
        if definition.name in templates:
            raise ValueError(f"duplicate workflow template {definition.name!r}")
        templates[definition.name] = WorkflowTemplate(
            definition.name, definition.description, digest, path, content, definition,
        )
    if missing := REQUIRED_TEMPLATE_NAMES - set(templates):
        raise ValueError(
            "packaged workflow template(s) missing: " + ", ".join(sorted(missing))
        )
    return MappingProxyType(templates)


def initialize_workflow_template(
    name: str, repository: Path,
) -> tuple[WorkflowTemplate, Path]:
    """Copy one template into a repository without overwriting any existing file."""
    templates = discover_workflow_templates()
    try:
        template = templates[name]
    except KeyError as exc:
        available = ", ".join(templates) or "none"
        raise ValueError(
            f"unknown workflow template {name!r} (available: {available})"
        ) from exc

    repository = repository.resolve()
    directory = workflow_directory(repository)
    metadata_directory = directory.parent
    if metadata_directory.exists():
        if not metadata_directory.resolve().is_relative_to(repository):
            raise ValueError(
                "repository metadata directory resolves outside the workspace: "
                f"{metadata_directory}"
            )
    else:
        metadata_directory.mkdir()
    if directory.exists():
        if not directory.resolve().is_relative_to(repository):
            raise ValueError(
                f"repository workflow directory resolves outside the workspace: {directory}"
            )
    else:
        directory.mkdir()
    resolved_directory = directory.resolve()
    destination = resolved_directory / f"{template.name}.yaml"
    try:
        with destination.open("x", encoding="utf-8", newline="") as handle:
            handle.write(template.content)
    except FileExistsError as exc:
        raise FileExistsError(
            f"workflow already exists and was not overwritten: {destination}"
        ) from exc
    return template, destination
