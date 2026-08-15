from __future__ import annotations

from pathlib import Path
from hal.cancellation import CancelledError
from hal.workflow_budgets import WorkflowBudgetExhaustedError, WorkflowBudgetReason

from hal.workflow_runtime import WorkflowNodeReceipt, execute_serial_workflow
from hal.workflow_schema import (
    WORKFLOW_DIRECTORY, WorkflowNodeStatus, WorkflowRunStatus, load_workflow,
)


def _definition(tmp_path: Path, nodes: str):
    directory = tmp_path / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True)
    path = directory / "runtime.yaml"
    path.write_text(f"version: 1\nname: runtime\nnodes:\n{nodes}", encoding="utf-8")
    return load_workflow(path, tmp_path)


def test_serial_scheduler_uses_stable_definition_order_for_ready_nodes(tmp_path: Path) -> None:
    definition = _definition(tmp_path, """
  - {id: first, type: agent, prompt: first}
  - {id: second, type: agent, prompt: second}
  - {id: join, type: agent, prompt: join, depends_on: [second, first]}
""")
    calls: list[str] = []
    transitions = []

    result = execute_serial_workflow(
        definition, {},
        lambda invocation: (
            calls.append(invocation.node.id) or WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)
        ),
        lambda *transition: transitions.append(transition),
    )

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert calls == ["first", "second", "join"]
    assert [item.status for item in result.nodes] == [WorkflowNodeStatus.SUCCEEDED] * 3
    assert transitions[:3] == [
        ("first", WorkflowNodeStatus.PENDING, WorkflowNodeStatus.READY),
        ("first", WorkflowNodeStatus.READY, WorkflowNodeStatus.RUNNING),
        ("first", WorkflowNodeStatus.RUNNING, WorkflowNodeStatus.SUCCEEDED),
    ]


def test_failed_dependency_skips_downstream_without_dispatch(tmp_path: Path) -> None:
    definition = _definition(tmp_path, """
  - {id: fail, type: agent, prompt: fail}
  - {id: blocked, type: agent, prompt: blocked, depends_on: [fail]}
""")
    calls: list[str] = []

    result = execute_serial_workflow(
        definition, {},
        lambda invocation: (
            calls.append(invocation.node.id) or WorkflowNodeReceipt(WorkflowNodeStatus.FAILED)
        ),
    )

    assert calls == ["fail"]
    assert result.status is WorkflowRunStatus.FAILED
    assert result.node("blocked").status is WorkflowNodeStatus.SKIPPED
    assert result.node("blocked").reason == "dependency did not succeed"


def test_all_terminal_dependency_policy_runs_after_failure(tmp_path: Path) -> None:
    definition = _definition(tmp_path, """
  - {id: fail, type: agent, prompt: fail}
  - id: cleanup
    type: agent
    prompt: cleanup
    depends_on: [fail]
    dependency_policy: all_terminal
""")
    calls: list[str] = []

    def execute(invocation):
        calls.append(invocation.node.id)
        status = WorkflowNodeStatus.FAILED if invocation.node.id == "fail" else WorkflowNodeStatus.SUCCEEDED
        return WorkflowNodeReceipt(status)

    result = execute_serial_workflow(definition, {}, execute)

    assert calls == ["fail", "cleanup"]
    assert result.status is WorkflowRunStatus.FAILED


def test_false_condition_is_an_explicit_successful_skip(tmp_path: Path) -> None:
    definition = _definition(tmp_path, """
  - id: optional
    type: agent
    prompt: optional
    condition: "${{ workflow.status == 'failed' }}"
""")

    result = execute_serial_workflow(
        definition, {}, lambda _node: (_ for _ in ()).throw(AssertionError("dispatched")),
    )

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert result.node("optional").status is WorkflowNodeStatus.SKIPPED
    assert result.node("optional").reason == "condition was false"


def test_executor_exception_becomes_sanitized_failed_receipt(tmp_path: Path) -> None:
    definition = _definition(
        tmp_path, "  - {id: work, type: agent, prompt: work}\n",
    )

    result = execute_serial_workflow(
        definition, {}, lambda _node: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert result.status is WorkflowRunStatus.FAILED
    assert result.node("work").reason == "RuntimeError: boom"


def test_cancellation_and_budget_exhaustion_remain_typed_terminal_states(tmp_path: Path) -> None:
    definition = _definition(
        tmp_path, "  - {id: work, type: agent, prompt: work}\n",
    )

    cancelled = execute_serial_workflow(
        definition, {}, lambda _invocation: (_ for _ in ()).throw(CancelledError("stop")),
    )
    exhausted = execute_serial_workflow(
        definition, {},
        lambda _invocation: (_ for _ in ()).throw(
            WorkflowBudgetExhaustedError(WorkflowBudgetReason.NODE_ATTEMPTS)
        ),
    )

    assert cancelled.status is WorkflowRunStatus.CANCELLED
    assert cancelled.node("work").status is WorkflowNodeStatus.CANCELLED
    assert exhausted.status is WorkflowRunStatus.BUDGET_EXHAUSTED
    assert exhausted.node("work").status is WorkflowNodeStatus.BUDGET_EXHAUSTED
