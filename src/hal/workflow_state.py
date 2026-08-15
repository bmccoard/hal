"""Atomic durable state and write-ahead receipts for workflow runs."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Any, Callable, Mapping

from .workflow_artifacts import WorkflowArtifactHandle
from .workflow_budgets import WorkflowBudgets, WorkflowUsage
from .workflow_runtime import (
    NodeExecutor, WorkflowNodeRecord, WorkflowRunRecord, execute_serial_workflow,
    materialize_workflow_inputs,
)
from .workflow_schema import WorkflowDefinition, WorkflowNodeStatus


WORKFLOW_RUN_RECORD_VERSION = 1
_RUN_ID = re.compile(r"wfrun_[0-9a-f]{16}\Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class WorkflowRunStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()

    def create(
        self,
        definition: WorkflowDefinition,
        inputs: Mapping[str, Any],
        workspace: Path,
        budgets: WorkflowBudgets,
        *,
        branch: str | None = None,
        head: str | None = None,
    ) -> "WorkflowRunState":
        run_id = f"wfrun_{secrets.token_hex(8)}"
        timestamp = _now()
        payload: dict[str, Any] = {
            "version": WORKFLOW_RUN_RECORD_VERSION,
            "revision": 0,
            "run_id": run_id,
            "workflow": {
                "name": definition.name,
                "schema_version": definition.version,
                "origin": "repository",
                "digest": definition.source.digest,
                "repository": str(definition.source.repository),
                "definition_path": definition.source.relative_path,
                "inputs": {
                    name: {
                        "type": item.type, "required": item.required,
                        "has_default": item.has_default,
                        "default": _json_safe(item.default) if item.has_default else None,
                    }
                    for name, item in definition.inputs.items()
                },
            },
            "status": "pending",
            "inputs": _json_safe(materialize_workflow_inputs(definition, inputs)),
            "graph": [
                {
                    "id": node.id,
                    "type": node.type,
                    "depends_on": list(node.depends_on),
                    "dependency_policy": node.dependency_policy,
                    "condition": node.condition,
                    "effects": sorted(effect.value for effect in node.effects),
                    "outputs": {
                        name: {"type": item.type, "source": item.source}
                        for name, item in node.outputs.items()
                    },
                    "resumable": node.resumable,
                    "idempotent": node.idempotent,
                }
                for node in definition.nodes
            ],
            "budgets": asdict(budgets),
            "usage": asdict(WorkflowUsage()),
            "workspace": {
                "path": str(workspace.resolve()),
                "branch": branch,
                "head": head,
            },
            "nodes": {
                node.id: {"status": "pending", "attempts": [], "outcome": None}
                for node in definition.nodes
            },
            "artifacts": {},
            "approvals": [],
            "trust": None,
            "lease": None,
            "events": [],
            "created_at": timestamp,
            "updated_at": timestamp,
            "completed_at": None,
        }
        state = WorkflowRunState(self, payload)
        state._persist("run_created", status="pending")
        return state

    def load(self, run_id: str) -> "WorkflowRunState":
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError(f"invalid workflow run id {run_id!r}")
        path = self.directory / f"{run_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"workflow run record is corrupt: {path.name}") from exc
        if not isinstance(payload, dict):
            raise ValueError("workflow run record must be an object")
        if payload.get("version") != WORKFLOW_RUN_RECORD_VERSION:
            raise ValueError(f"unsupported workflow run record version {payload.get('version')!r}")
        if payload.get("run_id") != run_id:
            raise ValueError("workflow run record ID mismatch")
        return WorkflowRunState(self, payload)

    def list(self) -> tuple["WorkflowRunState", ...]:
        if not self.directory.is_dir():
            return ()
        states = []
        for path in sorted(self.directory.glob("wfrun_*.json")):
            states.append(self.load(path.stem))
        return tuple(sorted(
            states, key=lambda state: str(state.payload.get("updated_at", "")), reverse=True,
        ))

    def archive(self, run_id: str) -> Path:
        state = self.load(run_id)
        if state.payload.get("status") not in {
            "succeeded", "failed", "denied", "cancelled", "timed_out",
            "budget_exhausted", "interrupted",
        }:
            raise ValueError("only terminal workflow runs can be archived")
        source = self.directory / f"{run_id}.json"
        target_directory = self.directory / "archive"
        target_directory.mkdir(parents=True, exist_ok=True)
        target = target_directory / source.name
        if target.exists():
            raise ValueError(f"workflow run {run_id!r} is already archived")
        os.replace(source, target)
        return target

    def _write(self, payload: Mapping[str, Any]) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{payload['run_id']}.json"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return path


class WorkflowRunState:
    def __init__(self, store: WorkflowRunStore, payload: dict[str, Any]) -> None:
        self.store = store
        self.payload = payload

    @property
    def run_id(self) -> str:
        return str(self.payload["run_id"])

    def transition(
        self, node_id: str, current: WorkflowNodeStatus, target: WorkflowNodeStatus,
    ) -> None:
        node = self.payload["nodes"][node_id]
        if node["status"] != current.value:
            raise ValueError(f"persisted node {node_id!r} has stale status {node['status']!r}")
        # Terminal state is committed with its completion receipt, never before it.
        if target.value in {
            "succeeded", "failed", "skipped", "denied", "cancelled", "timed_out",
            "budget_exhausted", "interrupted",
        }:
            return
        node["status"] = target.value
        if target is WorkflowNodeStatus.RUNNING:
            attempt = {
                "id": f"attempt_{len(node['attempts']) + 1}",
                "status": "running",
                "intent_at": _now(),
                "receipt_at": None,
                "outcome": None,
                "reason": None,
                "external_intent": None,
                "external_receipt": None,
            }
            node["attempts"].append(attempt)
            self.payload["status"] = "running"
            self._persist("attempt_intent", node_id=node_id, attempt_id=attempt["id"])
        else:
            self._persist("node_transition", node_id=node_id, status=target.value)

    def receipt(self, node_id: str, record: WorkflowNodeRecord) -> None:
        node = self.payload["nodes"][node_id]
        node["status"] = record.status.value
        node["outcome"] = record.outcome
        node["reason"] = record.reason
        if record.approval is not None:
            review = _json_safe(record.approval)
            review["node_id"] = node_id
            review["workflow_digest"] = self.payload["workflow"]["digest"]
            review["workspace_checkpoint"] = {
                key: self.payload["workspace"].get(key)
                for key in (
                    "path", "branch", "base_head", "checkpoint_head",
                    "checkpoint_dirty_digest", "checkpoint_dirty_paths",
                )
            }
            review["consequences"] = _downstream_consequences(self.payload["graph"], node_id)
            review_digest = _approval_review_digest(review)
            review["review_digest"] = review_digest
            token = f"{self.payload['revision'] + 1}:{review_digest}"
            review.update({
                "revision_token": token,
                "requested_at": _now(), "decision": None, "approver": None,
                "feedback": None, "decided_at": None, "stale": False,
            })
            self.payload["approvals"].append(review)
            node["approval_token"] = token
        if node["attempts"]:
            attempt = node["attempts"][-1]
            attempt.update({
                "status": record.status.value,
                "receipt_at": _now(),
                "outcome": record.outcome,
                "reason": record.reason,
            })
            if record.external_receipt is not None:
                attempt["external_receipt"] = _json_safe(record.external_receipt)
        artifact_ids = []
        for name, value in record.outputs.items():
            if isinstance(value, WorkflowArtifactHandle):
                artifact = value.artifact
                artifact_id = artifact.digest
                self.payload["artifacts"][artifact_id] = {
                    "type": artifact.type,
                    "producer": artifact.producer,
                    "digest": artifact.digest,
                    "size": artifact.size,
                    "media_type": artifact.media_type,
                    "location": artifact.location,
                }
                artifact_ids.append({"output": name, "artifact": artifact_id})
        node["artifacts"] = artifact_ids
        node["outputs"] = {
            name: (
                {"artifact": value.artifact.digest}
                if isinstance(value, WorkflowArtifactHandle) else _json_safe(value)
            )
            for name, value in record.outputs.items()
        }
        self._persist(
            "approval_waiting" if record.status is WorkflowNodeStatus.WAITING else "attempt_receipt",
            node_id=node_id, status=record.status.value,
        )

    def record_external_intent(self, node_id: str, intent: Mapping[str, Any]) -> None:
        """Durably journal a sanitized idempotent effect before adapter invocation."""
        node = self.payload["nodes"].get(node_id)
        if node is None or node.get("status") != "running" or not node.get("attempts"):
            raise ValueError(f"external intent requires running node {node_id!r}")
        attempt = node["attempts"][-1]
        if attempt.get("status") != "running" or attempt.get("receipt_at") is not None:
            raise ValueError(f"external intent requires an active attempt for {node_id!r}")
        value = _json_safe(intent)
        if not isinstance(value, dict):
            raise ValueError("external intent must be an object")
        allowed = {
            "idempotency_key", "provider", "operation", "remote_identity",
            "branch", "commit", "base", "head", "commits", "artifact_identities",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(
                "external intent contains unsafe field(s): " + ", ".join(sorted(unknown))
            )
        if (
            not re.fullmatch(r"[0-9a-f]{64}", str(value.get("idempotency_key", "")))
            or value.get("operation") not in {"push", "pull_request"}
            or not isinstance(value.get("provider"), str) or not value["provider"]
            or not isinstance(value.get("remote_identity"), str)
            or not value["remote_identity"]
        ):
            raise ValueError("external intent identity is malformed")
        previous = next((
            item.get("external_intent") for item in reversed(node["attempts"][:-1])
            if item.get("external_intent") is not None
        ), None)
        current = attempt.get("external_intent")
        pinned = current if current is not None else previous
        if pinned is not None and pinned != value:
            raise ValueError("external intent changed across recovery; refusing publication")
        if current == value:
            return
        attempt["external_intent"] = value
        self._persist(
            "external_intent", node_id=node_id,
            attempt_id=attempt["id"], idempotency_key=value["idempotency_key"],
            provider=value["provider"], operation=value["operation"],
        )

    def finish(self, result: WorkflowRunRecord, usage: WorkflowUsage) -> None:
        self.payload["status"] = result.status.value
        self.payload["usage"] = asdict(usage)
        if result.status.value == "waiting":
            self.payload["completed_at"] = None
            self._persist("run_waiting", status=result.status.value)
        else:
            self.payload["completed_at"] = _now()
            self._persist("run_completed", status=result.status.value)

    def update_usage(self, usage: WorkflowUsage) -> None:
        """Stage aggregate counters for the next atomic transition write."""
        self.payload["usage"] = asdict(usage)

    def recover_node(self, node_id: str, target: WorkflowNodeStatus, reason: str) -> None:
        node = self.payload["nodes"][node_id]
        node["status"] = target.value
        node["outcome"] = None
        node["reason"] = reason
        if node["attempts"] and node["attempts"][-1]["status"] == "running":
            node["attempts"][-1]["status"] = "interrupted"
            node["attempts"][-1]["reason"] = reason
            node["attempts"][-1]["receipt_at"] = _now()
        self.payload["status"] = "interrupted"
        self._persist("node_recovery", node_id=node_id, status=target.value, reason=reason)

    def request_cancel(self) -> None:
        if self.payload.get("status") in {
            "succeeded", "failed", "denied", "cancelled", "timed_out",
            "budget_exhausted",
        }:
            raise ValueError("terminal workflow run cannot be cancelled")
        timestamp = _now()
        self.payload["cancellation_requested_at"] = timestamp
        running = False
        for node in self.payload["nodes"].values():
            if node["status"] == "running":
                running = True
            elif node["status"] in {"pending", "ready"}:
                node["status"] = "cancelled"
                node["reason"] = "run cancellation requested"
        if not running:
            self.payload["status"] = "cancelled"
            self.payload["completed_at"] = timestamp
        self._persist("cancellation_requested", status=self.payload["status"])

    def attach_workspace(
        self, path: Path, *, branch: str, head: str,
        source_branch: str, source_dirty_paths: tuple[str, ...],
    ) -> None:
        if any(node["status"] != "pending" for node in self.payload["nodes"].values()):
            raise ValueError("workspace identity must be attached before node execution")
        self.payload["workspace"] = {
            "path": str(path.resolve()), "branch": branch, "head": head,
            "base_head": head, "checkpoint_head": head,
            "source_branch": source_branch,
            "source_dirty_paths": list(source_dirty_paths),
        }
        self._persist("workspace_attached", branch=branch, head=head)

    def attach_trust(self, digest: str, effects: tuple[str, ...]) -> None:
        if self.payload["workflow"]["digest"] != digest:
            raise ValueError("trust digest does not match the workflow definition")
        self.payload["trust"] = {
            "repository": self.payload["workflow"]["repository"],
            "digest": digest, "effects": list(effects), "granted_at": _now(),
        }
        self._persist("trust_granted", digest=digest, effects=list(effects))

    def mark_workspace_cleaned(self) -> None:
        self.payload["workspace"]["cleaned_at"] = _now()
        self._persist("workspace_cleaned")

    def update_workspace_checkpoint(
        self, *, head: str, branch: str, dirty_digest: str,
        dirty_paths: tuple[str, ...],
    ) -> None:
        workspace = self.payload["workspace"]
        workspace.update({
            "checkpoint_head": head, "branch": branch,
            "checkpoint_dirty_digest": dirty_digest,
            "checkpoint_dirty_paths": list(dirty_paths),
        })

    def commit_migration(
        self, workflow: dict[str, Any], graph: list[dict[str, Any]],
        new_nodes: tuple[str, ...], actor: str, reason: str,
    ) -> None:
        previous = dict(self.payload["workflow"])
        self.payload.setdefault("migrations", []).append({
            "from_digest": previous["digest"],
            "to_digest": workflow["digest"],
            "actor": actor,
            "reason": reason,
            "timestamp": _now(),
        })
        self.payload["workflow"] = workflow
        self.payload["trust"] = None
        self.payload["graph"] = graph
        for node_id in new_nodes:
            self.payload["nodes"][node_id] = {
                "status": "pending", "attempts": [], "outcome": None,
            }
        self._persist(
            "definition_migrated", from_digest=previous["digest"],
            to_digest=workflow["digest"], actor=actor,
        )

    def _persist(self, event: str, **fields: Any) -> None:
        self.payload["revision"] += 1
        timestamp = _now()
        self.payload["updated_at"] = timestamp
        self.payload["events"].append({
            "sequence": len(self.payload["events"]) + 1,
            "timestamp": timestamp,
            "event": event,
            **fields,
        })
        self.store._write(self.payload)


def execute_persisted_workflow(
    definition: WorkflowDefinition,
    inputs: Mapping[str, Any],
    executor: NodeExecutor,
    state: WorkflowRunState,
    usage: Callable[[], WorkflowUsage] = WorkflowUsage,
    workspace_snapshot: Callable[[], Mapping[str, Any]] | None = None,
) -> WorkflowRunRecord:
    """Run with intent persisted before dispatch and receipt persisted afterward."""
    def transition(node_id, current, target):
        state.update_usage(usage())
        state.transition(node_id, current, target)

    def receipt(node_id, record):
        state.update_usage(usage())
        if workspace_snapshot is not None:
            state.update_workspace_checkpoint(**workspace_snapshot())
        state.receipt(node_id, record)

    result = execute_serial_workflow(
        definition, inputs, executor,
        on_transition=transition,
        on_receipt=receipt,
    )
    if workspace_snapshot is not None:
        state.update_workspace_checkpoint(**workspace_snapshot())
    state.finish(result, usage())
    return result


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        value = {str(key): _json_safe(item) for key, item in value.items()}
    elif isinstance(value, (list, tuple)):
        value = [_json_safe(item) for item in value]
    encoded = json.dumps(value, allow_nan=False)
    return json.loads(encoded)


def _approval_review_digest(review: Mapping[str, Any]) -> str:
    excluded = {
        "review_digest", "revision_token", "requested_at", "decision", "approver",
        "feedback", "decided_at", "stale",
    }
    payload = {key: value for key, value in review.items() if key not in excluded}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _downstream_consequences(graph: list[dict[str, Any]], node_id: str) -> list[dict[str, Any]]:
    descendants = {node_id}
    consequences = []
    for node in graph:
        if node["id"] == node_id:
            continue
        if descendants.intersection(node["depends_on"]):
            descendants.add(node["id"])
            consequences.append({
                "id": node["id"], "type": node["type"], "effects": node["effects"],
            })
    return consequences
