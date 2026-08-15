"""Fail-closed publication isolation policy for repository workflows."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

from .cancellation import CancellationToken, cancellation_or_default
from .git import create_git_backend
from .workflow_artifacts import WorkflowArtifactHandle, WorkflowArtifactStore
from .workflow_expressions import render_workflow_template
from .workflow_runtime import WorkflowNodeInvocation, WorkflowNodeReceipt
from .workflow_schema import WorkflowDefinition, WorkflowNodeStatus


@dataclass(frozen=True, slots=True)
class PublicationIsolationCapability:
    """A trusted host assertion; it contains no credential material."""

    enforceable: bool
    boundary: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PublicationCredentialScope:
    """Non-secret description of authority retained inside an adapter."""

    credential_id: str
    remote_identities: frozenset[str]
    operations: frozenset[str] = frozenset({"push"})


@dataclass(frozen=True, slots=True)
class PushRequest:
    workspace: Path
    remote: str
    remote_identity: str
    branch: str
    commit: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PushResult:
    provider_id: str
    outcome: str
    remote_identity: str
    branch: str
    commit: str


@dataclass(frozen=True, slots=True)
class PullRequestRequest:
    workspace: Path
    remote: str
    remote_identity: str
    title: str
    body: str
    base: str
    head: str
    commits: tuple[str, ...]
    checks: Mapping[str, Any]
    review: str
    artifact_identities: Mapping[str, str]
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PullRequestResult:
    provider_id: str
    url: str
    outcome: str
    remote_identity: str
    base: str
    head: str
    commits: tuple[str, ...]


class PushAdapter(Protocol):
    """Credential-owning push boundary supplied by a trusted host integration."""

    provider: str
    isolation: PublicationIsolationCapability
    credential_scope: PublicationCredentialScope
    remote_identities: Mapping[str, str]

    def find_push(
        self, request: PushRequest, cancellation: CancellationToken,
    ) -> PushResult | None: ...

    def push(
        self, request: PushRequest, cancellation: CancellationToken,
    ) -> PushResult: ...


class PullRequestAdapter(Protocol):
    """Credential-owning create-or-discover boundary for pull requests."""

    provider: str
    isolation: PublicationIsolationCapability
    credential_scope: PublicationCredentialScope
    remote_identities: Mapping[str, str]

    def find_pull_request(
        self, request: PullRequestRequest, cancellation: CancellationToken,
    ) -> PullRequestResult | None: ...

    def create_pull_request(
        self, request: PullRequestRequest, cancellation: CancellationToken,
    ) -> PullRequestResult: ...


def publication_isolation(
    definition: WorkflowDefinition, adapter: PushAdapter | None = None,
) -> dict[str, Any]:
    """Report whether this host can isolate publication authority from other nodes.

    HAL currently executes commands and model-backed tools as host processes. Empty
    environment allow-lists prevent incidental credential inheritance, but do not
    provide a network namespace or an OS credential boundary. Publication therefore
    remains unavailable until an isolation provider is configured and implemented.
    """
    required = any(node.type == "publish" for node in definition.nodes)
    ordinary = [
        node.id for node in definition.nodes if node.type in {"agent", "command"}
    ]
    if not required:
        return {"required": False, "enforceable": True, "ordinary_nodes": ordinary}
    if adapter is not None and adapter.isolation.enforceable:
        return {
            "required": True, "enforceable": True, "ordinary_nodes": ordinary,
            "boundary": adapter.isolation.boundary,
        }
    return {
        "required": True,
        "enforceable": False,
        "ordinary_nodes": ordinary,
        "reason": (
            "this host runner has no enforceable network and publication-credential "
            "isolation boundary for ordinary agent and command nodes"
        ),
    }


def require_publication_isolation(
    definition: WorkflowDefinition, adapter: PushAdapter | None = None,
) -> None:
    report = publication_isolation(definition, adapter)
    if report["required"] and not report["enforceable"]:
        raise PermissionError(
            f"publication workflow {definition.name!r} cannot run: {report['reason']}"
        )


def execute_push_node(
    invocation: WorkflowNodeInvocation,
    workspace: Path,
    adapter: PushAdapter | None,
    cancellation: CancellationToken | None = None,
    *,
    git_backend: str = "auto",
    record_external_intent: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> WorkflowNodeReceipt:
    """Verify and submit one exact, approved, idempotent branch push."""
    if invocation.node.config.get("operation") != "push":
        raise NotImplementedError(
            f"publication operation {invocation.node.config.get('operation')!r} is unavailable"
        )
    if adapter is None:
        raise PermissionError("push requires a trusted scoped publication adapter")
    if not adapter.isolation.enforceable:
        raise PermissionError(adapter.isolation.reason or "push adapter isolation is not enforceable")
    config = invocation.node.config
    if adapter.provider != str(config["provider"]):
        raise PermissionError("push provider does not match the scoped adapter")
    approval_id = str(config["approval"])
    approval = invocation.context.get("nodes", {}).get(approval_id, {})
    if approval.get("status") != "succeeded" or approval.get("outcome") != "approve":
        raise PermissionError("push requires a current approved workflow gate")

    remote = _render_string(config["remote"], invocation, "remote")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
        raise ValueError("push remote must resolve to an allowlisted remote name")
    try:
        remote_identity = adapter.remote_identities[remote]
    except KeyError as exc:
        raise PermissionError(f"push remote {remote!r} is not allowlisted") from exc
    if not isinstance(remote_identity, str) or not remote_identity.strip():
        raise ValueError("push adapter returned an invalid remote identity")
    scope = adapter.credential_scope
    if not scope.credential_id.strip():
        raise ValueError("push adapter credential scope has no safe identity")
    if "push" not in scope.operations or remote_identity not in scope.remote_identities:
        raise PermissionError("push is outside the adapter credential scope")
    branch = _render_string(config["branch"], invocation, "branch")
    if not re.fullmatch(
        r"(?!.*(?:\.\.|@\{|//))[A-Za-z0-9][A-Za-z0-9._/-]*", branch,
    ) or branch.endswith(("/", ".", ".lock")):
        raise ValueError("push branch did not resolve to a safe branch name")
    commit = _render_string(config["commit"], invocation, "commit").lower()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise ValueError("push commit must resolve to a full Git object ID")

    cancellation = cancellation_or_default(cancellation)
    backend = create_git_backend(workspace, git_backend)
    status = backend.status(cancellation)
    history = backend.log(1, cancellation)
    head = history[0].commit if history else None
    if status.branch != branch:
        raise ValueError(f"push branch mismatch: expected {branch!r}, found {status.branch!r}")
    if head != commit:
        raise ValueError(f"push commit mismatch: expected {commit}, found {head or 'no HEAD'}")

    identity = {
        "provider": adapter.provider, "remote": remote_identity,
        "branch": branch, "commit": commit,
    }
    idempotency_key = sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    request = PushRequest(
        workspace.resolve(), remote, remote_identity, branch, commit, idempotency_key,
    )
    intent = MappingProxyType({
        "idempotency_key": idempotency_key, "provider": adapter.provider,
        "operation": "push", "remote_identity": remote_identity,
        "branch": branch, "commit": commit,
    })
    if record_external_intent is not None:
        record_external_intent(invocation.node.id, intent)
    result = adapter.find_push(request, cancellation)
    reconciled = result is not None
    if result is None:
        result = adapter.push(request, cancellation)
    if (
        result.remote_identity != remote_identity or result.branch != branch
        or result.commit != commit or result.outcome not in {"pushed", "already_current"}
        or (reconciled and result.outcome != "already_current")
        or (not reconciled and result.outcome != "pushed")
        or not result.provider_id.strip()
    ):
        raise ValueError("push adapter returned a receipt for a different effect")
    receipt = {
        **identity, "remote_name": remote, "provider_id": result.provider_id,
        "outcome": result.outcome, "idempotency_key": idempotency_key,
        "credential_scope": scope.credential_id,
    }
    sources = {
        "result": receipt,
        "provider_id": result.provider_id,
        "url": remote_identity,
    }
    outputs = {
        name: sources[output.source]
        for name, output in invocation.node.outputs.items()
    }
    external = MappingProxyType({
        "idempotency_key": idempotency_key,
        "provider_id": result.provider_id,
        "remote_identity": remote_identity,
        "branch": branch,
        "commit": commit,
        "outcome": result.outcome,
        "credential_scope": scope.credential_id,
    })
    return WorkflowNodeReceipt(
        WorkflowNodeStatus.SUCCEEDED, MappingProxyType(outputs),
        outcome=result.outcome, external_receipt=external,
    )


def execute_pull_request_node(
    invocation: WorkflowNodeInvocation,
    workspace: Path,
    adapter: PullRequestAdapter | None,
    artifact_store: WorkflowArtifactStore,
    cancellation: CancellationToken | None = None,
    *,
    git_backend: str = "auto",
    record_external_intent: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> WorkflowNodeReceipt:
    """Create or discover one exact, approved pull request through a trusted adapter."""
    if invocation.node.config.get("operation") != "pull_request":
        raise NotImplementedError(
            f"publication operation {invocation.node.config.get('operation')!r} is unavailable"
        )
    if adapter is None:
        raise PermissionError("pull request requires a trusted scoped publication adapter")
    if not adapter.isolation.enforceable:
        raise PermissionError(
            adapter.isolation.reason or "pull-request adapter isolation is not enforceable"
        )
    config = invocation.node.config
    if adapter.provider != str(config["provider"]):
        raise PermissionError("pull-request provider does not match the scoped adapter")
    approval_id = str(config["approval"])
    approval = invocation.context.get("nodes", {}).get(approval_id, {})
    if approval.get("status") != "succeeded" or approval.get("outcome") != "approve":
        raise PermissionError("pull request requires a current approved workflow gate")

    remote = _render_string(config["remote"], invocation, "remote")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
        raise ValueError("pull-request remote must resolve to an allowlisted remote name")
    try:
        remote_identity = adapter.remote_identities[remote]
    except KeyError as exc:
        raise PermissionError(f"pull-request remote {remote!r} is not allowlisted") from exc
    scope = adapter.credential_scope
    if not scope.credential_id.strip():
        raise ValueError("pull-request adapter credential scope has no safe identity")
    if (
        "pull_request" not in scope.operations
        or remote_identity not in scope.remote_identities
    ):
        raise PermissionError("pull request is outside the adapter credential scope")

    title = _typed_string(invocation.inputs["title"], "title", 512)
    body, body_id = _typed_artifact_text(
        invocation.inputs["body"], "markdown", "body", artifact_store,
    )
    base = _safe_branch(_typed_string(invocation.inputs["base"], "base", 255), "base")
    head = _safe_branch(_typed_string(invocation.inputs["head"], "head", 255), "head")
    commits = _commit_list(invocation.inputs["commits"])
    checks, checks_id = _typed_check_result(
        invocation.inputs["checks"], artifact_store,
    )
    review, review_id = _typed_artifact_text(
        invocation.inputs["review"], "markdown", "review", artifact_store,
    )

    cancellation = cancellation_or_default(cancellation)
    backend = create_git_backend(workspace, git_backend)
    status = backend.status(cancellation)
    history = backend.log(len(commits), cancellation)
    current_head = history[0].commit if history else None
    if status.branch != head:
        raise ValueError(f"pull-request head mismatch: expected {head!r}, found {status.branch!r}")
    if current_head != commits[-1]:
        raise ValueError(
            f"pull-request commit mismatch: expected {commits[-1]}, found {current_head or 'no HEAD'}"
        )
    actual_commits = tuple(item.commit for item in reversed(history))
    if actual_commits != commits:
        raise ValueError("pull-request commits do not match the repository commit sequence")

    artifact_identities = MappingProxyType({
        "body": body_id, "checks": checks_id, "review": review_id,
    })
    identity = {
        "provider": adapter.provider, "remote": remote_identity,
        "title": title, "body": body_id, "base": base, "head": head,
        "commits": commits, "checks": checks_id, "review": review_id,
    }
    idempotency_key = sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    request = PullRequestRequest(
        workspace.resolve(), remote, remote_identity, title, body, base, head,
        commits, MappingProxyType(dict(checks)), review, artifact_identities,
        idempotency_key,
    )
    intent = MappingProxyType({
        "idempotency_key": idempotency_key, "provider": adapter.provider,
        "operation": "pull_request", "remote_identity": remote_identity,
        "base": base, "head": head, "commits": list(commits),
        "artifact_identities": dict(artifact_identities),
    })
    if record_external_intent is not None:
        record_external_intent(invocation.node.id, intent)
    result = adapter.find_pull_request(request, cancellation)
    reconciled = result is not None
    if result is None:
        result = adapter.create_pull_request(request, cancellation)
    parsed_url = urlparse(result.url)
    if (
        result.remote_identity != remote_identity or result.base != base
        or result.head != head or tuple(result.commits) != commits
        or result.outcome not in {"created", "existing"}
        or (reconciled and result.outcome != "existing")
        or (not reconciled and result.outcome != "created")
        or not result.provider_id.strip()
        or parsed_url.scheme != "https" or not parsed_url.netloc
    ):
        raise ValueError("pull-request adapter returned a receipt for a different effect")
    receipt = {
        "provider": adapter.provider, "provider_id": result.provider_id,
        "url": result.url, "outcome": result.outcome,
        "remote_name": remote, "remote": remote_identity,
        "base": base, "head": head, "commits": list(commits),
        "artifact_identities": dict(artifact_identities),
        "idempotency_key": idempotency_key,
        "credential_scope": scope.credential_id,
    }
    sources = {
        "result": receipt, "provider_id": result.provider_id, "url": result.url,
    }
    outputs = {
        name: sources[output.source]
        for name, output in invocation.node.outputs.items()
    }
    external = MappingProxyType({
        "idempotency_key": idempotency_key, "provider_id": result.provider_id,
        "url": result.url, "remote_identity": remote_identity,
        "base": base, "head": head, "commits": list(commits),
        "artifact_identities": dict(artifact_identities),
        "outcome": result.outcome, "credential_scope": scope.credential_id,
    })
    return WorkflowNodeReceipt(
        WorkflowNodeStatus.SUCCEEDED, MappingProxyType(outputs),
        outcome=result.outcome, external_receipt=external,
    )


def _render_string(raw: Any, invocation: WorkflowNodeInvocation, field: str) -> str:
    value = render_workflow_template(str(raw), invocation.context)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"push {field} must resolve to a non-empty string")
    return value.strip()


def _typed_string(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"pull-request {field} must be a non-empty string")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"pull-request {field} exceeds {limit} characters")
    return value


def _safe_branch(value: str, field: str) -> str:
    if not re.fullmatch(
        r"(?!.*(?:\.\.|@\{|//))[A-Za-z0-9][A-Za-z0-9._/-]*", value,
    ) or value.endswith(("/", ".", ".lock")):
        raise ValueError(f"pull-request {field} is not a safe branch name")
    return value


def _commit_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("pull-request commits must be a non-empty JSON list")
    commits = tuple(str(item).lower() for item in value)
    if any(not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", item) for item in commits):
        raise ValueError("pull-request commits must contain full Git object IDs")
    if len(set(commits)) != len(commits):
        raise ValueError("pull-request commits must not contain duplicates")
    return commits


def _typed_artifact_text(
    value: Any, type_name: str, field: str, store: WorkflowArtifactStore,
) -> tuple[str, str]:
    if isinstance(value, WorkflowArtifactHandle):
        if value.artifact.type != type_name:
            raise ValueError(f"pull-request {field} artifact has the wrong type")
        data = store.read(value.artifact)
        identity = value.artifact.digest
    elif isinstance(value, str):
        data = value.encode("utf-8")
        identity = sha256(data).hexdigest()
    else:
        raise ValueError(f"pull-request {field} must be typed {type_name}")
    if len(data) > 1024 * 1024:
        raise ValueError(f"pull-request {field} exceeds 1 MiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"pull-request {field} must be UTF-8") from exc
    if field == "review" and not text.strip():
        raise ValueError("pull-request review must not be empty")
    return text, identity


def _typed_check_result(
    value: Any, store: WorkflowArtifactStore,
) -> tuple[Mapping[str, Any], str]:
    if isinstance(value, WorkflowArtifactHandle):
        if value.artifact.type != "check_result":
            raise ValueError("pull-request checks artifact has the wrong type")
        data = store.read(value.artifact)
        identity = value.artifact.digest
        if len(data) > 1024 * 1024:
            raise ValueError("pull-request checks exceed 1 MiB")
        try:
            decoded = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("pull-request checks artifact is invalid JSON") from exc
    else:
        decoded = value
        encoded = json.dumps(
            decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        identity = sha256(encoded).hexdigest()
    if not isinstance(decoded, Mapping):
        raise ValueError("pull-request checks must be a typed check result")
    if (
        decoded.get("passed") is False
        or (
            isinstance(decoded.get("exit_code"), int)
            and not isinstance(decoded.get("exit_code"), bool)
            and decoded["exit_code"] != 0
        )
        or str(decoded.get("status", "")).lower() in {
            "failed", "failure", "error", "timed_out", "cancelled",
        }
    ):
        raise PermissionError("pull-request checks report a failed validation")
    return decoded, identity
