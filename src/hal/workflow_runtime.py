"""Deterministic, side-effect-agnostic serial workflow scheduling."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .cancellation import CancelledError
from .workflow_budgets import WorkflowBudgetExhaustedError
from .workflow_expressions import evaluate_workflow_condition
from .workflow_expressions import render_workflow_template
from .workflow_schema import (
    NODE_TERMINAL_STATUSES, WorkflowDefinition, WorkflowNodeDefinition,
    WorkflowNodeStatus, WorkflowRunStatus, require_status_transition,
)


@dataclass(frozen=True, slots=True)
class WorkflowNodeReceipt:
    """Sanitized result returned by a trusted node implementation."""

    status: WorkflowNodeStatus
    outputs: Mapping[str, Any] = MappingProxyType({})
    outcome: str | None = None
    reason: str | None = None
    approval: Mapping[str, Any] | None = None
    external_receipt: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if (
            self.status not in NODE_TERMINAL_STATUSES
            and self.status is not WorkflowNodeStatus.WAITING
        ) or self.status is WorkflowNodeStatus.SKIPPED:
            raise ValueError("node executor must return waiting or a terminal, non-skipped status")


@dataclass(frozen=True, slots=True)
class WorkflowNodeRecord:
    node_id: str
    status: WorkflowNodeStatus
    outputs: Mapping[str, Any] = MappingProxyType({})
    outcome: str | None = None
    reason: str | None = None
    approval: Mapping[str, Any] | None = None
    external_receipt: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class WorkflowRunRecord:
    status: WorkflowRunStatus
    nodes: tuple[WorkflowNodeRecord, ...]

    def node(self, node_id: str) -> WorkflowNodeRecord:
        for item in self.nodes:
            if item.node_id == node_id:
                return item
        raise KeyError(node_id)


@dataclass(frozen=True, slots=True)
class WorkflowNodeInvocation:
    node: WorkflowNodeDefinition
    inputs: Mapping[str, Any]
    context: Mapping[str, Any]


NodeExecutor = Callable[[WorkflowNodeInvocation], WorkflowNodeReceipt]
TransitionObserver = Callable[[str, WorkflowNodeStatus, WorkflowNodeStatus], None]
ReceiptObserver = Callable[[str, WorkflowNodeRecord], None]


def execute_serial_workflow(
    definition: WorkflowDefinition,
    inputs: Mapping[str, Any],
    executor: NodeExecutor,
    on_transition: TransitionObserver | None = None,
    on_receipt: ReceiptObserver | None = None,
    initial_records: Mapping[str, WorkflowNodeRecord] | None = None,
) -> WorkflowRunRecord:
    """Execute ready nodes in definition order using a trusted node dispatcher.

    This scheduler knows graph and state semantics only. It never invokes a model,
    command, network service, or filesystem operation itself.
    """
    resolved_workflow_inputs = materialize_workflow_inputs(definition, inputs)
    seeded = dict(initial_records or {})
    unknown_seeded = set(seeded) - {node.id for node in definition.nodes}
    if unknown_seeded:
        raise ValueError(f"resume records contain unknown node(s): {', '.join(sorted(unknown_seeded))}")
    states = {
        node.id: seeded[node.id].status if node.id in seeded else WorkflowNodeStatus.PENDING
        for node in definition.nodes
    }
    records: dict[str, WorkflowNodeRecord] = seeded

    def transition(node_id: str, target: WorkflowNodeStatus) -> None:
        current = states[node_id]
        require_status_transition(current, target)
        states[node_id] = target
        if on_transition is not None:
            on_transition(node_id, current, target)

    while any(status not in NODE_TERMINAL_STATUSES for status in states.values()):
        progressed = False
        for node in definition.nodes:
            if states[node.id] is not WorkflowNodeStatus.PENDING:
                continue
            dependency_states = [states[item] for item in node.depends_on]
            if any(status not in NODE_TERMINAL_STATUSES for status in dependency_states):
                continue
            if node.dependency_policy == "all_succeeded" and any(
                status is not WorkflowNodeStatus.SUCCEEDED for status in dependency_states
            ):
                transition(node.id, WorkflowNodeStatus.SKIPPED)
                records[node.id] = WorkflowNodeRecord(
                    node.id, WorkflowNodeStatus.SKIPPED,
                    reason="dependency did not succeed",
                )
                if on_receipt is not None:
                    on_receipt(node.id, records[node.id])
                progressed = True
                continue
            if node.condition and not evaluate_workflow_condition(
                node.condition, _condition_context(resolved_workflow_inputs, records),
            ):
                transition(node.id, WorkflowNodeStatus.SKIPPED)
                records[node.id] = WorkflowNodeRecord(
                    node.id, WorkflowNodeStatus.SKIPPED, reason="condition was false",
                )
                if on_receipt is not None:
                    on_receipt(node.id, records[node.id])
                progressed = True
                continue
            transition(node.id, WorkflowNodeStatus.READY)
            transition(node.id, WorkflowNodeStatus.RUNNING)
            try:
                context = _condition_context(resolved_workflow_inputs, records)
                node_inputs = {
                    name: (
                        render_workflow_template(item.value, context)
                        if isinstance(item.value, str)
                        and ("${{" in item.value or "}}" in item.value)
                        else item.value
                    )
                    for name, item in node.inputs.items()
                }
                receipt = executor(WorkflowNodeInvocation(
                    node, MappingProxyType(node_inputs), context,
                ))
            except CancelledError as exc:
                receipt = WorkflowNodeReceipt(
                    WorkflowNodeStatus.CANCELLED, reason=str(exc),
                )
            except WorkflowBudgetExhaustedError as exc:
                receipt = WorkflowNodeReceipt(
                    WorkflowNodeStatus.BUDGET_EXHAUSTED, reason=str(exc),
                )
            except Exception as exc:  # trusted dispatch boundary; preserve scheduler state
                receipt = WorkflowNodeReceipt(
                    WorkflowNodeStatus.FAILED,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            transition(node.id, receipt.status)
            records[node.id] = WorkflowNodeRecord(
                node.id, receipt.status, MappingProxyType(dict(receipt.outputs)),
                receipt.outcome, receipt.reason, receipt.approval,
                receipt.external_receipt,
            )
            if on_receipt is not None:
                on_receipt(node.id, records[node.id])
            progressed = True
        if not progressed:
            if any(status is WorkflowNodeStatus.WAITING for status in states.values()):
                ordered = tuple(
                    records.get(node.id, WorkflowNodeRecord(node.id, states[node.id]))
                    for node in definition.nodes
                )
                return WorkflowRunRecord(WorkflowRunStatus.WAITING, ordered)
            # A validated DAG cannot reach this state. Fail closed if a custom
            # definition or future state extension violates that invariant.
            raise RuntimeError("workflow scheduler made no progress")

    ordered = tuple(records[node.id] for node in definition.nodes)
    failed = any(
        item.status not in {WorkflowNodeStatus.SUCCEEDED, WorkflowNodeStatus.SKIPPED}
        for item in ordered
    )
    # A dependency-caused skip represents propagated failure; a condition skip does not.
    failed = failed or any(item.reason == "dependency did not succeed" for item in ordered)
    if any(item.status is WorkflowNodeStatus.CANCELLED for item in ordered):
        run_status = WorkflowRunStatus.CANCELLED
    elif any(item.status is WorkflowNodeStatus.BUDGET_EXHAUSTED for item in ordered):
        run_status = WorkflowRunStatus.BUDGET_EXHAUSTED
    else:
        run_status = WorkflowRunStatus.FAILED if failed else WorkflowRunStatus.SUCCEEDED
    return WorkflowRunRecord(run_status, ordered)


def _condition_context(
    inputs: Mapping[str, Any], records: Mapping[str, WorkflowNodeRecord],
) -> Mapping[str, Any]:
    return {
        "inputs": dict(inputs),
        "nodes": {
            node_id: {
                "status": record.status.value,
                "outcome": record.outcome or record.status.value,
                "outputs": dict(record.outputs),
            }
            for node_id, record in records.items()
        },
        "workflow": {"status": WorkflowRunStatus.RUNNING.value},
    }


def materialize_workflow_inputs(
    definition: WorkflowDefinition, supplied: Mapping[str, Any],
) -> Mapping[str, Any]:
    unknown = set(supplied) - set(definition.inputs)
    if unknown:
        raise ValueError(f"unknown workflow input(s): {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    for name, item in definition.inputs.items():
        if name in supplied:
            value = supplied[name]
        elif item.has_default:
            value = item.default
        elif item.required:
            raise ValueError(f"missing required workflow input {name!r}")
        else:
            value = None
        if value is not None and not _runtime_value_matches(value, item.type):
            raise ValueError(f"workflow input {name!r} does not match type {item.type!r}")
        values[name] = value
    return MappingProxyType(values)


def _runtime_value_matches(value: Any, type_name: str) -> bool:
    return {
        "string": isinstance(value, str),
        "markdown": isinstance(value, str),
        "path": isinstance(value, str),
        "diff": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "json": isinstance(value, (str, int, float, bool, list, tuple, Mapping)) or value is None,
        "check_result": isinstance(value, Mapping),
    }[type_name]
