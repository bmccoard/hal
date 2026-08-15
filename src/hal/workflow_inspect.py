"""Read-only, JSON-safe workflow discovery and inspection."""
from __future__ import annotations

from dataclasses import fields
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .workflow_policy import (
    workflow_required_effects, workflow_requires_trust,
)
from .workflow_schema import WorkflowDefinition
from .workflow_publication import publication_isolation


def inspect_repository_workflow(definition: WorkflowDefinition) -> dict[str, Any]:
    """Describe an inert repository workflow without exposing environment values."""
    identity = definition.source.identity(definition.name)
    return {
        "name": definition.name,
        "description": definition.description,
        "version": definition.version,
        "origin": identity.origin.value,
        "digest": identity.digest,
        "path": definition.source.relative_path,
        "trust_required": workflow_requires_trust(definition),
        "effects": sorted(effect.value for effect in workflow_required_effects(definition)),
        "publication_isolation": publication_isolation(definition),
        "inputs": {
            name: {
                "type": item.type,
                "required": item.required,
                "has_default": item.has_default,
                **({"default": _json_value(item.default)} if item.has_default else {}),
            }
            for name, item in definition.inputs.items()
        },
        "execution": {
            "workspace": definition.execution.workspace,
            "max_parallel": definition.execution.max_parallel,
            "timeout_seconds": definition.execution.timeout_seconds,
            "budgets": {
                field.name: getattr(definition.execution.budgets, field.name)
                for field in fields(definition.execution.budgets)
            },
        },
        "nodes": [_inspect_node(node) for node in definition.nodes],
    }


def inspect_builtin_workflow(workflow: Any) -> dict[str, Any]:
    """Describe the legacy built-in workflow through the same identity surface."""
    identity = workflow.identity
    return {
        "name": workflow.name,
        "description": workflow.description,
        "version": 1,
        "origin": identity.origin.value,
        "digest": identity.digest,
        "path": None,
        "trust_required": False,
        "effects": ["model", "read", "workspace_mutation"],
        "phases": list(workflow.phases),
        "nodes": [],
    }


def workflow_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in ("name", "description", "origin", "digest", "trust_required", "effects")
    }


def _inspect_node(node: Any) -> dict[str, Any]:
    config = {name: _json_value(value) for name, value in node.config.items()}
    if "environment" in config:
        # Workflows may use literals as well as secret expressions. Inspection
        # reports the allow-list, never a value that could contain a credential.
        config["environment"] = {"names": sorted(node.config["environment"])}
    return {
        "id": node.id,
        "type": node.type,
        "depends_on": list(node.depends_on),
        "dependency_policy": node.dependency_policy,
        "condition": node.condition or None,
        "effects": sorted(effect.value for effect in node.effects),
        "resumable": node.resumable,
        "idempotent": node.idempotent,
        "inputs": {
            name: {"type": item.type, "value": _json_value(item.value)}
            for name, item in node.inputs.items()
        },
        "outputs": {
            name: {"type": item.type, "source": item.source}
            for name, item in node.outputs.items()
        },
        "config": config,
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
