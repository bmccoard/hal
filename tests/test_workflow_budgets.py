from __future__ import annotations

import pytest

from hal.harness import RunBudgets
from hal.workflow_budgets import (
    WorkflowBudgetExhaustedError,
    WorkflowBudgetLedger,
    WorkflowBudgetReason,
    WorkflowBudgets,
    WorkflowUsage,
    compose_workflow_budgets,
    remaining_harness_budgets,
)


def test_workflow_budget_values_are_typed_and_positive() -> None:
    budgets = WorkflowBudgets(node_attempts=4, elapsed_seconds=1.5)
    assert budgets["node_attempts"] == 4
    with pytest.raises(KeyError):
        budgets["missing"]
    with pytest.raises(ValueError, match="positive integer"):
        WorkflowBudgets(node_attempts=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive number"):
        WorkflowBudgets(elapsed_seconds=0)
    with pytest.raises(ValueError, match="positive number"):
        WorkflowBudgets(tool_calls=True)  # type: ignore[arg-type]


def test_budget_composition_uses_strictest_finite_limit() -> None:
    composed = compose_workflow_budgets(
        WorkflowBudgets(node_attempts=10, provider_calls=None, tool_calls=100),
        WorkflowBudgets(node_attempts=5, provider_calls=20, tool_calls=None),
    )
    assert composed == WorkflowBudgets(
        node_attempts=5, provider_calls=20, tool_calls=100,
    )
    assert compose_workflow_budgets(None, None) is None


def test_ledger_accumulates_all_branches_and_rejects_overage() -> None:
    ledger = WorkflowBudgetLedger(WorkflowBudgets(node_attempts=2, tool_calls=5))
    ledger = ledger.consume(WorkflowUsage(node_attempts=1, tool_calls=3))
    ledger = ledger.consume(WorkflowUsage(node_attempts=1, tool_calls=2))
    assert ledger.usage == WorkflowUsage(node_attempts=2, tool_calls=5)
    assert ledger.remaining().node_attempts == 0
    with pytest.raises(WorkflowBudgetExhaustedError) as error:
        ledger.require_available(WorkflowBudgetReason.NODE_ATTEMPTS)
    assert error.value.reason == WorkflowBudgetReason.NODE_ATTEMPTS
    with pytest.raises(WorkflowBudgetExhaustedError) as error:
        ledger.consume(WorkflowUsage(tool_calls=1))
    assert error.value.reason == WorkflowBudgetReason.TOOL_CALLS


def test_remaining_harness_budget_cannot_reset_aggregate_usage() -> None:
    aggregate = WorkflowBudgets(
        provider_calls=10, tool_calls=20, elapsed_seconds=100,
        input_tokens=1_000, output_tokens=500,
    )
    usage = WorkflowUsage(
        provider_calls=6, tool_calls=7, elapsed_seconds=25,
        input_tokens=300, output_tokens=100,
    )
    remaining = remaining_harness_budgets(
        aggregate, usage,
        RunBudgets(
            provider_calls=8, tool_calls=5, elapsed_seconds=90,
            input_tokens=None, output_tokens=50,
        ),
    )
    assert remaining == RunBudgets(
        provider_calls=4, tool_calls=5, elapsed_seconds=75,
        input_tokens=700, output_tokens=50,
    )


def test_exhausted_aggregate_prevents_a_fresh_agent_attempt() -> None:
    with pytest.raises(WorkflowBudgetExhaustedError) as error:
        remaining_harness_budgets(
            WorkflowBudgets(provider_calls=2),
            WorkflowUsage(provider_calls=2),
            RunBudgets(provider_calls=50),
        )
    assert error.value.reason == WorkflowBudgetReason.PROVIDER_CALLS


def test_usage_and_run_budget_conversion_are_validated() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        WorkflowUsage(node_attempts=-1)
    converted = WorkflowBudgets.from_run_budgets(RunBudgets(provider_calls=3, tool_calls=4))
    assert converted.provider_calls == 3
    assert converted.tool_calls == 4
    assert converted.node_attempts is None
