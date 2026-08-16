from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

import pytest
import hal.workflow_worktrees as workflow_worktrees

from hal.workflow_artifacts import WorkflowArtifactHandle, WorkflowArtifactStore
from hal.workflow_budgets import WorkflowBudgets
from hal.workflow_resume import audit_workflow_resume, resume_persisted_workflow
from hal.workflow_runtime import (
    WorkflowNodeReceipt, WorkflowNodeRecord, WorkflowTransientError,
)
from hal.workflow_schema import WORKFLOW_DIRECTORY, WorkflowNodeStatus, load_workflow
from hal.workflow_state import WorkflowRunStore, execute_persisted_workflow
from hal.workflow_worktrees import WorkflowWorktreeIdentity, validate_workspace_claim


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


def test_resume_audit_requires_exact_pinned_validated_workspace_claims(
    tmp_path: Path, monkeypatch,
) -> None:
    definition, artifacts, _store, state, _handle = _completed(tmp_path)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    identity = WorkflowWorktreeIdentity(
        isolated.resolve(), "abc123", "hal/isolated", "clean", (), True,
    )
    monkeypatch.setattr(workflow_worktrees, "inspect_worktree", lambda *_args: identity)
    claim = validate_workspace_claim(tmp_path, isolated, {
        "path": str(isolated), "head": identity.head, "branch": identity.branch,
        "checkpoint_dirty_digest": identity.dirty_digest,
    })
    state.payload["workspace_claims"] = {
        "work": {
            "path": str(claim.path), "repository": str(claim.repository),
            "branch": claim.branch,
        }
    }

    with pytest.raises(ValueError, match="missing or changed"):
        audit_workflow_resume(state, definition, artifacts)

    audit = audit_workflow_resume(
        state, definition, artifacts, workspace_claims={"work": claim},
    )
    assert audit.completed_nodes == ("work",)


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


def test_resume_continues_after_last_completed_loop_attempt(tmp_path: Path) -> None:
    directory = tmp_path / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True)
    path = directory / "resume-loop.yaml"
    path.write_text("""
version: 1
name: resume-loop
nodes:
  - id: work
    type: agent
    prompt: work
    outputs:
      complete: {type: boolean, source: structured_response}
    loop:
      max_attempts: 3
      until: "${{ node.outputs.complete == true }}"
""".lstrip(), encoding="utf-8")
    definition = load_workflow(path, tmp_path)
    artifacts = WorkflowArtifactStore(tmp_path / "artifacts")
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets(node_attempts=3))
    state.transition("work", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY)
    state.transition("work", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING)
    state.continue_loop("work", WorkflowNodeRecord(
        "work", WorkflowNodeStatus.SUCCEEDED, {"complete": False},
        attempt_status=WorkflowNodeStatus.SUCCEEDED,
        attempt_elapsed_seconds=0.25,
    ))
    calls = []

    def execute(invocation):
        calls.append((invocation.attempt_id, invocation.attempt_index))
        return WorkflowNodeReceipt(
            WorkflowNodeStatus.SUCCEEDED, {"complete": True},
        )

    result = resume_persisted_workflow(
        store.load(state.run_id), definition, artifacts, execute,
    )
    payload = store.load(state.run_id).payload

    assert result.status.value == "succeeded"
    assert calls == [("attempt_2", 2)]
    assert [item["id"] for item in payload["nodes"]["work"]["attempts"]] == [
        "attempt_1", "attempt_2",
    ]


def test_resume_honors_durable_retry_backoff_and_attempt_cap(tmp_path: Path) -> None:
    directory = tmp_path / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True)
    path = directory / "resume-retry.yaml"
    path.write_text("""
version: 1
name: resume-retry
nodes:
  - id: work
    type: approval
    prompt: work
    retry:
      max_attempts: 2
      error_classes: [network]
      initial_backoff_seconds: 30
""".lstrip(), encoding="utf-8")
    definition = load_workflow(path, tmp_path)
    artifacts = WorkflowArtifactStore(tmp_path / "artifacts")
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets(node_attempts=2))
    state.transition("work", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY)
    state.transition("work", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING)
    state.continue_loop("work", WorkflowNodeRecord(
        "work", WorkflowNodeStatus.FAILED,
        reason="offline", attempt_status=WorkflowNodeStatus.FAILED,
        error_class="network", retry_delay_seconds=30,
    ))

    class Executor:
        waits = []
        calls = []

        def wait_for_retry(self, seconds):
            self.waits.append(seconds)

        def __call__(self, invocation):
            self.calls.append((invocation.attempt_id, invocation.retry_index))
            raise WorkflowTransientError("network", "still offline")

    executor = Executor()
    result = resume_persisted_workflow(
        store.load(state.run_id), definition, artifacts, executor,
    )
    payload = store.load(state.run_id).payload

    assert result.status.value == "failed"
    assert len(executor.waits) == 1
    assert 0 < executor.waits[0] <= 30
    assert executor.calls == [("attempt_2", 1)]
    assert len(payload["nodes"]["work"]["attempts"]) == 2
    assert [event["event"] for event in payload["events"]].count("retry_scheduled") == 1


def test_concurrent_resume_keeps_completed_nodes_and_runs_ready_peers_in_parallel(
    tmp_path: Path,
) -> None:
    directory = tmp_path / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True)
    path = directory / "resume-concurrent.yaml"
    path.write_text("""
version: 1
name: resume-concurrent
execution:
  max_parallel: 2
nodes:
  - {id: completed, type: agent, prompt: completed}
  - {id: second, type: agent, prompt: second, depends_on: [completed]}
  - {id: third, type: agent, prompt: third, depends_on: [completed]}
""".lstrip(), encoding="utf-8")
    definition = load_workflow(path, tmp_path)
    artifacts = WorkflowArtifactStore(tmp_path / "artifacts")
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets(node_attempts=3))
    state.transition("completed", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY)
    state.transition("completed", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING)
    state.receipt("completed", WorkflowNodeRecord(
        "completed", WorkflowNodeStatus.SUCCEEDED,
    ))
    barrier = threading.Barrier(2)
    calls = []

    def execute(invocation):
        calls.append(invocation.node.id)
        barrier.wait(timeout=2)
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

    result = resume_persisted_workflow(
        store.load(state.run_id), definition, artifacts, execute,
        max_parallel=2,
    )

    assert result.status.value == "succeeded"
    assert set(calls) == {"second", "third"}


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
