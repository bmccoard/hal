from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hal.workflow_artifacts import WorkflowArtifactHandle, WorkflowArtifactStore
from hal.workflow_budgets import WorkflowBudgets
from hal.workflow_resume import audit_workflow_resume, resume_persisted_workflow
from hal.workflow_runtime import WorkflowNodeReceipt, WorkflowNodeRecord
from hal.workflow_schema import WORKFLOW_DIRECTORY, WorkflowNodeStatus, load_workflow
from hal.workflow_state import WorkflowRunStore, execute_persisted_workflow


def _definition(root: Path, prompt: str = "work"):
    directory = root / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "resume.yaml"
    path.write_text(f"""
version: 1
name: resume
nodes:
  - id: work
    type: agent
    prompt: {prompt}
    outputs:
      report: {{type: markdown, source: final_response}}
""".lstrip(), encoding="utf-8")
    return load_workflow(path, root)


def _completed(root: Path):
    definition = _definition(root)
    artifact_store = WorkflowArtifactStore(root / "artifacts")
    handle = WorkflowArtifactHandle(artifact_store.put(
        "done", type="markdown", producer="work", media_type="text/markdown",
    ))
    store = WorkflowRunStore(root / "runs")
    state = store.create(definition, {}, root, WorkflowBudgets(node_attempts=2))
    execute_persisted_workflow(
        definition, {},
        lambda _invocation: WorkflowNodeReceipt(
            WorkflowNodeStatus.SUCCEEDED, {"report": handle},
        ),
        state,
    )
    return definition, artifact_store, store, state, handle


def test_resume_audit_validates_completed_nodes_and_artifacts(tmp_path: Path) -> None:
    definition, artifacts, _store, state, _handle = _completed(tmp_path)

    audit = audit_workflow_resume(state, definition, artifacts, workspace=tmp_path)

    assert audit.completed_nodes == ("work",)
    assert audit.indeterminate_nodes == ()


def test_resume_audit_rejects_stale_definition_and_artifact(tmp_path: Path) -> None:
    definition, artifacts, _store, state, handle = _completed(tmp_path)
    changed = _definition(tmp_path, "changed")
    with pytest.raises(ValueError, match="digest changed"):
        audit_workflow_resume(state, changed, artifacts)

    assert handle.artifact.location is not None
    (artifacts.directory / handle.artifact.location).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="size|digest"):
        audit_workflow_resume(state, definition, artifacts)


def test_resume_audit_rejects_budget_overage_and_active_lease(tmp_path: Path) -> None:
    definition, artifacts, store, state, _handle = _completed(tmp_path)
    state.payload["usage"]["node_attempts"] = 3
    store._write(state.payload)
    with pytest.raises(ValueError, match="exceeds persisted node_attempts"):
        audit_workflow_resume(store.load(state.run_id), definition, artifacts)

    state.payload["usage"]["node_attempts"] = 1
    state.payload["lease"] = {
        "owner": "worker-1",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }
    store._write(state.payload)
    with pytest.raises(ValueError, match="actively leased"):
        audit_workflow_resume(store.load(state.run_id), definition, artifacts)


def test_resume_audit_classifies_missing_completion_receipt_as_indeterminate(
    tmp_path: Path,
) -> None:
    definition = _definition(tmp_path)
    artifacts = WorkflowArtifactStore(tmp_path / "artifacts")
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets())
    state.transition("work", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY)
    state.transition("work", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING)

    audit = audit_workflow_resume(
        store.load(state.run_id), definition, artifacts,
    )

    assert audit.indeterminate_nodes == ("work",)
    assert audit.completed_nodes == ()


def test_resume_audit_rejects_missing_workspace_and_malformed_receipt(tmp_path: Path) -> None:
    definition, artifacts, store, state, _handle = _completed(tmp_path)
    missing = tmp_path / "missing-worktree"
    state.payload["workspace"]["path"] = str(missing)
    store._write(state.payload)
    with pytest.raises(ValueError, match="workspace is missing"):
        audit_workflow_resume(store.load(state.run_id), definition, artifacts)

    state.payload["workspace"]["path"] = str(tmp_path)
    state.payload["nodes"]["work"]["attempts"][-1]["receipt_at"] = None
    store._write(state.payload)
    with pytest.raises(ValueError, match="missing its completion receipt"):
        audit_workflow_resume(store.load(state.run_id), definition, artifacts)


def test_resume_never_reruns_successful_nodes(tmp_path: Path) -> None:
    directory = tmp_path / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True)
    path = directory / "resume-two.yaml"
    path.write_text("""
version: 1
name: resume-two
nodes:
  - id: first
    type: agent
    prompt: first
    outputs:
      result: {type: markdown, source: final_response}
  - id: second
    type: agent
    prompt: second
    depends_on: [first]
    inputs:
      result: {type: markdown, value: "${{ nodes.first.outputs.result }}"}
""".lstrip(), encoding="utf-8")
    definition = load_workflow(path, tmp_path)
    artifacts = WorkflowArtifactStore(tmp_path / "artifacts")
    handle = WorkflowArtifactHandle(artifacts.put(
        "first result", type="markdown", producer="first", media_type="text/markdown",
    ))
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets())
    state.transition("first", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY)
    state.transition("first", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING)
    state.receipt("first", WorkflowNodeRecord(
        "first", WorkflowNodeStatus.SUCCEEDED, {"result": handle}, "completed",
    ))
    calls = []

    result = resume_persisted_workflow(
        store.load(state.run_id), definition, artifacts,
        lambda invocation: (
            calls.append(invocation.node.id)
            or WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)
        ),
    )

    assert result.status.value == "succeeded"
    assert calls == ["second"]
    assert isinstance(result.node("first").outputs["result"], WorkflowArtifactHandle)


def test_indeterminate_nonresumable_node_requires_explicit_retry(tmp_path: Path) -> None:
    definition = _definition(tmp_path)
    artifacts = WorkflowArtifactStore(tmp_path / "artifacts")
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets())
    state.transition("work", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY)
    state.transition("work", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING)
    calls = []

    with pytest.raises(PermissionError, match="explicit retry"):
        resume_persisted_workflow(
            store.load(state.run_id), definition, artifacts,
            lambda invocation: calls.append(invocation.node.id),
        )
    assert calls == []
    interrupted = store.load(state.run_id)
    assert interrupted.payload["nodes"]["work"]["status"] == "interrupted"

    result = resume_persisted_workflow(
        interrupted, definition, artifacts,
        lambda invocation: (
            calls.append(invocation.node.id)
            or WorkflowNodeReceipt(
                WorkflowNodeStatus.SUCCEEDED, {"report": "retried"},
            )
        ),
        retry_nodes=frozenset({"work"}),
    )
    assert result.status.value == "succeeded"
    assert calls == ["work"]
