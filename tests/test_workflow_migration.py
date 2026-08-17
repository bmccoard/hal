from __future__ import annotations

from pathlib import Path

import pytest

from hal.workflow_budgets import WorkflowBudgets
from hal.workflow_migration import migrate_workflow_definition
from hal.workflow_schema import WORKFLOW_DIRECTORY, WorkflowNodeStatus, load_workflow
from hal.workflow_state import WorkflowRunStore


def _write(root: Path, nodes: str, inputs: str = "", execution: str = ""):
    directory = root / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "migration.yaml"
    path.write_text(
        f"version: 1\nname: migration\n{execution}{inputs}nodes:\n{nodes}",
        encoding="utf-8",
    )
    return load_workflow(path, root)


def test_explicit_migration_preserves_digest_history_and_adds_pending_nodes(tmp_path: Path) -> None:
    original = _write(
        tmp_path,
        "  - {id: first, type: agent, prompt: first}\n",
        execution="execution: {budgets: {provider_calls: 10}}\n",
    )
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(original, {}, tmp_path, original.execution.budgets)
    updated = _write(tmp_path, """
  - {id: first, type: agent, prompt: revised prompt}
  - {id: second, type: agent, prompt: second, depends_on: [first]}
""", execution="execution: {budgets: {provider_calls: 50}}\n")

    migrate_workflow_definition(
        state, updated, actor="tester", reason="add validation step",
    )

    persisted = store.load(state.run_id).payload
    assert persisted["workflow"]["digest"] == updated.source.digest
    assert persisted["migrations"] == [{
        "from_digest": original.source.digest,
        "to_digest": updated.source.digest,
        "actor": "tester",
        "reason": "add validation step",
        "timestamp": persisted["migrations"][0]["timestamp"],
    }]
    assert persisted["nodes"]["first"]["status"] == "pending"
    assert persisted["nodes"]["second"]["status"] == "pending"
    assert persisted["budgets"]["provider_calls"] == 50
    assert persisted["events"][-1]["event"] == "definition_migrated"


@pytest.mark.parametrize(
    ("nodes", "inputs", "message"),
    [
        ("  - {id: replacement, type: agent, prompt: work}\n", "", "cannot remove"),
        ("  - {id: first, type: command, command: {argv: [test]}}\n", "", "node contract"),
        (
            "  - {id: first, type: agent, prompt: work}\n",
            "inputs:\n  request: {type: string}\n",
            "input contract",
        ),
    ],
)
def test_migration_rejects_incompatible_graph_or_inputs(
    tmp_path: Path, nodes: str, inputs: str, message: str,
) -> None:
    original = _write(tmp_path, "  - {id: first, type: agent, prompt: first}\n")
    state = WorkflowRunStore(tmp_path / "runs").create(
        original, {}, tmp_path, WorkflowBudgets(),
    )
    changed = _write(tmp_path, nodes, inputs)
    with pytest.raises(ValueError, match=message):
        migrate_workflow_definition(state, changed, actor="tester", reason="change")


def test_migration_rejects_in_flight_or_leased_runs(tmp_path: Path) -> None:
    original = _write(tmp_path, "  - {id: first, type: agent, prompt: first}\n")
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(original, {}, tmp_path, WorkflowBudgets())
    changed = _write(tmp_path, """
  - {id: first, type: agent, prompt: revised}
  - {id: second, type: agent, prompt: second}
""")
    state.transition("first", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY)
    state.transition("first", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING)
    with pytest.raises(ValueError, match="in-flight"):
        migrate_workflow_definition(state, changed, actor="tester", reason="change")

    state.payload["nodes"]["first"]["status"] = "interrupted"
    state.payload["lease"] = {"owner": "worker"}
    with pytest.raises(ValueError, match="leased"):
        migrate_workflow_definition(state, changed, actor="tester", reason="change")
