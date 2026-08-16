from __future__ import annotations

import json
from pathlib import Path

import pytest
import hal.workflow_worktrees as workflow_worktrees

from hal.workflow_artifacts import WorkflowArtifactHandle, WorkflowArtifactStore
from hal.workflow_budgets import WorkflowBudgets, WorkflowUsage
from hal.workflow_runtime import (
    WorkflowNodeReceipt, WorkflowTransientError,
)
from hal.workflow_schema import WORKFLOW_DIRECTORY, WorkflowNodeStatus, load_workflow
from hal.workflow_state import (
    WORKFLOW_RUN_RECORD_VERSION, WorkflowRunStore,
    execute_persisted_concurrent_workflow, execute_persisted_workflow,
)
from hal.workflow_worktrees import WorkflowWorktreeIdentity, validate_workspace_claim


def _definition(tmp_path: Path):
    directory = tmp_path / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True)
    path = directory / "durable.yaml"
    path.write_text("""
version: 1
name: durable
inputs:
  request: {type: string, default: default request}
nodes:
  - id: command
    type: command
    command: {argv: [tool]}
    environment:
      API_TOKEN: must-not-be-persisted
    outputs:
      report: {type: check_result, source: result}
""".lstrip(), encoding="utf-8")
    return load_workflow(path, tmp_path)


def test_run_record_is_versioned_atomic_sanitized_and_complete(tmp_path: Path) -> None:
    definition = _definition(tmp_path)
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(
        definition, {}, tmp_path, WorkflowBudgets(node_attempts=3),
        branch="hal/run", head="abc123",
    )
    artifact_store = WorkflowArtifactStore(tmp_path / "artifacts")
    handle = WorkflowArtifactHandle(artifact_store.put(
        '{"exit_code":0}', type="check_result", producer="command",
        media_type="application/json",
    ))

    result = execute_persisted_workflow(
        definition, {},
        lambda _invocation: WorkflowNodeReceipt(
            WorkflowNodeStatus.SUCCEEDED, {"report": handle}, outcome="completed",
        ),
        state,
        lambda: WorkflowUsage(node_attempts=1, elapsed_seconds=.25),
    )
    payload = store.load(state.run_id).payload
    rendered = json.dumps(payload)

    assert result.status.value == "succeeded"
    assert payload["version"] == WORKFLOW_RUN_RECORD_VERSION
    assert payload["workflow"]["digest"] == definition.source.digest
    assert payload["inputs"] == {"request": "default request"}
    assert payload["workspace"]["branch"] == "hal/run"
    assert payload["nodes"]["command"]["attempts"][0]["status"] == "succeeded"
    assert payload["artifacts"][handle.artifact.digest]["size"] == handle.artifact.size
    assert payload["usage"]["node_attempts"] == 1
    assert payload["completed_at"] is not None
    assert "must-not-be-persisted" not in rendered
    assert not tuple((tmp_path / "runs").glob("*.tmp"))


def test_attempt_intent_is_durable_before_executor_side_effect(tmp_path: Path) -> None:
    definition = _definition(tmp_path)
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets())

    def execute(_invocation):
        persisted = store.load(state.run_id).payload
        node = persisted["nodes"]["command"]
        assert node["status"] == "running"
        assert node["attempts"][0]["status"] == "running"
        assert node["attempts"][0]["receipt_at"] is None
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED, {"report": {}})

    execute_persisted_workflow(definition, {}, execute, state)

    events = store.load(state.run_id).payload["events"]
    names = [event["event"] for event in events]
    assert names.index("attempt_intent") < names.index("attempt_receipt")


def test_concurrent_run_pins_validated_workspace_claims_before_dispatch(
    tmp_path: Path, monkeypatch,
) -> None:
    definition = _definition(tmp_path)
    workspace = tmp_path / "isolated"
    workspace.mkdir()
    identity = WorkflowWorktreeIdentity(
        workspace.resolve(), "abc123", "hal/isolated", "clean", (), True,
    )
    monkeypatch.setattr(workflow_worktrees, "inspect_worktree", lambda *_args: identity)
    claim = validate_workspace_claim(tmp_path, workspace, {
        "path": str(workspace), "head": identity.head, "branch": identity.branch,
        "checkpoint_dirty_digest": identity.dirty_digest,
    })
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets())

    execute_persisted_concurrent_workflow(
        definition, {},
        lambda invocation: WorkflowNodeReceipt(
            WorkflowNodeStatus.SUCCEEDED, {"report": {}},
        ),
        state, max_parallel=2, workspace_claims={"command": claim},
    )

    payload = store.load(state.run_id).payload
    assert payload["workspace_claims"] == {
        "command": {
            "path": str(workspace.resolve()),
            "repository": str(tmp_path.resolve()),
            "branch": "hal/isolated",
        }
    }
    names = [event["event"] for event in payload["events"]]
    assert names.index("workspace_claims_bound") < names.index("attempt_intent")


def test_failure_before_intent_prevents_dispatch(tmp_path: Path, monkeypatch) -> None:
    definition = _definition(tmp_path)
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets())
    original = store._write
    writes = 0

    def fail_intent(payload):
        nonlocal writes
        writes += 1
        if writes == 2:  # ready persists first; running intent is second
            raise OSError("injected intent failure")
        return original(payload)

    monkeypatch.setattr(store, "_write", fail_intent)
    called = False

    def execute(_invocation):
        nonlocal called
        called = True
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED, {"report": {}})

    with pytest.raises(OSError, match="intent failure"):
        execute_persisted_workflow(definition, {}, execute, state)
    assert called is False
    persisted = store.load(state.run_id).payload
    assert persisted["nodes"]["command"]["status"] == "ready"


def test_failure_after_side_effect_leaves_durable_in_flight_attempt(tmp_path: Path, monkeypatch) -> None:
    definition = _definition(tmp_path)
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets())
    original = store._write
    writes = 0

    def fail_receipt(payload):
        nonlocal writes
        writes += 1
        if writes == 3:  # ready, intent, then completion receipt
            raise OSError("injected receipt failure")
        return original(payload)

    monkeypatch.setattr(store, "_write", fail_receipt)
    calls = 0

    def execute(_invocation):
        nonlocal calls
        calls += 1
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED, {"report": {}})

    with pytest.raises(OSError, match="receipt failure"):
        execute_persisted_workflow(definition, {}, execute, state)
    assert calls == 1
    persisted = store.load(state.run_id).payload
    attempt = persisted["nodes"]["command"]["attempts"][0]
    assert persisted["nodes"]["command"]["status"] == "running"
    assert attempt["status"] == "running"
    assert attempt["receipt_at"] is None


@pytest.mark.parametrize("content", ["{broken", "[]"])
def test_corrupt_or_partial_records_fail_closed(tmp_path: Path, content: str) -> None:
    store = WorkflowRunStore(tmp_path)
    path = tmp_path / "wfrun_0123456789abcdef.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt|must be an object"):
        store.load("wfrun_0123456789abcdef")


def test_unknown_record_version_fails_closed(tmp_path: Path) -> None:
    store = WorkflowRunStore(tmp_path)
    path = tmp_path / "wfrun_0123456789abcdef.json"
    path.write_text(
        '{"version": 999, "run_id": "wfrun_0123456789abcdef"}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported.*version"):
        store.load("wfrun_0123456789abcdef")


def test_loop_attempts_are_individually_journaled(tmp_path: Path) -> None:
    directory = tmp_path / WORKFLOW_DIRECTORY
    path = directory / "loop.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("""
version: 1
name: loop
nodes:
  - id: work
    type: agent
    prompt: work
    outputs:
      complete: {type: boolean, source: structured_response}
    loop:
      max_attempts: 2
      until: "${{ node.outputs.complete == true }}"
""".lstrip(), encoding="utf-8")
    definition = load_workflow(path, tmp_path)
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets(node_attempts=2))
    calls = 0

    def execute(_invocation):
        nonlocal calls
        calls += 1
        return WorkflowNodeReceipt(
            WorkflowNodeStatus.SUCCEEDED, {"complete": False},
        )

    result = execute_persisted_workflow(definition, {}, execute, state)
    payload = store.load(state.run_id).payload
    attempts = payload["nodes"]["work"]["attempts"]

    assert result.status.value == "failed"
    assert payload["nodes"]["work"]["status"] == "failed"
    assert [item["id"] for item in attempts] == ["attempt_1", "attempt_2"]
    assert [item["status"] for item in attempts] == ["succeeded", "succeeded"]
    assert all(item["receipt_at"] is not None for item in attempts)
    assert [event["event"] for event in payload["events"]].count("attempt_intent") == 2
    assert [event["event"] for event in payload["events"]].count("loop_attempt_receipt") == 1


def test_transient_retries_are_durably_journaled(tmp_path: Path) -> None:
    directory = tmp_path / WORKFLOW_DIRECTORY
    path = directory / "retry.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("""
version: 1
name: retry
nodes:
  - id: work
    type: approval
    prompt: work
    retry:
      max_attempts: 2
      error_classes: [network]
      initial_backoff_seconds: 0.01
""".lstrip(), encoding="utf-8")
    definition = load_workflow(path, tmp_path)
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets(node_attempts=2))

    class Executor:
        calls = 0
        waits = []

        def __call__(self, _invocation):
            self.calls += 1
            if self.calls == 1:
                raise WorkflowTransientError("network", "offline")
            return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

        def wait_for_retry(self, seconds):
            self.waits.append(seconds)

    executor = Executor()
    result = execute_persisted_workflow(definition, {}, executor, state)
    payload = store.load(state.run_id).payload
    attempts = payload["nodes"]["work"]["attempts"]

    assert result.status.value == "succeeded"
    assert executor.waits == [0.01]
    assert [item["id"] for item in attempts] == ["attempt_1", "attempt_2"]
    assert attempts[0]["error_class"] == "network"
    assert attempts[0]["retry_delay_seconds"] == 0.01
    assert attempts[1]["error_class"] is None
    assert [event["event"] for event in payload["events"]].count("retry_scheduled") == 1
    assert "retry_not_before" not in payload["nodes"]["work"]


def test_concurrent_attempt_intents_and_receipts_have_one_ordered_journal(
    tmp_path: Path,
) -> None:
    directory = tmp_path / WORKFLOW_DIRECTORY
    path = directory / "concurrent.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("""
version: 1
name: concurrent
execution:
  max_parallel: 2
nodes:
  - {id: first, type: agent, prompt: first}
  - {id: second, type: agent, prompt: second}
""".lstrip(), encoding="utf-8")
    definition = load_workflow(path, tmp_path)
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets(node_attempts=2))
    barrier = __import__("threading").Barrier(2)

    def execute(invocation):
        assert state.payload["nodes"][invocation.node.id]["status"] == "running"
        barrier.wait(timeout=2)
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

    result = execute_persisted_concurrent_workflow(
        definition, {}, execute, state,
    )
    payload = store.load(state.run_id).payload
    events = payload["events"]

    assert result.status.value == "succeeded"
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    intents = [event["node_id"] for event in events if event["event"] == "attempt_intent"]
    assert intents == ["first", "second"]
    assert all(
        payload["nodes"][node_id]["attempts"][0]["receipt_at"] is not None
        for node_id in ("first", "second")
    )
