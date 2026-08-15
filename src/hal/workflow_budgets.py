"""Aggregate budgets shared by every node, branch, retry, and subworkflow."""
from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum

from .harness import RunBudgets, compose_run_budgets


class WorkflowBudgetReason(str, Enum):
    NODE_ATTEMPTS = "node_attempts"
    PROVIDER_CALLS = "provider_calls"
    TOOL_CALLS = "tool_calls"
    ELAPSED_SECONDS = "elapsed_seconds"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"


class WorkflowBudgetExhaustedError(RuntimeError):
    def __init__(self, reason: WorkflowBudgetReason) -> None:
        self.reason = reason
        super().__init__(f"workflow budget exhausted: {reason.value}")


@dataclass(frozen=True, slots=True)
class WorkflowBudgets:
    node_attempts: int | None = None
    provider_calls: int | None = None
    tool_calls: int | None = None
    elapsed_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{item.name} must be a positive number or null")
            if item.name != "elapsed_seconds" and not isinstance(value, int):
                raise ValueError(f"{item.name} must be a positive integer or null")

    def __getitem__(self, name: str) -> int | float | None:
        if name not in {item.name for item in fields(self)}:
            raise KeyError(name)
        return getattr(self, name)

    @classmethod
    def from_run_budgets(cls, budgets: RunBudgets) -> WorkflowBudgets:
        return cls(
            provider_calls=budgets.provider_calls,
            tool_calls=budgets.tool_calls,
            elapsed_seconds=budgets.elapsed_seconds,
            input_tokens=budgets.input_tokens,
            output_tokens=budgets.output_tokens,
        )


@dataclass(frozen=True, slots=True)
class WorkflowUsage:
    node_attempts: int = 0
    provider_calls: int = 0
    tool_calls: int = 0
    elapsed_seconds: float = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{item.name} usage must be a non-negative number")
            if item.name != "elapsed_seconds" and not isinstance(value, int):
                raise ValueError(f"{item.name} usage must be a non-negative integer")

    def plus(self, delta: WorkflowUsage) -> WorkflowUsage:
        return WorkflowUsage(**{
            item.name: getattr(self, item.name) + getattr(delta, item.name)
            for item in fields(self)
        })


@dataclass(frozen=True, slots=True)
class WorkflowBudgetRemaining:
    node_attempts: int | None = None
    provider_calls: int | None = None
    tool_calls: int | None = None
    elapsed_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class WorkflowBudgetLedger:
    budgets: WorkflowBudgets
    usage: WorkflowUsage = WorkflowUsage()

    def remaining(self) -> WorkflowBudgetRemaining:
        values: dict[str, int | float | None] = {}
        for item in fields(self.budgets):
            limit = getattr(self.budgets, item.name)
            used = getattr(self.usage, item.name)
            values[item.name] = None if limit is None else max(0, limit - used)
        return WorkflowBudgetRemaining(**values)

    def require_available(self, *reasons: WorkflowBudgetReason) -> None:
        selected = reasons or tuple(WorkflowBudgetReason)
        for reason in selected:
            limit = getattr(self.budgets, reason.value)
            if limit is not None and getattr(self.usage, reason.value) >= limit:
                raise WorkflowBudgetExhaustedError(reason)

    def consume(self, delta: WorkflowUsage) -> WorkflowBudgetLedger:
        updated = self.usage.plus(delta)
        for reason in WorkflowBudgetReason:
            limit = getattr(self.budgets, reason.value)
            if limit is not None and getattr(updated, reason.value) > limit:
                raise WorkflowBudgetExhaustedError(reason)
        return WorkflowBudgetLedger(self.budgets, updated)


def compose_workflow_budgets(*budgets: WorkflowBudgets | None) -> WorkflowBudgets | None:
    """Compose aggregate limits monotonically by selecting each strictest limit."""
    layers = tuple(item for item in budgets if item is not None)
    if not layers:
        return None
    values: dict[str, int | float | None] = {}
    for item in fields(WorkflowBudgets):
        finite = [getattr(layer, item.name) for layer in layers if getattr(layer, item.name) is not None]
        values[item.name] = min(finite) if finite else None
    return WorkflowBudgets(**values)


def remaining_harness_budgets(
    workflow_budgets: WorkflowBudgets | None,
    usage: WorkflowUsage,
    node_budgets: RunBudgets | None = None,
) -> RunBudgets | None:
    """Narrow one agent attempt to the remaining aggregate workflow allowance."""
    if workflow_budgets is None:
        return node_budgets
    ledger = WorkflowBudgetLedger(workflow_budgets, usage)
    for reason in (
        WorkflowBudgetReason.PROVIDER_CALLS, WorkflowBudgetReason.TOOL_CALLS,
        WorkflowBudgetReason.ELAPSED_SECONDS, WorkflowBudgetReason.INPUT_TOKENS,
        WorkflowBudgetReason.OUTPUT_TOKENS,
    ):
        ledger.require_available(reason)
    remaining = ledger.remaining()
    aggregate = RunBudgets(
        provider_calls=remaining.provider_calls,
        tool_calls=remaining.tool_calls,
        elapsed_seconds=remaining.elapsed_seconds,
        input_tokens=remaining.input_tokens,
        output_tokens=remaining.output_tokens,
    )
    return compose_run_budgets(aggregate, node_budgets)
