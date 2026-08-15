from __future__ import annotations

from pathlib import Path

import pytest

from hal.workflow_approvals import (
    StaleApprovalError, WorkflowApprovalDecision, authorize_approval_decision,
    decide_approval, pending_approval,
)
from hal.workflow_artifacts import WorkflowArtifactStore
from hal.workflow_budgets import WorkflowBudgets
from hal.workflow_nodes import WorkflowNodeDispatcher
from hal.workflow_resume import resume_persisted_workflow
from hal.workflow_runtime import WorkflowNodeReceipt
from hal.workflow_schema import WORKFLOW_DIRECTORY, WorkflowNodeStatus, load_workflow
from hal.workflow_state import WorkflowRunStore, execute_persisted_workflow


def _definition(root: Path):
    directory = root / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True)
    path = directory / "approval.yaml"
    path.write_text("""
version: 1
name: approval
execution: {workspace: worktree}
nodes:
  - id: approve
    type: approval
    prompt: Approve publication?
    outputs:
      outcome: {type: string, source: outcome}
      feedback: {type: string, source: feedback}
  - id: publish
    type: command
    depends_on: [approve]
    command: {argv: [publish]}
""".lstrip(), encoding="utf-8")
    return load_workflow(path, root)


def _waiting(root: Path):
    definition = _definition(root)
    store = WorkflowRunStore(root / "runs")
    state = store.create(definition, {}, root, WorkflowBudgets(node_attempts=4))
    state.attach_trust(
        definition.source.digest,
        ("approval", "command_execution", "workspace_mutation"),
    )
    dispatcher = WorkflowNodeDispatcher(root)
    result = execute_persisted_workflow(definition, {}, dispatcher, state)
    return definition, store, state, result


def test_approval_waits_durably_across_disconnect(tmp_path: Path) -> None:
    _definition_value, store, state, result = _waiting(tmp_path)

    persisted = store.load(state.run_id).payload
    approval = pending_approval(store.load(state.run_id), "approve")
    assert result.status.value == "waiting"
    assert persisted["status"] == "waiting"
    assert persisted["completed_at"] is None
    assert persisted["nodes"]["approve"]["status"] == "waiting"
    assert persisted["nodes"]["publish"]["status"] == "pending"
    assert approval["prompt"] == "Approve publication?"
    assert approval["decision"] is None
    assert approval["revision_token"].endswith(approval["review_digest"])
    assert approval["consequences"] == [{
        "id": "publish", "type": "command",
        "effects": ["command_execution", "workspace_mutation"],
    }]


def test_approve_records_identity_feedback_and_resumes_without_rerunning_gate(
    tmp_path: Path,
) -> None:
    definition, store, state, _result = _waiting(tmp_path)
    approval = pending_approval(state, "approve")
    authorize_approval_decision(state, definition, WorkflowArtifactStore(tmp_path / "artifacts"),
        WorkflowApprovalDecision(
        "approve", "approve", "alice@example.com", "looks good",
        approval["revision_token"],
    ))
    calls = []
    artifacts = WorkflowArtifactStore(tmp_path / "artifacts")

    result = resume_persisted_workflow(
        store.load(state.run_id), definition, artifacts,
        lambda invocation: (
            calls.append(invocation.node.id)
            or WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)
        ),
    )

    assert result.status.value == "succeeded"
    assert calls == ["publish"]
    assert result.node("approve").outputs == {
        "outcome": "approve", "feedback": "looks good",
    }
    decided = store.load(state.run_id).payload["approvals"][0]
    assert decided["approver"] == "alice@example.com"
    assert decided["decided_at"] is not None


@pytest.mark.parametrize(
    ("decision", "run_status"),
    [("deny", "denied"), ("request_changes", "denied"), ("cancel", "cancelled")],
)
def test_negative_decisions_prevent_dependent_side_effects(
    tmp_path: Path, decision: str, run_status: str,
) -> None:
    _definition_value, store, state, _result = _waiting(tmp_path)
    approval = pending_approval(state, "approve")
    decide_approval(state, WorkflowApprovalDecision(
        "approve", decision, "reviewer", "not authorized",
        approval["revision_token"],
    ))

    persisted = store.load(state.run_id).payload
    assert persisted["status"] == run_status
    assert persisted["nodes"]["publish"]["status"] == "skipped"
    assert persisted["approvals"][0]["decision"] == decision


def test_stale_revision_and_changed_review_snapshot_fail_closed(tmp_path: Path) -> None:
    _definition_value, store, state, _result = _waiting(tmp_path)
    approval = pending_approval(state, "approve")
    with pytest.raises(ValueError, match="revision token is stale"):
        decide_approval(state, WorkflowApprovalDecision(
            "approve", "approve", "reviewer", "", "old-token",
        ))

    state.update_workspace_checkpoint(
        head="a" * 40, branch="hal/approval/run",
        dirty_digest="b" * 64, dirty_paths=("changed.py",),
    )
    with pytest.raises(StaleApprovalError) as raised:
        decide_approval(state, WorkflowApprovalDecision(
            "approve", "approve", "reviewer", "", approval["revision_token"],
        ))
    refreshed = pending_approval(store.load(state.run_id), "approve")
    assert refreshed["stale"] is True
    assert refreshed["revision_token"] == raised.value.token
    assert refreshed["workspace_checkpoint"]["checkpoint_dirty_paths"] == ["changed.py"]


def test_authenticated_approver_identity_is_required(tmp_path: Path) -> None:
    _definition_value, _store, state, _result = _waiting(tmp_path)
    approval = pending_approval(state, "approve")
    with pytest.raises(ValueError, match="authenticated approver"):
        decide_approval(state, WorkflowApprovalDecision(
            "approve", "approve", "", "", approval["revision_token"],
        ))
