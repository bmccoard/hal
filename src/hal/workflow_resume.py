"""Fail-closed audits for durable workflow restart and resume."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .workflow_artifacts import WorkflowArtifact, WorkflowArtifactHandle, WorkflowArtifactStore
from .workflow_budgets import WorkflowUsage
from .workflow_runtime import (
    NodeExecutor, WorkflowNodeRecord, WorkflowRunRecord,
    execute_concurrent_workflow, execute_serial_workflow,
)
from .workflow_schema import (
    NODE_TERMINAL_STATUSES, TRANSIENT_ERROR_CLASSES, WorkflowAttemptStatus,
    WorkflowDefinition, WorkflowNodeStatus,
)
from .workflow_policy import workflow_required_effects, workflow_requires_trust
from .workflow_state import WorkflowRunState
from .workflow_worktrees import ValidatedWorkflowWorkspace


@dataclass(frozen=True, slots=True)
class WorkflowResumeAudit:
    run_id: str
    indeterminate_nodes: tuple[str, ...]
    completed_nodes: tuple[str, ...]


def audit_workflow_resume(
    state: WorkflowRunState,
    definition: WorkflowDefinition,
    artifact_store: WorkflowArtifactStore,
    *,
    repository: Path | None = None,
    workspace: Path | None = None,
    current_head: str | None = None,
    workspace_claims: Mapping[str, ValidatedWorkflowWorkspace] | None = None,
) -> WorkflowResumeAudit:
    """Validate every pinned identity and durable object before recovery is offered."""
    payload = state.payload
    pinned = _mapping(payload.get("workflow"), "workflow identity")
    if pinned.get("name") != definition.name:
        raise ValueError("workflow run references a different workflow name")
    if pinned.get("digest") != definition.source.digest:
        raise ValueError("workflow definition digest changed; explicit migration or cancellation is required")
    if workflow_requires_trust(definition):
        trust = _mapping(payload.get("trust"), "workflow trust")
        required = {effect.value for effect in workflow_required_effects(definition)}
        if (
            trust.get("digest") != definition.source.digest
            or trust.get("repository") != str(definition.source.repository)
            or not isinstance(trust.get("effects"), list)
            or not required <= set(trust["effects"])
        ):
            raise ValueError("workflow trust is missing, stale, or insufficient")
    expected_repository = (repository or definition.source.repository).resolve()
    if Path(str(pinned.get("repository"))).resolve() != expected_repository:
        raise ValueError("workflow repository identity changed")
    stored_workspace = _mapping(payload.get("workspace"), "workspace identity")
    expected_workspace = (workspace or Path(str(stored_workspace.get("path")))).resolve()
    if Path(str(stored_workspace.get("path"))).resolve() != expected_workspace:
        raise ValueError("workflow workspace identity changed")
    if not expected_workspace.is_dir():
        raise ValueError("workflow workspace is missing")
    if current_head is not None and stored_workspace.get("head") not in {None, current_head}:
        raise ValueError("workflow workspace HEAD changed")
    _audit_workspace_claims(payload.get("workspace_claims", {}), workspace_claims)
    _audit_budgets(payload)
    _audit_lease(payload.get("lease"))
    _audit_artifacts(payload.get("artifacts"), artifact_store)
    nodes = _mapping(payload.get("nodes"), "node state")
    graph_ids = {node.id for node in definition.nodes}
    if set(nodes) != graph_ids:
        raise ValueError("persisted workflow graph does not match the pinned definition")
    indeterminate: list[str] = []
    completed: list[str] = []
    for node_id, raw in nodes.items():
        node = _mapping(raw, f"node {node_id}")
        try:
            status = WorkflowNodeStatus(str(node.get("status")))
        except ValueError as exc:
            raise ValueError(f"node {node_id!r} has unknown persisted status") from exc
        attempts = node.get("attempts")
        if not isinstance(attempts, list):
            raise ValueError(f"node {node_id!r} attempts must be a list")
        for index, attempt in enumerate(attempts, 1):
            if not isinstance(attempt, dict):
                raise ValueError(f"node {node_id!r} has malformed attempt state")
            if attempt.get("id") != f"attempt_{index}":
                raise ValueError(f"node {node_id!r} has invalid or non-sequential attempt ID")
            try:
                attempt_status = WorkflowAttemptStatus(str(attempt.get("status")))
            except ValueError as exc:
                raise ValueError(f"node {node_id!r} has unknown attempt status") from exc
            elapsed = attempt.get("elapsed_seconds", 0.0)
            if (
                isinstance(elapsed, bool)
                or not isinstance(elapsed, (int, float))
                or elapsed < 0
            ):
                raise ValueError(f"node {node_id!r} has invalid attempt elapsed time")
            error_class = attempt.get("error_class")
            if error_class is not None and error_class not in TRANSIENT_ERROR_CLASSES:
                raise ValueError(f"node {node_id!r} has unknown transient error class")
            retry_delay = attempt.get("retry_delay_seconds", 0.0)
            if (
                isinstance(retry_delay, bool)
                or not isinstance(retry_delay, (int, float))
                or retry_delay < 0
            ):
                raise ValueError(f"node {node_id!r} has invalid retry delay")
            if attempt_status is WorkflowAttemptStatus.RUNNING:
                if attempt.get("receipt_at") is not None:
                    raise ValueError(f"node {node_id!r} has a running attempt with a receipt")
            elif attempt.get("receipt_at") is None:
                raise ValueError(f"terminal node {node_id!r} is missing its completion receipt")
            external = attempt.get("external_receipt")
            intent = attempt.get("external_intent")
            if intent is not None:
                _audit_external_intent(node_id, intent)
            if external is not None and (
                not isinstance(external, dict)
                or not isinstance(external.get("idempotency_key"), str)
                or not external["idempotency_key"]
                or not isinstance(external.get("provider_id"), str)
                or not external["provider_id"]
            ):
                raise ValueError(f"node {node_id!r} has malformed external receipt")
            if (
                external is not None and intent is not None
                and external["idempotency_key"] != intent["idempotency_key"]
            ):
                raise ValueError(f"node {node_id!r} external receipt does not match its intent")
        if status is WorkflowNodeStatus.RUNNING:
            if not attempts or attempts[-1].get("status") != "running" or attempts[-1].get("receipt_at") is not None:
                raise ValueError(f"node {node_id!r} has inconsistent in-flight receipt state")
            indeterminate.append(node_id)
        elif status is WorkflowNodeStatus.READY and node.get("retry_not_before") is not None:
            _retry_delay_remaining(node["retry_not_before"])
        elif status in NODE_TERMINAL_STATUSES:
            if status not in {WorkflowNodeStatus.SKIPPED, WorkflowNodeStatus.CANCELLED} and (
                not attempts or attempts[-1].get("receipt_at") is None
            ):
                raise ValueError(f"terminal node {node_id!r} is missing its completion receipt")
            completed.append(node_id)
    return WorkflowResumeAudit(state.run_id, tuple(indeterminate), tuple(completed))


def resume_persisted_workflow(
    state: WorkflowRunState,
    definition: WorkflowDefinition,
    artifact_store: WorkflowArtifactStore,
    executor: NodeExecutor,
    *,
    retry_nodes: frozenset[str] = frozenset(),
    usage: Callable[[], WorkflowUsage] = WorkflowUsage,
    workspace_snapshot: Callable[[], Mapping[str, Any]] | None = None,
    max_parallel: int | None = None,
    workspace_claims: Mapping[str, ValidatedWorkflowWorkspace] | None = None,
) -> WorkflowRunRecord:
    """Resume audited state without rerunning successful nodes."""
    audit = audit_workflow_resume(
        state, definition, artifact_store, workspace_claims=workspace_claims,
    )
    by_id = {node.id: node for node in definition.nodes}
    unknown_retry = set(retry_nodes) - set(by_id)
    if unknown_retry:
        raise ValueError(f"unknown retry node(s): {', '.join(sorted(unknown_retry))}")
    needs_decision: list[str] = []
    for node_id in audit.indeterminate_nodes:
        node = by_id[node_id]
        if node.type == "publish":
            state.recover_node(
                node_id, WorkflowNodeStatus.PENDING,
                "idempotent publication reconciliation authorized",
            )
        elif node.resumable or node_id in retry_nodes:
            state.recover_node(node_id, WorkflowNodeStatus.PENDING, "resume authorized")
        else:
            state.recover_node(
                node_id, WorkflowNodeStatus.INTERRUPTED,
                "in-flight operation has no completion receipt; explicit retry required",
            )
            needs_decision.append(node_id)
    for node_id in retry_nodes - set(audit.indeterminate_nodes):
        raw = state.payload["nodes"][node_id]
        status = WorkflowNodeStatus(raw["status"])
        if status is WorkflowNodeStatus.SUCCEEDED:
            raise ValueError(f"successful node {node_id!r} cannot be retried")
        state.recover_node(node_id, WorkflowNodeStatus.PENDING, "explicit retry authorized")
    if needs_decision:
        raise PermissionError(
            "explicit retry is required for indeterminate node(s): "
            + ", ".join(needs_decision)
        )
    initial = _restore_terminal_records(state, artifact_store)
    def transition(node_id, current, target):
        state.update_usage(usage())
        state.transition(node_id, current, target)

    def receipt(node_id, record):
        state.update_usage(usage())
        if workspace_snapshot is not None:
            state.update_workspace_checkpoint(**workspace_snapshot())
        state.receipt(node_id, record)

    def loop_continue(node_id, record):
        state.update_usage(usage())
        if workspace_snapshot is not None:
            state.update_workspace_checkpoint(**workspace_snapshot())
        state.continue_loop(node_id, record)

    initial_states = {
        node_id: WorkflowNodeStatus(raw["status"])
        for node_id, raw in state.payload["nodes"].items()
        if WorkflowNodeStatus(raw["status"]) not in NODE_TERMINAL_STATUSES
        and WorkflowNodeStatus(raw["status"]) is not WorkflowNodeStatus.WAITING
    }
    attempt_counts = {
        node_id: len(raw.get("attempts", []))
        for node_id, raw in state.payload["nodes"].items()
    }
    loop_elapsed = {
        node_id: sum(
            float(attempt.get("elapsed_seconds", 0.0))
            for attempt in raw.get("attempts", [])
        )
        for node_id, raw in state.payload["nodes"].items()
    }
    retry_counts = {
        node_id: _consecutive_retry_count(raw)
        for node_id, raw in state.payload["nodes"].items()
    }
    retry_delays = {
        node_id: _retry_delay_remaining(raw["retry_not_before"])
        for node_id, raw in state.payload["nodes"].items()
        if raw.get("retry_not_before") is not None
    }

    scheduler = (
        execute_concurrent_workflow
        if workspace_claims or (max_parallel is not None and max_parallel > 1)
        else execute_serial_workflow
    )
    scheduler_options = {
        "on_transition": transition, "on_receipt": receipt,
        "initial_records": initial, "on_loop_continue": loop_continue,
        "initial_states": initial_states,
        "initial_attempt_counts": attempt_counts,
        "initial_loop_elapsed": loop_elapsed,
        "initial_retry_counts": retry_counts,
        "initial_retry_delays": retry_delays,
    }
    if scheduler is execute_concurrent_workflow:
        scheduler_options["max_parallel"] = max_parallel
        scheduler_options["workspace_claims"] = workspace_claims
    result = scheduler(
        definition, state.payload["inputs"], executor, **scheduler_options,
    )
    if workspace_snapshot is not None:
        state.update_workspace_checkpoint(**workspace_snapshot())
    state.finish(result, usage())
    return result


def _audit_workspace_claims(
    stored: Any,
    claims: Mapping[str, ValidatedWorkflowWorkspace] | None,
) -> None:
    pinned = _mapping(stored, "workspace claims")
    supplied = {
        node_id: {
            "path": str(claim.path),
            "repository": str(claim.repository),
            "branch": claim.branch,
        }
        for node_id, claim in (claims or {}).items()
    }
    if pinned != supplied:
        raise ValueError("validated workflow workspace claims are missing or changed")


def _restore_terminal_records(
    state: WorkflowRunState, artifact_store: WorkflowArtifactStore,
) -> Mapping[str, WorkflowNodeRecord]:
    records: dict[str, WorkflowNodeRecord] = {}
    artifacts = state.payload["artifacts"]
    for node_id, raw in state.payload["nodes"].items():
        status = WorkflowNodeStatus(raw["status"])
        if status not in NODE_TERMINAL_STATUSES and status is not WorkflowNodeStatus.WAITING:
            continue
        outputs = {}
        for name, value in raw.get("outputs", {}).items():
            if isinstance(value, dict) and set(value) == {"artifact"}:
                metadata = artifacts[value["artifact"]]
                artifact = WorkflowArtifact(
                    metadata["type"], metadata["producer"], metadata["digest"],
                    metadata["size"], metadata["media_type"], location=metadata["location"],
                )
                artifact_store.validate(artifact)
                outputs[name] = WorkflowArtifactHandle(artifact)
            else:
                outputs[name] = value
        records[node_id] = WorkflowNodeRecord(
            node_id, status, MappingProxyType(outputs), raw.get("outcome"), raw.get("reason"),
            external_receipt=(
                raw["attempts"][-1].get("external_receipt")
                if raw.get("attempts") else None
            ),
        )
    return MappingProxyType(records)


def _audit_budgets(payload: dict[str, Any]) -> None:
    budgets = _mapping(payload.get("budgets"), "workflow budgets")
    usage = _mapping(payload.get("usage"), "workflow usage")
    for name, used in usage.items():
        if isinstance(used, bool) or not isinstance(used, (int, float)) or used < 0:
            raise ValueError(f"workflow usage {name!r} is invalid")
        limit = budgets.get(name)
        if limit is not None and used > limit:
            raise ValueError(f"workflow usage exceeds persisted {name} budget")


def _consecutive_retry_count(node: Mapping[str, Any]) -> int:
    attempts = node.get("attempts", [])
    reset_at = node.get("retry_reset_at", 0)
    if isinstance(reset_at, bool) or not isinstance(reset_at, int) or not 0 <= reset_at <= len(attempts):
        raise ValueError("workflow node has invalid retry reset position")
    count = 0
    for attempt in reversed(attempts[reset_at:]):
        if attempt.get("error_class") is None:
            break
        count += 1
    return count


def _retry_delay_remaining(value: Any) -> float:
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("workflow node has invalid retry wake time") from exc
    if timestamp.tzinfo is None:
        raise ValueError("workflow node retry wake time must include a timezone")
    return max(0.0, (timestamp - datetime.now(timezone.utc)).total_seconds())


def _audit_external_intent(node_id: str, value: Any) -> None:
    intent = _mapping(value, f"node {node_id} external intent")
    if (
        not isinstance(intent.get("idempotency_key"), str)
        or len(intent["idempotency_key"]) != 64
        or any(character not in "0123456789abcdef" for character in intent["idempotency_key"])
        or intent.get("operation") not in {"push", "pull_request"}
        or not isinstance(intent.get("provider"), str) or not intent["provider"]
        or not isinstance(intent.get("remote_identity"), str)
        or not intent["remote_identity"]
    ):
        raise ValueError(f"node {node_id!r} has malformed external intent")


def _audit_lease(value: Any) -> None:
    if value is None:
        return
    lease = _mapping(value, "worker lease")
    if not isinstance(lease.get("owner"), str) or not lease["owner"]:
        raise ValueError("worker lease owner is invalid")
    try:
        expires = datetime.fromisoformat(str(lease["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise ValueError("worker lease expiry is invalid") from exc
    if expires > datetime.now(timezone.utc):
        raise ValueError(f"workflow run is actively leased by {lease['owner']!r}")


def _audit_artifacts(value: Any, store: WorkflowArtifactStore) -> None:
    artifacts = _mapping(value, "artifact metadata")
    for artifact_id, raw in artifacts.items():
        item = _mapping(raw, f"artifact {artifact_id}")
        artifact = WorkflowArtifact(
            str(item.get("type")), str(item.get("producer")), str(item.get("digest")),
            item.get("size"), str(item.get("media_type")),
            location=item.get("location"),
        )
        if artifact.digest != artifact_id:
            raise ValueError("artifact identity does not match its digest")
        store.validate(artifact)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"persisted {name} must be an object")
    return value
