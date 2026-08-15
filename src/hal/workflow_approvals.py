"""Durable optimistic-concurrency decisions for workflow approval gates."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .workflow_schema import WorkflowNodeStatus
from .workflow_schema import WorkflowDefinition
from .workflow_artifacts import WorkflowArtifactStore
from .workflow_resume import audit_workflow_resume
from .workflow_state import (
    WorkflowRunState, _approval_review_digest, _downstream_consequences,
)


class StaleApprovalError(ValueError):
    def __init__(self, token: str) -> None:
        self.token = token
        super().__init__(
            f"approval review is stale; inspect the refreshed request and use revision {token}"
        )


@dataclass(frozen=True, slots=True)
class WorkflowApprovalDecision:
    node_id: str
    decision: str
    approver: str
    feedback: str
    revision_token: str


def pending_approval(state: WorkflowRunState, node_id: str) -> dict[str, Any]:
    matches = [
        item for item in state.payload["approvals"]
        if item["node_id"] == node_id and item["decision"] is None
    ]
    if not matches:
        raise ValueError(f"node {node_id!r} has no pending approval")
    return matches[-1]


def decide_approval(
    state: WorkflowRunState,
    decision: WorkflowApprovalDecision,
) -> None:
    if decision.decision not in {"approve", "deny", "request_changes", "cancel"}:
        raise ValueError("approval decision must be approve, deny, request_changes, or cancel")
    if not decision.approver.strip():
        raise ValueError("approval requires an authenticated approver identity")
    approval = pending_approval(state, decision.node_id)
    node = state.payload["nodes"].get(decision.node_id)
    if node is None or node["status"] != "waiting":
        raise ValueError(f"approval node {decision.node_id!r} is not waiting")
    if approval["revision_token"] != decision.revision_token:
        raise ValueError("approval revision token is stale")
    candidate = dict(approval)
    candidate["workflow_digest"] = state.payload["workflow"]["digest"]
    candidate["workspace_checkpoint"] = {
        key: state.payload["workspace"].get(key)
        for key in (
            "path", "branch", "base_head", "checkpoint_head",
            "checkpoint_dirty_digest", "checkpoint_dirty_paths",
        )
    }
    candidate["consequences"] = _downstream_consequences(
        state.payload["graph"], decision.node_id,
    )
    current_digest = _approval_review_digest(candidate)
    if current_digest != approval["review_digest"]:
        approval.update({
            "workflow_digest": candidate["workflow_digest"],
            "workspace_checkpoint": candidate["workspace_checkpoint"],
            "consequences": candidate["consequences"],
            "review_digest": current_digest,
            "revision_token": f"{state.payload['revision'] + 1}:{current_digest}",
            "requested_at": _now(), "stale": True,
        })
        node["approval_token"] = approval["revision_token"]
        state._persist(
            "approval_refreshed", node_id=decision.node_id,
            revision_token=approval["revision_token"],
        )
        raise StaleApprovalError(approval["revision_token"])
    timestamp = _now()
    approval.update({
        "decision": decision.decision, "approver": decision.approver.strip(),
        "feedback": decision.feedback, "decided_at": timestamp, "stale": False,
    })
    graph_node = next(item for item in state.payload["graph"] if item["id"] == decision.node_id)
    values = {"outcome": decision.decision, "feedback": decision.feedback}
    node["outputs"] = {
        name: values[definition["source"]]
        for name, definition in graph_node["outputs"].items()
    }
    target = {
        "approve": WorkflowNodeStatus.SUCCEEDED,
        "deny": WorkflowNodeStatus.DENIED,
        "request_changes": WorkflowNodeStatus.DENIED,
        "cancel": WorkflowNodeStatus.CANCELLED,
    }[decision.decision]
    node["status"] = target.value
    node["outcome"] = decision.decision
    node["reason"] = None if decision.decision == "approve" else f"approval {decision.decision}"
    attempt = node["attempts"][-1]
    attempt.update({
        "status": target.value, "receipt_at": timestamp,
        "outcome": decision.decision, "reason": node["reason"],
    })
    if decision.decision == "approve":
        state.payload["status"] = "interrupted"
    else:
        _skip_descendants(state, decision.node_id, node["reason"])
        state.payload["status"] = (
            "cancelled" if decision.decision == "cancel" else "denied"
        )
        state.payload["completed_at"] = timestamp
    state._persist(
        "approval_decided", node_id=decision.node_id,
        decision=decision.decision, approver=decision.approver.strip(),
    )


def authorize_approval_decision(
    state: WorkflowRunState,
    definition: WorkflowDefinition,
    artifact_store: WorkflowArtifactStore,
    decision: WorkflowApprovalDecision,
) -> None:
    """Shared CLI/TUI/remote authorization boundary for one decision."""
    audit_workflow_resume(state, definition, artifact_store)
    decide_approval(state, decision)


def _skip_descendants(state: WorkflowRunState, node_id: str, reason: str) -> None:
    blocked = {node_id}
    for graph_node in state.payload["graph"]:
        if blocked.intersection(graph_node["depends_on"]):
            blocked.add(graph_node["id"])
            node = state.payload["nodes"][graph_node["id"]]
            if node["status"] in {"pending", "ready"}:
                node["status"] = "skipped"
                node["reason"] = reason


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
