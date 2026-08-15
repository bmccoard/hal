from pathlib import Path

import pytest
from dulwich import porcelain

from hal.git import DulwichGitBackend
from hal.workflow_artifacts import WorkflowArtifactStore
from hal.workflow_budgets import WorkflowBudgets
from hal.workflow_nodes import WorkflowNodeDispatcher
from hal.workflow_policy import workflow_required_effects
from hal.workflow_publication import (
    PublicationCredentialScope, PublicationIsolationCapability,
    PushRequest, PushResult, execute_push_node,
)
from hal.workflow_runtime import (
    WorkflowNodeInvocation, WorkflowNodeRecord,
)
from hal.workflow_schema import (
    WORKFLOW_DIRECTORY, WorkflowNodeStatus, WorkflowRunStatus, load_workflow,
)
from hal.workflow_resume import resume_persisted_workflow
from hal.workflow_state import WorkflowRunStore


class FakePushAdapter:
    provider = "git"
    isolation = PublicationIsolationCapability(True, "test-isolated-push")
    remote_identities = {"origin": "https://example.test/acme/repo.git"}
    credential_scope = PublicationCredentialScope(
        "test-push-token", frozenset(remote_identities.values()),
    )

    def __init__(self) -> None:
        self.requests: list[PushRequest] = []
        self.keys: set[str] = set()
        self.pushes = 0

    def find_push(self, request: PushRequest, _cancellation) -> PushResult | None:
        self.requests.append(request)
        if request.idempotency_key not in self.keys:
            return None
        return PushResult(
            "refs/heads/" + request.branch, "already_current", request.remote_identity,
            request.branch, request.commit,
        )

    def push(self, request: PushRequest, _cancellation) -> PushResult:
        self.pushes += 1
        self.keys.add(request.idempotency_key)
        return PushResult(
            "refs/heads/" + request.branch, "pushed", request.remote_identity,
            request.branch, request.commit,
        )


def _definition(monkeypatch, tmp_path: Path):
    for name, value in {
        "GIT_AUTHOR_NAME": "HAL Test", "GIT_AUTHOR_EMAIL": "hal@example.test",
        "GIT_COMMITTER_NAME": "HAL Test", "GIT_COMMITTER_EMAIL": "hal@example.test",
    }.items():
        monkeypatch.setenv(name, value)
    root = tmp_path / "repo"
    porcelain.init(root)
    (root / "one.txt").write_text("one\n", encoding="utf-8")
    backend = DulwichGitBackend(root)
    commit = backend.commit("Initial", ["one.txt"])
    branch = backend.status().branch
    path = root / WORKFLOW_DIRECTORY / "push.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(f"""
version: 1
name: push
execution:
  workspace: worktree
nodes:
  - id: approve
    type: approval
    prompt: Approve exact push
    inputs:
      remote: {{type: string, value: origin}}
      branch: {{type: string, value: "{branch}"}}
      commit: {{type: string, value: "{commit}"}}
  - id: push
    type: publish
    depends_on: [approve]
    provider: git
    operation: push
    remote: origin
    branch: "{branch}"
    commit: "{commit}"
    approval: approve
    outputs:
      receipt: {{type: check_result, source: result}}
      provider_id: {{type: string, source: provider_id}}
""".lstrip(), encoding="utf-8")
    return root, load_workflow(path, root), commit, branch


def _invocation(definition):
    node = definition.nodes[1]
    return WorkflowNodeInvocation(node, {}, {
        "inputs": {},
        "nodes": {"approve": {
            "status": "succeeded", "outcome": "approve", "outputs": {},
        }},
    })


def test_push_verifies_exact_identity_and_has_stable_idempotency_key(
    monkeypatch, tmp_path: Path,
) -> None:
    root, definition, commit, branch = _definition(monkeypatch, tmp_path)
    adapter = FakePushAdapter()

    first = execute_push_node(
        _invocation(definition), root, adapter, git_backend="dulwich",
    )
    second = execute_push_node(
        _invocation(definition), root, adapter, git_backend="dulwich",
    )

    assert first.status is WorkflowNodeStatus.SUCCEEDED
    assert first.outcome == "pushed"
    assert second.outcome == "already_current"
    assert adapter.requests[0].idempotency_key == adapter.requests[1].idempotency_key
    assert adapter.requests[0].commit == commit
    assert adapter.requests[0].branch == branch
    assert adapter.pushes == 1
    assert first.external_receipt["provider_id"] == f"refs/heads/{branch}"
    assert first.external_receipt["credential_scope"] == "test-push-token"


def test_push_rejects_unapproved_or_unallowlisted_effect(monkeypatch, tmp_path: Path) -> None:
    root, definition, _commit, _branch = _definition(monkeypatch, tmp_path)
    adapter = FakePushAdapter()
    invocation = _invocation(definition)
    denied = WorkflowNodeInvocation(invocation.node, {}, {
        "inputs": {}, "nodes": {"approve": {"status": "denied", "outcome": "deny"}},
    })
    with pytest.raises(PermissionError, match="approved workflow gate"):
        execute_push_node(denied, root, adapter, git_backend="dulwich")
    adapter.remote_identities = {}
    with pytest.raises(PermissionError, match="not allowlisted"):
        execute_push_node(invocation, root, adapter, git_backend="dulwich")
    assert adapter.requests == []


def test_push_rejects_effect_outside_credential_scope(monkeypatch, tmp_path: Path) -> None:
    root, definition, _commit, _branch = _definition(monkeypatch, tmp_path)
    adapter = FakePushAdapter()
    adapter.credential_scope = PublicationCredentialScope(
        "wrong-repository", frozenset({"https://example.test/other/repo.git"}),
    )

    with pytest.raises(PermissionError, match="outside the adapter credential scope"):
        execute_push_node(_invocation(definition), root, adapter, git_backend="dulwich")
    assert adapter.requests == []


def test_push_external_receipt_is_durable(monkeypatch, tmp_path: Path) -> None:
    root, definition, _commit, _branch = _definition(monkeypatch, tmp_path)
    receipt = execute_push_node(
        _invocation(definition), root, FakePushAdapter(), git_backend="dulwich",
    )
    state = WorkflowRunStore(tmp_path / "runs").create(
        definition, {}, root, WorkflowBudgets(),
    )
    state.transition("push", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY)
    state.transition("push", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING)
    state.receipt("push", WorkflowNodeRecord(
        "push", receipt.status, receipt.outputs, receipt.outcome, receipt.reason,
        external_receipt=receipt.external_receipt,
    ))

    restored = state.store.load(state.run_id)
    external = restored.payload["nodes"]["push"]["attempts"][-1]["external_receipt"]
    assert external["idempotency_key"] == receipt.external_receipt["idempotency_key"]
    assert external["provider_id"] == receipt.external_receipt["provider_id"]


def test_crash_after_push_is_reconciled_without_second_push(monkeypatch, tmp_path: Path) -> None:
    root, definition, _commit, _branch = _definition(monkeypatch, tmp_path)

    class CrashAfterAcceptance(FakePushAdapter):
        def push(self, request, _cancellation):
            self.pushes += 1
            self.keys.add(request.idempotency_key)
            raise RuntimeError("simulated process crash after remote acceptance")

    adapter = CrashAfterAcceptance()
    store = WorkflowRunStore(tmp_path / "runs")
    state = store.create(definition, {}, root, WorkflowBudgets())
    state.attach_trust(
        definition.source.digest,
        tuple(sorted(effect.value for effect in workflow_required_effects(definition))),
    )
    state.transition("approve", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY)
    state.transition("approve", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING)
    state.receipt("approve", WorkflowNodeRecord(
        "approve", WorkflowNodeStatus.SUCCEEDED, outcome="approve",
    ))
    state.transition("push", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY)
    state.transition("push", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING)

    with pytest.raises(RuntimeError, match="after remote acceptance"):
        execute_push_node(
            _invocation(definition), root, adapter, git_backend="dulwich",
            record_external_intent=state.record_external_intent,
        )
    crashed = store.load(state.run_id)
    first_attempt = crashed.payload["nodes"]["push"]["attempts"][-1]
    assert first_attempt["status"] == "running"
    assert first_attempt["external_intent"]["idempotency_key"] in adapter.keys
    assert first_attempt["external_receipt"] is None

    artifact_store = WorkflowArtifactStore(tmp_path / "artifacts")
    dispatcher = WorkflowNodeDispatcher(
        root, push_adapter=adapter, artifact_store=artifact_store,
        git_backend="dulwich", external_intent=crashed.record_external_intent,
    )
    result = resume_persisted_workflow(
        crashed, definition, artifact_store, dispatcher,
    )

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert result.node("push").outcome == "already_current"
    assert adapter.pushes == 1
    persisted = store.load(state.run_id).payload["nodes"]["push"]["attempts"]
    assert len(persisted) == 2
    assert persisted[0]["status"] == "interrupted"
    assert persisted[1]["external_receipt"]["idempotency_key"] == (
        persisted[0]["external_intent"]["idempotency_key"]
    )


def test_recovery_refuses_changed_external_intent(monkeypatch, tmp_path: Path) -> None:
    root, definition, _commit, _branch = _definition(monkeypatch, tmp_path)
    state = WorkflowRunStore(tmp_path / "runs").create(
        definition, {}, root, WorkflowBudgets(),
    )
    state.transition("push", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY)
    state.transition("push", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING)
    intent = {
        "idempotency_key": "1" * 64, "provider": "git", "operation": "push",
        "remote_identity": "https://example.test/acme/repo.git",
        "branch": "main", "commit": "2" * 40,
    }
    state.record_external_intent("push", intent)
    state.recover_node("push", WorkflowNodeStatus.PENDING, "simulate restart")
    state.transition("push", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY)
    state.transition("push", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING)

    with pytest.raises(ValueError, match="changed across recovery"):
        state.record_external_intent("push", {**intent, "idempotency_key": "3" * 64})


def test_push_schema_requires_approval_to_review_exact_effect(tmp_path: Path) -> None:
    path = tmp_path / WORKFLOW_DIRECTORY / "bad.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("""
version: 1
name: bad
nodes:
  - id: approve
    type: approval
    prompt: approve
  - id: push
    type: publish
    depends_on: [approve]
    provider: git
    operation: push
    remote: origin
    branch: main
    commit: "1111111111111111111111111111111111111111"
    approval: approve
""".lstrip(), encoding="utf-8")

    with pytest.raises(ValueError, match="review the exact push remote"):
        load_workflow(path, tmp_path)


def test_publication_schema_requires_isolated_worktree(tmp_path: Path) -> None:
    path = tmp_path / WORKFLOW_DIRECTORY / "current.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("""
version: 1
name: current
nodes:
  - id: approve
    type: approval
    prompt: approve
    inputs:
      remote: {type: string, value: origin}
      branch: {type: string, value: main}
      commit: {type: string, value: "1111111111111111111111111111111111111111"}
  - id: publish
    type: publish
    depends_on: [approve]
    provider: git
    operation: push
    remote: origin
    branch: main
    commit: "1111111111111111111111111111111111111111"
    approval: approve
""".lstrip(), encoding="utf-8")

    with pytest.raises(ValueError, match="execution.workspace: worktree"):
        load_workflow(path, tmp_path)
