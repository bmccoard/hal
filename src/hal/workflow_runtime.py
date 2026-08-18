"""Deterministic, side-effect-agnostic serial workflow scheduling."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import time
import threading
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .cancellation import CancelledError
from .workflow_budgets import WorkflowBudgetExhaustedError
from .workflow_artifacts import WorkflowArtifactHandle
from .workflow_expressions import evaluate_workflow_condition
from .workflow_expressions import render_workflow_template
from .workflow_expressions import WorkflowExpressionError
from .workflow_schema import (
    MAX_WORKFLOW_PARALLELISM, NODE_TERMINAL_STATUSES,
    WorkflowDefinition, WorkflowNodeDefinition,
    TRANSIENT_ERROR_CLASSES, WorkflowEffect, WorkflowNodeStatus, WorkflowRunStatus,
    require_status_transition,
)
from .workflow_worktrees import ValidatedWorkflowWorkspace


MAX_WORKFLOW_WORKERS_PER_RUN = MAX_WORKFLOW_PARALLELISM
MAX_WORKFLOW_WORKERS_GLOBAL = 16
DEFAULT_WORKFLOW_OUTPUT_BUFFER_LIMIT = 8 * 1024 * 1024
_SHARED_WORKFLOW_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_WORKFLOW_WORKERS_GLOBAL, thread_name_prefix="hal-workflow",
)


class WorkflowTransientError(RuntimeError):
    """Trusted signal that an idempotent node attempt may be retried safely."""

    def __init__(self, error_class: str, message: str) -> None:
        if error_class not in TRANSIENT_ERROR_CLASSES:
            raise ValueError(f"unknown transient workflow error class {error_class!r}")
        self.error_class = error_class
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class WorkflowNodeReceipt:
    """Sanitized result returned by a trusted node implementation."""

    status: WorkflowNodeStatus
    outputs: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    outcome: str | None = None
    reason: str | None = None
    approval: Mapping[str, Any] | None = None
    external_receipt: Mapping[str, Any] | None = None
    error_class: str | None = None

    def __post_init__(self) -> None:
        if (
            self.status not in NODE_TERMINAL_STATUSES
            and self.status is not WorkflowNodeStatus.WAITING
        ) or self.status is WorkflowNodeStatus.SKIPPED:
            raise ValueError("node executor must return waiting or a terminal, non-skipped status")
        if self.error_class is not None and (
            self.status is not WorkflowNodeStatus.FAILED
            or self.error_class not in TRANSIENT_ERROR_CLASSES
        ):
            raise ValueError(
                "transient error_class requires a failed receipt and a registered class"
            )


@dataclass(frozen=True, slots=True)
class WorkflowNodeRecord:
    node_id: str
    status: WorkflowNodeStatus
    outputs: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    outcome: str | None = None
    reason: str | None = None
    approval: Mapping[str, Any] | None = None
    external_receipt: Mapping[str, Any] | None = None
    attempt_status: WorkflowNodeStatus | None = None
    attempt_elapsed_seconds: float = 0.0
    attempt_outcome: str | None = None
    attempt_reason: str | None = None
    error_class: str | None = None
    retry_delay_seconds: float = 0.0
    record_attempt: bool = True


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
    attempt_id: str = "attempt_1"
    attempt_index: int = 1
    timeout_seconds: float | None = None
    retry_index: int = 0
    workspace: Path | None = None


NodeExecutor = Callable[[WorkflowNodeInvocation], WorkflowNodeReceipt]
TransitionObserver = Callable[[str, WorkflowNodeStatus, WorkflowNodeStatus], None]
ReceiptObserver = Callable[[str, WorkflowNodeRecord], None]
LoopContinuationObserver = Callable[[str, WorkflowNodeRecord], None]


def execute_serial_workflow(
    definition: WorkflowDefinition,
    inputs: Mapping[str, Any],
    executor: NodeExecutor,
    on_transition: TransitionObserver | None = None,
    on_receipt: ReceiptObserver | None = None,
    initial_records: Mapping[str, WorkflowNodeRecord] | None = None,
    on_loop_continue: LoopContinuationObserver | None = None,
    initial_states: Mapping[str, WorkflowNodeStatus] | None = None,
    initial_attempt_counts: Mapping[str, int] | None = None,
    initial_loop_elapsed: Mapping[str, float] | None = None,
    initial_retry_counts: Mapping[str, int] | None = None,
    initial_retry_delays: Mapping[str, float] | None = None,
    retry_wait: Callable[[float], None] | None = None,
    context_records: Mapping[str, WorkflowNodeRecord] | None = None,
    invocation_workspace: Path | None = None,
    max_buffered_output_bytes: int | None = DEFAULT_WORKFLOW_OUTPUT_BUFFER_LIMIT,
    monotonic: Callable[[], float] = time.monotonic,
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
    seeded_states = dict(initial_states or {})
    unknown_states = set(seeded_states) - {node.id for node in definition.nodes}
    if unknown_states:
        raise ValueError(f"resume states contain unknown node(s): {', '.join(sorted(unknown_states))}")
    states = {
        node.id: (
            seeded[node.id].status if node.id in seeded
            else seeded_states.get(node.id, WorkflowNodeStatus.PENDING)
        )
        for node in definition.nodes
    }
    records: dict[str, WorkflowNodeRecord] = dict(context_records or {})
    records.update(seeded)
    buffered_by_node = {
        node_id: _workflow_output_size(record.outputs)
        for node_id, record in records.items()
    }
    buffered_output_bytes = sum(buffered_by_node.values())
    if max_buffered_output_bytes is not None and (
        isinstance(max_buffered_output_bytes, bool)
        or not isinstance(max_buffered_output_bytes, int)
        or max_buffered_output_bytes <= 0
    ):
        raise ValueError("workflow output buffer limit must be a positive integer or null")
    if (
        max_buffered_output_bytes is not None
        and buffered_output_bytes > max_buffered_output_bytes
    ):
        raise ValueError("restored workflow outputs exceed the configured buffer limit")
    attempt_counts = dict(initial_attempt_counts or {})
    loop_elapsed = dict(initial_loop_elapsed or {})
    retry_counts = dict(initial_retry_counts or {})
    retry_delays = dict(initial_retry_delays or {})
    wait_for_retry = retry_wait or getattr(executor, "wait_for_retry", time.sleep)

    def transition(node_id: str, target: WorkflowNodeStatus) -> None:
        current = states[node_id]
        require_status_transition(current, target)
        states[node_id] = target
        if on_transition is not None:
            on_transition(node_id, current, target)

    while any(status not in NODE_TERMINAL_STATUSES for status in states.values()):
        progressed = False
        for node in definition.nodes:
            if states[node.id] not in {WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY}:
                continue
            if states[node.id] is WorkflowNodeStatus.PENDING:
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

            while states[node.id] is WorkflowNodeStatus.READY:
                delay = retry_delays.pop(node.id, 0.0)
                if delay > 0:
                    try:
                        wait_for_retry(delay)
                    except CancelledError as exc:
                        transition(node.id, WorkflowNodeStatus.CANCELLED)
                        records[node.id] = WorkflowNodeRecord(
                            node.id, WorkflowNodeStatus.CANCELLED,
                            reason=str(exc), record_attempt=False,
                        )
                        if on_receipt is not None:
                            on_receipt(node.id, records[node.id])
                        break
                    except WorkflowBudgetExhaustedError as exc:
                        transition(node.id, WorkflowNodeStatus.BUDGET_EXHAUSTED)
                        records[node.id] = WorkflowNodeRecord(
                            node.id, WorkflowNodeStatus.BUDGET_EXHAUSTED,
                            reason=str(exc), record_attempt=False,
                        )
                        if on_receipt is not None:
                            on_receipt(node.id, records[node.id])
                        break
                attempt_index = attempt_counts.get(node.id, 0) + 1
                attempt_id = f"attempt_{attempt_index}"
                retry_index = retry_counts.get(node.id, 0)
                loop = node.config.get("loop") if node.type == "agent" else None
                timeout = float(loop["timeout_seconds"]) if loop and "timeout_seconds" in loop else None
                elapsed_before = loop_elapsed.get(node.id, 0.0)
                if timeout is not None and elapsed_before >= timeout:
                    transition(node.id, WorkflowNodeStatus.TIMED_OUT)
                    records[node.id] = WorkflowNodeRecord(
                        node.id, WorkflowNodeStatus.TIMED_OUT,
                        reason=f"loop timed out after {timeout:g} seconds",
                    )
                    if on_receipt is not None:
                        on_receipt(node.id, records[node.id])
                    break
                transition(node.id, WorkflowNodeStatus.RUNNING)
                started = monotonic()
                try:
                    context = _condition_context(
                        resolved_workflow_inputs, records,
                        attempt_index=attempt_index, attempt_id=attempt_id,
                    )
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
                        attempt_id, attempt_index,
                        None if timeout is None else max(0.0, timeout - elapsed_before),
                        retry_index,
                        invocation_workspace,
                    ))
                except CancelledError as exc:
                    receipt = WorkflowNodeReceipt(
                        WorkflowNodeStatus.CANCELLED, reason=str(exc),
                    )
                except WorkflowBudgetExhaustedError as exc:
                    receipt = WorkflowNodeReceipt(
                        WorkflowNodeStatus.BUDGET_EXHAUSTED, reason=str(exc),
                    )
                except WorkflowTransientError as exc:
                    receipt = WorkflowNodeReceipt(
                        WorkflowNodeStatus.FAILED, reason=str(exc),
                        error_class=exc.error_class,
                    )
                except Exception as exc:  # trusted dispatch boundary; preserve scheduler state
                    receipt = WorkflowNodeReceipt(
                        WorkflowNodeStatus.FAILED,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                output_size = _workflow_output_size(receipt.outputs)
                previous_size = buffered_by_node.get(node.id, 0)
                projected = buffered_output_bytes - previous_size + output_size
                if (
                    max_buffered_output_bytes is not None
                    and projected > max_buffered_output_bytes
                ):
                    receipt = WorkflowNodeReceipt(
                        (
                            WorkflowNodeStatus.FAILED
                            if receipt.status is WorkflowNodeStatus.SUCCEEDED
                            else receipt.status
                        ),
                        reason=(
                            "workflow output buffer exceeds "
                            f"{max_buffered_output_bytes} bytes"
                        ),
                    )
                    output_size = 0
                    projected = buffered_output_bytes - previous_size
                buffered_by_node[node.id] = output_size
                buffered_output_bytes = projected
                attempt_elapsed = max(0.0, monotonic() - started)
                attempt_counts[node.id] = attempt_index
                loop_elapsed[node.id] = elapsed_before + attempt_elapsed

                should_continue = False
                exhausted_reason: str | None = None
                retry = node.config.get("retry")
                if (
                    retry and receipt.status is WorkflowNodeStatus.FAILED
                    and receipt.error_class in set(retry["error_classes"])
                    and retry_index + 1 < int(retry["max_attempts"])
                ):
                    delay = min(
                        float(retry.get("max_backoff_seconds", 30.0)),
                        float(retry.get("initial_backoff_seconds", 0.5))
                        * float(retry.get("multiplier", 2.0)) ** retry_index,
                    )
                    attempt_record = WorkflowNodeRecord(
                        node.id, receipt.status, MappingProxyType(dict(receipt.outputs)),
                        receipt.outcome, receipt.reason, receipt.approval,
                        receipt.external_receipt, receipt.status, attempt_elapsed,
                        receipt.outcome, receipt.reason, receipt.error_class, delay,
                    )
                    require_status_transition(
                        WorkflowNodeStatus.RUNNING, WorkflowNodeStatus.READY,
                    )
                    states[node.id] = WorkflowNodeStatus.READY
                    retry_counts[node.id] = retry_index + 1
                    retry_delays[node.id] = delay
                    if on_loop_continue is not None:
                        on_loop_continue(node.id, attempt_record)
                    continue
                if loop and receipt.status not in {
                    WorkflowNodeStatus.WAITING, WorkflowNodeStatus.DENIED,
                    WorkflowNodeStatus.CANCELLED, WorkflowNodeStatus.BUDGET_EXHAUSTED,
                }:
                    attempt_record = WorkflowNodeRecord(
                        node.id, receipt.status, MappingProxyType(dict(receipt.outputs)),
                        receipt.outcome, receipt.reason, receipt.approval,
                        receipt.external_receipt, receipt.status, attempt_elapsed,
                    )
                    loop_context = _condition_context(
                        resolved_workflow_inputs, records,
                        current_node=attempt_record, attempt_index=attempt_index,
                        attempt_id=attempt_id,
                    )
                    try:
                        satisfied = evaluate_workflow_condition(
                            str(loop["until"]), loop_context,
                        )
                    except WorkflowExpressionError as exc:
                        receipt = WorkflowNodeReceipt(
                            WorkflowNodeStatus.FAILED,
                            MappingProxyType(dict(receipt.outputs)),
                            outcome="loop_expression_failed",
                            reason=f"loop condition evaluation failed: {exc}",
                        )
                    else:
                        max_attempts = loop.get("max_attempts")
                        hit_attempt_bound = (
                            max_attempts is not None
                            and attempt_index >= int(max_attempts)
                        )
                        hit_time_bound = (
                            timeout is not None and loop_elapsed[node.id] >= timeout
                        )
                        should_continue = (
                            not satisfied and not hit_attempt_bound and not hit_time_bound
                        )
                        if not satisfied and not should_continue:
                            exhausted_reason = (
                                f"loop timed out after {timeout:g} seconds"
                                if hit_time_bound
                                else f"loop condition was not satisfied after {attempt_index} attempt(s)"
                            )
                        if should_continue:
                            require_status_transition(
                                WorkflowNodeStatus.RUNNING, WorkflowNodeStatus.READY,
                            )
                            states[node.id] = WorkflowNodeStatus.READY
                            retry_counts[node.id] = 0
                            if on_loop_continue is not None:
                                on_loop_continue(node.id, attempt_record)
                            continue

                final_status = receipt.status
                final_outcome = receipt.outcome
                final_reason = receipt.reason
                if exhausted_reason is not None:
                    final_status = (
                        WorkflowNodeStatus.TIMED_OUT
                        if timeout is not None and loop_elapsed[node.id] >= timeout
                        else WorkflowNodeStatus.FAILED
                    )
                    final_outcome = "loop_exhausted"
                    final_reason = exhausted_reason
                transition(node.id, final_status)
                records[node.id] = WorkflowNodeRecord(
                    node.id, final_status, MappingProxyType(dict(receipt.outputs)),
                    final_outcome, final_reason, receipt.approval,
                    receipt.external_receipt, receipt.status, attempt_elapsed,
                    receipt.outcome, receipt.reason, receipt.error_class,
                )
                if on_receipt is not None:
                    on_receipt(node.id, records[node.id])
                break
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
    return _workflow_result(ordered)


def execute_concurrent_workflow(
    definition: WorkflowDefinition,
    inputs: Mapping[str, Any],
    executor: NodeExecutor,
    *,
    max_parallel: int | None = None,
    on_transition: TransitionObserver | None = None,
    on_receipt: ReceiptObserver | None = None,
    on_loop_continue: LoopContinuationObserver | None = None,
    initial_records: Mapping[str, WorkflowNodeRecord] | None = None,
    initial_states: Mapping[str, WorkflowNodeStatus] | None = None,
    initial_attempt_counts: Mapping[str, int] | None = None,
    initial_loop_elapsed: Mapping[str, float] | None = None,
    initial_retry_counts: Mapping[str, int] | None = None,
    initial_retry_delays: Mapping[str, float] | None = None,
    retry_wait: Callable[[float], None] | None = None,
    workspace_claims: Mapping[str, ValidatedWorkflowWorkspace] | None = None,
    max_buffered_output_bytes: int = DEFAULT_WORKFLOW_OUTPUT_BUFFER_LIMIT,
    monotonic: Callable[[], float] = time.monotonic,
) -> WorkflowRunRecord:
    """Execute independent ready nodes concurrently with stable graph ordering."""
    requested_limit = definition.execution.max_parallel if max_parallel is None else max_parallel
    if isinstance(requested_limit, bool) or not isinstance(requested_limit, int) or requested_limit <= 0:
        raise ValueError("workflow max_parallel must be a positive integer")
    limit = min(requested_limit, MAX_WORKFLOW_WORKERS_PER_RUN)
    if (
        isinstance(max_buffered_output_bytes, bool)
        or not isinstance(max_buffered_output_bytes, int)
        or max_buffered_output_bytes <= 0
    ):
        raise ValueError("workflow output buffer limit must be a positive integer")
    node_ids = {node.id for node in definition.nodes}
    claims = dict(workspace_claims or {})
    unknown_claims = set(claims) - node_ids
    if unknown_claims:
        raise ValueError(
            f"workspace claims contain unknown node(s): {', '.join(sorted(unknown_claims))}"
        )
    if any(not isinstance(claim, ValidatedWorkflowWorkspace) for claim in claims.values()):
        raise TypeError("workspace claims must be host-validated workspace objects")
    if limit == 1 and not claims:
        return execute_serial_workflow(
            definition, inputs, executor,
            on_transition=on_transition, on_receipt=on_receipt,
            initial_records=initial_records, on_loop_continue=on_loop_continue,
            initial_states=initial_states,
            initial_attempt_counts=initial_attempt_counts,
            initial_loop_elapsed=initial_loop_elapsed,
            initial_retry_counts=initial_retry_counts,
            initial_retry_delays=initial_retry_delays,
            retry_wait=retry_wait,
            max_buffered_output_bytes=max_buffered_output_bytes,
            monotonic=monotonic,
        )

    resolved_inputs = materialize_workflow_inputs(definition, inputs)
    by_id = {node.id: node for node in definition.nodes}
    order = {node.id: index for index, node in enumerate(definition.nodes)}
    seeded = dict(initial_records or {})
    unknown = set(seeded) - set(by_id)
    if unknown:
        raise ValueError(f"resume records contain unknown node(s): {', '.join(sorted(unknown))}")
    seeded_states = dict(initial_states or {})
    unknown_states = set(seeded_states) - set(by_id)
    if unknown_states:
        raise ValueError(f"resume states contain unknown node(s): {', '.join(sorted(unknown_states))}")
    states = {
        node.id: (
            seeded[node.id].status if node.id in seeded
            else seeded_states.get(node.id, WorkflowNodeStatus.PENDING)
        )
        for node in definition.nodes
    }
    records = dict(seeded)
    output_lock = threading.Lock()
    buffered_by_node = {
        node_id: _workflow_output_size(record.outputs)
        for node_id, record in records.items()
    }
    buffered_output_bytes = sum(buffered_by_node.values())
    if buffered_output_bytes > max_buffered_output_bytes:
        raise ValueError("restored workflow outputs exceed the configured buffer limit")
    active: dict[Future[WorkflowRunRecord], str] = {}
    callback_lock = threading.RLock()

    def transition(node_id: str, current: WorkflowNodeStatus, target: WorkflowNodeStatus) -> None:
        if on_transition is not None:
            with callback_lock:
                on_transition(node_id, current, target)

    def receipt(node_id: str, record: WorkflowNodeRecord) -> None:
        if on_receipt is not None:
            with callback_lock:
                on_receipt(node_id, record)

    def loop_continue(node_id: str, record: WorkflowNodeRecord) -> None:
        if on_loop_continue is not None:
            with callback_lock:
                on_loop_continue(node_id, record)

    def terminal_without_dispatch(
        node_id: str, status: WorkflowNodeStatus, reason: str,
    ) -> None:
        current = states[node_id]
        require_status_transition(current, status)
        transition(node_id, current, status)
        states[node_id] = status
        record = WorkflowNodeRecord(node_id, status, reason=reason)
        records[node_id] = record
        receipt(node_id, record)

    def mutates_workspace(node: WorkflowNodeDefinition) -> bool:
        if node.effects & {
            WorkflowEffect.WORKSPACE_MUTATION, WorkflowEffect.GIT_MUTATION,
            WorkflowEffect.PUBLICATION,
        }:
            return True
        return node.type == "agent" and str(
            node.config.get("capability") or "inspect"
        ).lower() not in {"inspect", "plan"}

    def workspace_conflict(
        left: WorkflowNodeDefinition, right: WorkflowNodeDefinition,
    ) -> bool:
        if not (mutates_workspace(left) or mutates_workspace(right)):
            return False
        left_claim, right_claim = claims.get(left.id), claims.get(right.id)
        if left_claim is None or right_claim is None:
            return True
        left_path, right_path = left_claim.path, right_claim.path
        return (
            left_path == right_path
            or left_path in right_path.parents
            or right_path in left_path.parents
        )

    def run_node(
        node: WorkflowNodeDefinition,
        context_snapshot: Mapping[str, WorkflowNodeRecord],
        start_status: WorkflowNodeStatus,
        gate: threading.Event,
        next_gate: threading.Event,
    ) -> WorkflowRunRecord:
        gate.wait()
        released = False

        def node_transition(
            node_id: str, current: WorkflowNodeStatus, target: WorkflowNodeStatus,
        ) -> None:
            nonlocal released
            transition(node_id, current, target)
            if target is WorkflowNodeStatus.RUNNING and not released:
                released = True
                next_gate.set()

        isolated = replace(node, depends_on=(), condition="")
        subdefinition = replace(definition, nodes=(isolated,))

        def bounded_executor(invocation: WorkflowNodeInvocation) -> WorkflowNodeReceipt:
            nonlocal buffered_output_bytes
            result = executor(invocation)
            output_size = _workflow_output_size(result.outputs)
            with output_lock:
                previous_size = buffered_by_node.get(node.id, 0)
                projected = buffered_output_bytes - previous_size + output_size
                if projected > max_buffered_output_bytes:
                    buffered_by_node[node.id] = 0
                    buffered_output_bytes -= previous_size
                    return WorkflowNodeReceipt(
                        (
                            WorkflowNodeStatus.FAILED
                            if result.status is WorkflowNodeStatus.SUCCEEDED
                            else result.status
                        ),
                        reason=(
                            "workflow output buffer exceeds "
                            f"{max_buffered_output_bytes} bytes"
                        ),
                    )
                buffered_by_node[node.id] = output_size
                buffered_output_bytes = projected
            return result
        try:
            return execute_serial_workflow(
                subdefinition, resolved_inputs, bounded_executor,
                on_transition=node_transition, on_receipt=receipt,
                on_loop_continue=loop_continue,
                initial_states={
                    node.id: start_status
                } if start_status is WorkflowNodeStatus.READY else None,
                initial_attempt_counts={
                    node.id: int((initial_attempt_counts or {}).get(node.id, 0))
                },
                initial_loop_elapsed={
                    node.id: float((initial_loop_elapsed or {}).get(node.id, 0.0))
                },
                initial_retry_counts={
                    node.id: int((initial_retry_counts or {}).get(node.id, 0))
                },
                initial_retry_delays={
                    node.id: float((initial_retry_delays or {}).get(node.id, 0.0))
                },
                retry_wait=retry_wait, context_records=context_snapshot,
                invocation_workspace=(claims[node.id].path if node.id in claims else None),
                max_buffered_output_bytes=None,
                monotonic=monotonic,
            )
        finally:
            next_gate.set()

    with nullcontext(_SHARED_WORKFLOW_EXECUTOR) as pool:
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
                    terminal_without_dispatch(
                        node.id, WorkflowNodeStatus.SKIPPED,
                        "dependency did not succeed",
                    )
                    progressed = True
                    continue
                if node.condition and not evaluate_workflow_condition(
                    node.condition, _condition_context(resolved_inputs, records),
                ):
                    terminal_without_dispatch(
                        node.id, WorkflowNodeStatus.SKIPPED, "condition was false",
                    )
                    progressed = True

            capacity = limit - len(active)
            ready: list[WorkflowNodeDefinition] = []
            if capacity > 0:
                for node in definition.nodes:
                    if len(ready) >= capacity:
                        break
                    if (
                        states[node.id] not in {
                            WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY,
                        }
                        or node.id in active.values()
                        or not all(
                            states[item] in NODE_TERMINAL_STATUSES
                            for item in node.depends_on
                        )
                        or (
                            node.dependency_policy == "all_succeeded"
                            and any(
                                states[item] is not WorkflowNodeStatus.SUCCEEDED
                                for item in node.depends_on
                            )
                        )
                    ):
                        continue
                    conflicts = any(
                        workspace_conflict(node, by_id[node_id])
                        for node_id in active.values()
                    ) or any(
                        workspace_conflict(node, selected) for selected in ready
                    )
                    if conflicts:
                        break
                    ready.append(node)
            if ready:
                gates = [threading.Event() for _ in range(len(ready) + 1)]
                gates[0].set()
                for index, node in enumerate(ready):
                    snapshot = MappingProxyType(dict(records))
                    start_status = states[node.id]
                    active[pool.submit(
                        run_node, node, snapshot, start_status,
                        gates[index], gates[index + 1],
                    )] = node.id
                    states[node.id] = WorkflowNodeStatus.RUNNING
                progressed = True

            if active:
                completed, _pending = wait(tuple(active), return_when=FIRST_COMPLETED)
                for future in sorted(completed, key=lambda item: order[active[item]]):
                    node_id = active.pop(future)
                    result = future.result()
                    record = result.node(node_id)
                    states[node_id] = record.status
                    records[node_id] = record
                progressed = True

            if not progressed:
                if any(status is WorkflowNodeStatus.WAITING for status in states.values()):
                    ordered = tuple(
                        records.get(node.id, WorkflowNodeRecord(node.id, states[node.id]))
                        for node in definition.nodes
                    )
                    return WorkflowRunRecord(WorkflowRunStatus.WAITING, ordered)
                raise RuntimeError("concurrent workflow scheduler made no progress")

    ordered = tuple(records[node.id] for node in definition.nodes)
    return _workflow_result(ordered)


def _workflow_output_size(outputs: Mapping[str, Any]) -> int:
    """Return a deterministic retained-memory charge for a node output mapping."""
    def encoded_size(value: Any) -> int:
        if isinstance(value, WorkflowArtifactHandle):
            return encoded_size(value.summary()) + (
                value.artifact.size if value.artifact.inline is not None else 0
            )
        if isinstance(value, Mapping):
            size = 2
            for index, (key, item) in enumerate(value.items()):
                if index:
                    size += 1
                size += len(json.dumps(str(key), ensure_ascii=False).encode("utf-8"))
                size += 1 + encoded_size(item)
            return size
        if isinstance(value, (list, tuple)):
            return 2 + max(0, len(value) - 1) + sum(encoded_size(item) for item in value)
        try:
            return len(
                json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
        except (TypeError, ValueError):
            return len(repr(value).encode("utf-8", errors="replace"))

    return encoded_size(outputs)


def _workflow_result(ordered: tuple[WorkflowNodeRecord, ...]) -> WorkflowRunRecord:
    failed = any(
        item.status not in {WorkflowNodeStatus.SUCCEEDED, WorkflowNodeStatus.SKIPPED}
        for item in ordered
    )
    # A dependency-caused skip represents propagated failure; a condition skip does not.
    failed = failed or any(item.reason == "dependency did not succeed" for item in ordered)
    if any(item.status is WorkflowNodeStatus.CANCELLED for item in ordered):
        run_status = WorkflowRunStatus.CANCELLED
    elif any(item.status is WorkflowNodeStatus.DENIED for item in ordered):
        run_status = WorkflowRunStatus.DENIED
    elif any(item.status is WorkflowNodeStatus.TIMED_OUT for item in ordered):
        run_status = WorkflowRunStatus.TIMED_OUT
    elif any(item.status is WorkflowNodeStatus.BUDGET_EXHAUSTED for item in ordered):
        run_status = WorkflowRunStatus.BUDGET_EXHAUSTED
    elif any(item.status is WorkflowNodeStatus.INTERRUPTED for item in ordered):
        run_status = WorkflowRunStatus.INTERRUPTED
    else:
        run_status = WorkflowRunStatus.FAILED if failed else WorkflowRunStatus.SUCCEEDED
    return WorkflowRunRecord(run_status, ordered)


def _condition_context(
    inputs: Mapping[str, Any], records: Mapping[str, WorkflowNodeRecord],
    *,
    current_node: WorkflowNodeRecord | None = None,
    attempt_index: int | None = None,
    attempt_id: str | None = None,
) -> Mapping[str, Any]:
    context: dict[str, Any] = {
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
    if current_node is not None:
        context["node"] = {
            "status": current_node.status.value,
            "outputs": dict(current_node.outputs),
        }
    if attempt_index is not None:
        context["attempt"] = {
            "id": attempt_id, "index": attempt_index,
            "status": (
                current_node.attempt_status or current_node.status
            ).value if current_node is not None else WorkflowNodeStatus.RUNNING.value,
        }
    return context


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
