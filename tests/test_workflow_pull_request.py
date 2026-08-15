from pathlib import Path

import pytest
from dulwich import porcelain

from hal.git import DulwichGitBackend
from hal.workflow_artifacts import WorkflowArtifactHandle, WorkflowArtifactStore
from hal.workflow_budgets import WorkflowBudgets
from hal.workflow_publication import (
    PublicationCredentialScope, PublicationIsolationCapability,
    PullRequestRequest, PullRequestResult, execute_pull_request_node,
)
from hal.workflow_runtime import WorkflowNodeInvocation, WorkflowNodeRecord
from hal.workflow_schema import WORKFLOW_DIRECTORY, WorkflowNodeStatus, load_workflow
from hal.workflow_state import WorkflowRunStore


REMOTE = "https://example.test/acme/repo"


class FakePullRequestAdapter:
    provider = "github"
    isolation = PublicationIsolationCapability(True, "test-isolated-pr")
    remote_identities = {"origin": REMOTE}
    credential_scope = PublicationCredentialScope(
        "test-pr-token", frozenset({REMOTE}), frozenset({"pull_request"}),
    )

    def __init__(self) -> None:
        self.requests: list[PullRequestRequest] = []
        self.keys: set[str] = set()
        self.creates = 0

    def find_pull_request(self, request, _cancellation) -> PullRequestResult | None:
        self.requests.append(request)
        if request.idempotency_key not in self.keys:
            return None
        return PullRequestResult(
            "pr-42", "https://example.test/acme/repo/pulls/42", "existing",
            request.remote_identity, request.base, request.head, request.commits,
        )

    def create_pull_request(self, request, _cancellation) -> PullRequestResult:
        self.creates += 1
        self.keys.add(request.idempotency_key)
        return PullRequestResult(
            "pr-42", "https://example.test/acme/repo/pulls/42", "created",
            request.remote_identity, request.base, request.head, request.commits,
        )


def _definition(monkeypatch, tmp_path: Path):
    for name, value in {
        "GIT_AUTHOR_NAME": "HAL Test", "GIT_AUTHOR_EMAIL": "hal@example.test",
        "GIT_COMMITTER_NAME": "HAL Test", "GIT_COMMITTER_EMAIL": "hal@example.test",
    }.items():
        monkeypatch.setenv(name, value)
    root = tmp_path / "repo"
    porcelain.init(root)
    (root / "change.txt").write_text("change\n", encoding="utf-8")
    backend = DulwichGitBackend(root)
    commit = backend.commit("Change", ["change.txt"])
    head = backend.status().branch
    path = root / WORKFLOW_DIRECTORY / "pull-request.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(f"""
version: 1
name: pull-request
execution:
  workspace: worktree
nodes:
  - id: approve
    type: approval
    prompt: Approve exact PR
    inputs:
      remote: {{type: string, value: origin}}
      title: {{type: string, value: "Add change"}}
      body: {{type: markdown, value: "PR body"}}
      base: {{type: string, value: main}}
      head: {{type: string, value: "{head}"}}
      commits: {{type: json, value: ["{commit}"]}}
      checks: {{type: check_result, value: {{passed: true}}}}
      review: {{type: markdown, value: "Reviewed"}}
  - id: pr
    type: publish
    depends_on: [approve]
    provider: github
    operation: pull_request
    remote: origin
    approval: approve
    inputs:
      title: {{type: string, value: "Add change"}}
      body: {{type: markdown, value: "PR body"}}
      base: {{type: string, value: main}}
      head: {{type: string, value: "{head}"}}
      commits: {{type: json, value: ["{commit}"]}}
      checks: {{type: check_result, value: {{passed: true}}}}
      review: {{type: markdown, value: "Reviewed"}}
    outputs:
      receipt: {{type: check_result, source: result}}
      provider_id: {{type: string, source: provider_id}}
      url: {{type: string, source: url}}
""".lstrip(), encoding="utf-8")
    return root, load_workflow(path, root), commit, head


def _artifacts(tmp_path: Path):
    store = WorkflowArtifactStore(tmp_path / "artifacts")
    body = WorkflowArtifactHandle(store.put(
        "PR body", type="markdown", producer="body", media_type="text/markdown",
    ))
    checks = WorkflowArtifactHandle(store.put(
        '{"passed":true}', type="check_result", producer="checks",
        media_type="application/json",
    ))
    review = WorkflowArtifactHandle(store.put(
        "Reviewed", type="markdown", producer="review", media_type="text/markdown",
    ))
    return store, body, checks, review


def _invocation(definition, commit, head, body, checks, review):
    return WorkflowNodeInvocation(definition.nodes[1], {
        "title": "Add change", "body": body, "base": "main", "head": head,
        "commits": [commit], "checks": checks, "review": review,
    }, {
        "inputs": {},
        "nodes": {"approve": {
            "status": "succeeded", "outcome": "approve", "outputs": {},
        }},
    })


def test_pull_request_create_or_discover_is_exact_and_idempotent(
    monkeypatch, tmp_path: Path,
) -> None:
    root, definition, commit, head = _definition(monkeypatch, tmp_path)
    store, body, checks, review = _artifacts(tmp_path)
    invocation = _invocation(definition, commit, head, body, checks, review)
    adapter = FakePullRequestAdapter()

    first = execute_pull_request_node(
        invocation, root, adapter, store, git_backend="dulwich",
    )
    second = execute_pull_request_node(
        invocation, root, adapter, store, git_backend="dulwich",
    )

    assert first.status is WorkflowNodeStatus.SUCCEEDED
    assert first.outcome == "created"
    assert second.outcome == "existing"
    assert adapter.requests[0].idempotency_key == adapter.requests[1].idempotency_key
    assert adapter.requests[0].commits == (commit,)
    assert adapter.requests[0].artifact_identities == {
        "body": body.artifact.digest,
        "checks": checks.artifact.digest,
        "review": review.artifact.digest,
    }
    assert first.outputs["provider_id"] == "pr-42"
    assert first.outputs["url"].endswith("/pulls/42")
    assert adapter.creates == 1


def test_pull_request_rejects_unapproved_or_out_of_scope_effect(
    monkeypatch, tmp_path: Path,
) -> None:
    root, definition, commit, head = _definition(monkeypatch, tmp_path)
    store, body, checks, review = _artifacts(tmp_path)
    invocation = _invocation(definition, commit, head, body, checks, review)
    adapter = FakePullRequestAdapter()
    denied = WorkflowNodeInvocation(
        invocation.node, invocation.inputs,
        {"inputs": {}, "nodes": {"approve": {"status": "denied", "outcome": "deny"}}},
    )

    with pytest.raises(PermissionError, match="approved workflow gate"):
        execute_pull_request_node(denied, root, adapter, store, git_backend="dulwich")
    adapter.credential_scope = PublicationCredentialScope(
        "other", frozenset({"https://example.test/other/repo"}),
        frozenset({"pull_request"}),
    )
    with pytest.raises(PermissionError, match="outside the adapter credential scope"):
        execute_pull_request_node(invocation, root, adapter, store, git_backend="dulwich")
    assert adapter.requests == []


def test_pull_request_rejects_provider_receipt_for_different_effect(
    monkeypatch, tmp_path: Path,
) -> None:
    root, definition, commit, head = _definition(monkeypatch, tmp_path)
    store, body, checks, review = _artifacts(tmp_path)
    adapter = FakePullRequestAdapter()

    def wrong(request, _cancellation):
        return PullRequestResult(
            "pr-9", "https://example.test/acme/repo/pulls/9", "created",
            request.remote_identity, "wrong-base", request.head, request.commits,
        )

    adapter.find_pull_request = wrong
    with pytest.raises(ValueError, match="different effect"):
        execute_pull_request_node(
            _invocation(definition, commit, head, body, checks, review),
            root, adapter, store, git_backend="dulwich",
        )


def test_pull_request_rejects_failed_checks(monkeypatch, tmp_path: Path) -> None:
    root, definition, commit, head = _definition(monkeypatch, tmp_path)
    store, body, _checks, review = _artifacts(tmp_path)
    failed = WorkflowArtifactHandle(store.put(
        '{"passed":false}', type="check_result", producer="failed-checks",
        media_type="application/json",
    ))
    adapter = FakePullRequestAdapter()

    with pytest.raises(PermissionError, match="failed validation"):
        execute_pull_request_node(
            _invocation(definition, commit, head, body, failed, review),
            root, adapter, store, git_backend="dulwich",
        )
    assert adapter.requests == []

def test_pull_request_external_receipt_is_durable(monkeypatch, tmp_path: Path) -> None:
    root, definition, commit, head = _definition(monkeypatch, tmp_path)
    store, body, checks, review = _artifacts(tmp_path)
    receipt = execute_pull_request_node(
        _invocation(definition, commit, head, body, checks, review),
        root, FakePullRequestAdapter(), store, git_backend="dulwich",
    )
    state = WorkflowRunStore(tmp_path / "runs").create(
        definition, {}, root, WorkflowBudgets(),
    )
    state.transition("pr", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY)
    state.transition("pr", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING)
    state.receipt("pr", WorkflowNodeRecord(
        "pr", receipt.status, receipt.outputs, receipt.outcome, receipt.reason,
        external_receipt=receipt.external_receipt,
    ))

    external = state.store.load(state.run_id).payload["nodes"]["pr"]["attempts"][-1][
        "external_receipt"
    ]
    assert external["provider_id"] == "pr-42"
    assert external["url"].endswith("/pulls/42")
    assert external["artifact_identities"]["checks"] == checks.artifact.digest


def test_crash_after_pr_creation_is_discovered_before_second_create(
    monkeypatch, tmp_path: Path,
) -> None:
    root, definition, commit, head = _definition(monkeypatch, tmp_path)
    store, body, checks, review = _artifacts(tmp_path)
    invocation = _invocation(definition, commit, head, body, checks, review)

    class CrashAfterCreation(FakePullRequestAdapter):
        def create_pull_request(self, request, _cancellation):
            self.creates += 1
            self.keys.add(request.idempotency_key)
            raise RuntimeError("simulated crash after PR creation")

    adapter = CrashAfterCreation()
    intents = []
    journal = lambda node_id, intent: intents.append((node_id, dict(intent)))
    with pytest.raises(RuntimeError, match="after PR creation"):
        execute_pull_request_node(
            invocation, root, adapter, store, git_backend="dulwich",
            record_external_intent=journal,
        )

    recovered = execute_pull_request_node(
        invocation, root, adapter, store, git_backend="dulwich",
        record_external_intent=journal,
    )

    assert recovered.outcome == "existing"
    assert adapter.creates == 1
    assert intents[0][1]["idempotency_key"] == intents[1][1]["idempotency_key"]
    assert recovered.external_receipt["provider_id"] == "pr-42"


def test_pull_request_schema_requires_exact_approval_review(tmp_path: Path) -> None:
    path = tmp_path / WORKFLOW_DIRECTORY / "bad-pr.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("""
version: 1
name: bad-pr
execution: {workspace: worktree}
nodes:
  - id: approve
    type: approval
    prompt: approve
  - id: pr
    type: publish
    depends_on: [approve]
    provider: github
    operation: pull_request
    remote: origin
    approval: approve
    inputs:
      title: {type: string, value: Change}
      body: {type: markdown, value: Body}
      base: {type: string, value: main}
      head: {type: string, value: feature}
      commits: {type: json, value: ["1111111111111111111111111111111111111111"]}
      checks: {type: check_result, value: {passed: true}}
      review: {type: markdown, value: Reviewed}
""".lstrip(), encoding="utf-8")

    with pytest.raises(ValueError, match="review the exact PR remote"):
        load_workflow(path, tmp_path)
