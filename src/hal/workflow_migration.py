"""Explicit compatibility checks for pinned workflow definition migration."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .workflow_schema import WorkflowDefinition
from .workflow_state import WorkflowRunState


def migrate_workflow_definition(
    state: WorkflowRunState,
    definition: WorkflowDefinition,
    *,
    actor: str,
    reason: str,
) -> None:
    payload = state.payload
    pinned = payload["workflow"]
    if not actor.strip() or not reason.strip():
        raise ValueError("migration actor and reason must not be empty")
    if pinned["name"] != definition.name:
        raise ValueError("migration cannot rename a workflow")
    if pinned["schema_version"] != definition.version:
        raise ValueError("migration cannot change workflow schema version")
    if pinned["repository"] != str(definition.source.repository):
        raise ValueError("migration cannot change repository identity")
    if pinned["digest"] == definition.source.digest:
        raise ValueError("workflow run already uses this definition digest")
    if payload.get("lease") is not None:
        raise ValueError("leased workflow run cannot be migrated")
    if any(node["status"] == "running" for node in payload["nodes"].values()):
        raise ValueError("workflow with in-flight nodes cannot be migrated")
    expected_inputs = pinned.get("inputs", {})
    actual_inputs = {
        name: {
            "type": item.type, "required": item.required,
            "has_default": item.has_default,
            "default": item.default if item.has_default else None,
        }
        for name, item in definition.inputs.items()
    }
    if actual_inputs != expected_inputs:
        raise ValueError("migration cannot change the workflow input contract")
    old_graph = {item["id"]: item for item in payload["graph"]}
    new_graph = {node.id: node for node in definition.nodes}
    removed = set(old_graph) - set(new_graph)
    if removed:
        raise ValueError(f"migration cannot remove node(s): {', '.join(sorted(removed))}")
    for node_id, old in old_graph.items():
        new = new_graph[node_id]
        new_outputs = {
            name: {"type": item.type, "source": item.source}
            for name, item in new.outputs.items()
        }
        if old["type"] != new.type or old["outputs"] != new_outputs:
            raise ValueError(f"migration changes node contract for {node_id!r}")
        if old["depends_on"] != list(new.depends_on):
            raise ValueError(f"migration changes dependencies for existing node {node_id!r}")
    graph = [_graph_item(node) for node in definition.nodes]
    workflow = {
        "name": definition.name,
        "schema_version": definition.version,
        "origin": "repository",
        "digest": definition.source.digest,
        "repository": str(definition.source.repository),
        "definition_path": definition.source.relative_path,
        "inputs": expected_inputs,
    }
    state.commit_migration(
        workflow, graph, tuple(node_id for node_id in new_graph if node_id not in old_graph),
        actor.strip(), reason.strip(), asdict(definition.execution.budgets),
    )


def _graph_item(node: Any) -> dict[str, Any]:
    return {
        "id": node.id, "type": node.type, "depends_on": list(node.depends_on),
        "dependency_policy": node.dependency_policy, "condition": node.condition,
        "effects": sorted(effect.value for effect in node.effects),
        "outputs": {
            name: {"type": item.type, "source": item.source}
            for name, item in node.outputs.items()
        },
        "resumable": node.resumable, "idempotent": node.idempotent,
    }
