from __future__ import annotations

from pathlib import Path
import pytest
import threading
import time
import hal.workflow_worktrees as workflow_worktrees
from hal.cancellation import CancelledError
from hal.workflow_budgets import (
    WorkflowBudgetExhaustedError, WorkflowBudgetLedger, WorkflowBudgetReason,
    WorkflowBudgets, WorkflowUsage,
)

from hal.workflow_runtime import (
    MAX_WORKFLOW_WORKERS_PER_RUN, WorkflowNodeReceipt, WorkflowTransientError,
    execute_concurrent_workflow, execute_serial_workflow,
)
from hal.workflow_schema import (
    WORKFLOW_DIRECTORY, WorkflowNodeStatus, WorkflowRunStatus, load_workflow,
)
from hal.workflow_worktrees import WorkflowWorktreeIdentity, validate_workspace_claim


def _definition(tmp_path: Path, nodes: str):
    directory = tmp_path / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True)
    path = directory / "runtime.yaml"
    path.write_text(f"version: 1\nname: runtime\nnodes:\n{nodes}", encoding="utf-8")
    return load_workflow(path, tmp_path)


def _workspace_claims(monkeypatch, repository: Path, workspaces: dict[str, Path]):
    identities = {
        path.resolve(): WorkflowWorktreeIdentity(
            path.resolve(), f"head-{node_id}", f"hal/{node_id}", "clean", (), True,
        )
        for node_id, path in workspaces.items()
    }
    monkeypatch.setattr(
        workflow_worktrees, "inspect_worktree",
        lambda _repository, workspace, _cancellation=None: identities[workspace.resolve()],
    )
    return {
        node_id: validate_workspace_claim(repository, path, {
            "path": str(path), "head": identities[path.resolve()].head,
            "branch": identities[path.resolve()].branch,
            "checkpoint_dirty_digest": identities[path.resolve()].dirty_digest,
        })
        for node_id, path in workspaces.items()
    }


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


def test_bounded_loop_runs_distinct_attempts_until_typed_condition_is_true(
    tmp_path: Path,
) -> None:
    definition = _definition(tmp_path, """
  - id: work
    type: agent
    prompt: work
    outputs:
      complete: {type: boolean, source: structured_response}
    loop:
      max_attempts: 3
      until: "${{ node.outputs.complete == true }}"
""")
    invocations = []

    def execute(invocation):
        invocations.append(invocation)
        return WorkflowNodeReceipt(
            WorkflowNodeStatus.SUCCEEDED,
            {"complete": invocation.attempt_index == 3},
        )

    result = execute_serial_workflow(definition, {}, execute)

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert [item.attempt_id for item in invocations] == [
        "attempt_1", "attempt_2", "attempt_3",
    ]
    assert [item.attempt_index for item in invocations] == [1, 2, 3]
    assert invocations[1].context["attempt"] == {
        "id": "attempt_2", "index": 2, "status": "running",
    }
    assert result.node("work").outputs == {"complete": True}


def test_bounded_loop_fails_when_attempt_limit_does_not_satisfy_condition(
    tmp_path: Path,
) -> None:
    definition = _definition(tmp_path, """
  - id: work
    type: agent
    prompt: work
    outputs:
      complete: {type: boolean, source: structured_response}
    loop:
      max_attempts: 2
      until: "${{ node.outputs.complete == true }}"
""")

    result = execute_serial_workflow(
        definition, {},
        lambda _invocation: WorkflowNodeReceipt(
            WorkflowNodeStatus.SUCCEEDED, {"complete": False},
        ),
    )

    assert result.status is WorkflowRunStatus.FAILED
    assert result.node("work").status is WorkflowNodeStatus.FAILED
    assert result.node("work").attempt_status is WorkflowNodeStatus.SUCCEEDED
    assert result.node("work").reason == (
        "loop condition was not satisfied after 2 attempt(s)"
    )


def test_loop_never_repeats_after_cancellation(tmp_path: Path) -> None:
    definition = _definition(tmp_path, """
  - id: work
    type: agent
    prompt: work
    loop:
      max_attempts: 3
      until: "${{ attempt.status == 'succeeded' }}"
""")
    calls = 0

    def execute(_invocation):
        nonlocal calls
        calls += 1
        return WorkflowNodeReceipt(WorkflowNodeStatus.CANCELLED, reason="stop")

    result = execute_serial_workflow(definition, {}, execute)

    assert calls == 1
    assert result.status is WorkflowRunStatus.CANCELLED


def test_loop_timeout_is_passed_to_attempt_and_is_a_typed_terminal_state(
    tmp_path: Path,
) -> None:
    definition = _definition(tmp_path, """
  - id: work
    type: agent
    prompt: work
    loop:
      timeout_seconds: 1
      until: "${{ attempt.status == 'failed' }}"
""")
    ticks = iter((0.0, 0.6, 0.6, 1.1))
    timeouts = []

    def execute(invocation):
        timeouts.append(invocation.timeout_seconds)
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

    result = execute_serial_workflow(
        definition, {}, execute, monotonic=lambda: next(ticks),
    )

    assert timeouts == [1.0, pytest.approx(0.4)]
    assert result.status is WorkflowRunStatus.TIMED_OUT
    assert result.node("work").status is WorkflowNodeStatus.TIMED_OUT
    assert result.node("work").reason == "loop timed out after 1 seconds"


def test_loop_attempts_cannot_reset_aggregate_attempt_budget(tmp_path: Path) -> None:
    definition = _definition(tmp_path, """
  - id: work
    type: agent
    prompt: work
    loop:
      max_attempts: 3
      until: "${{ attempt.status == 'failed' }}"
""")
    ledger = WorkflowBudgetLedger(WorkflowBudgets(node_attempts=1))
    calls = 0

    def execute(_invocation):
        nonlocal calls, ledger
        calls += 1
        ledger = ledger.consume(WorkflowUsage(node_attempts=1))
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

    result = execute_serial_workflow(definition, {}, execute)

    assert calls == 2
    assert ledger.usage.node_attempts == 1
    assert result.status is WorkflowRunStatus.BUDGET_EXHAUSTED
    assert result.node("work").status is WorkflowNodeStatus.BUDGET_EXHAUSTED


def _retry_definition(tmp_path: Path, policy: str):
    return _definition(tmp_path, f"""
  - id: work
    type: approval
    prompt: work
    retry: {policy}
""")


def test_transient_retry_uses_capped_exponential_backoff_and_distinct_attempts(
    tmp_path: Path,
) -> None:
    definition = _retry_definition(tmp_path, """
      max_attempts: 4
      error_classes: [network]
      initial_backoff_seconds: 0.5
      multiplier: 3
      max_backoff_seconds: 1
""")
    calls = []
    waits = []

    def execute(invocation):
        calls.append((invocation.attempt_id, invocation.retry_index))
        if len(calls) < 3:
            raise WorkflowTransientError("network", "temporarily unavailable")
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

    result = execute_serial_workflow(
        definition, {}, execute, retry_wait=waits.append,
    )

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert calls == [
        ("attempt_1", 0), ("attempt_2", 1), ("attempt_3", 2),
    ]
    assert waits == [0.5, 1.0]


def test_transient_retry_is_capped_and_requires_a_declared_error_class(
    tmp_path: Path,
) -> None:
    definition = _retry_definition(
        tmp_path, "{max_attempts: 2, error_classes: [network]}",
    )
    calls = 0

    def unavailable(_invocation):
        nonlocal calls
        calls += 1
        raise WorkflowTransientError("network", "offline")

    capped = execute_serial_workflow(
        definition, {}, unavailable, retry_wait=lambda _delay: None,
    )
    undeclared = execute_serial_workflow(
        definition, {},
        lambda _invocation: (_ for _ in ()).throw(
            WorkflowTransientError("timeout", "slow")
        ),
        retry_wait=lambda _delay: (_ for _ in ()).throw(AssertionError("waited")),
    )

    assert calls == 2
    assert capped.status is WorkflowRunStatus.FAILED
    assert capped.node("work").error_class == "network"
    assert undeclared.status is WorkflowRunStatus.FAILED
    assert undeclared.node("work").error_class == "timeout"


@pytest.mark.parametrize(
    "terminal",
    [WorkflowNodeStatus.DENIED, WorkflowNodeStatus.CANCELLED],
)
def test_retry_never_repeats_denial_or_cancellation(
    tmp_path: Path, terminal: WorkflowNodeStatus,
) -> None:
    definition = _retry_definition(
        tmp_path, "{max_attempts: 3, error_classes: [network]}",
    )
    calls = 0

    def execute(_invocation):
        nonlocal calls
        calls += 1
        return WorkflowNodeReceipt(terminal)

    result = execute_serial_workflow(definition, {}, execute)

    assert calls == 1
    assert result.node("work").status is terminal


def test_retry_backoff_is_cancellable_and_budget_aware(tmp_path: Path) -> None:
    definition = _retry_definition(
        tmp_path, "{max_attempts: 3, error_classes: [network]}",
    )

    def transient(_invocation):
        raise WorkflowTransientError("network", "offline")

    cancelled = execute_serial_workflow(
        definition, {}, transient,
        retry_wait=lambda _delay: (_ for _ in ()).throw(CancelledError("stop")),
    )
    exhausted = execute_serial_workflow(
        definition, {}, transient,
        retry_wait=lambda _delay: (_ for _ in ()).throw(
            WorkflowBudgetExhaustedError(WorkflowBudgetReason.ELAPSED_SECONDS)
        ),
    )

    assert cancelled.status is WorkflowRunStatus.CANCELLED
    assert exhausted.status is WorkflowRunStatus.BUDGET_EXHAUSTED


def test_concurrent_scheduler_caps_active_nodes_and_preserves_result_order(
    tmp_path: Path,
) -> None:
    definition = _definition(tmp_path, """
  - {id: first, type: agent, prompt: first}
  - {id: second, type: agent, prompt: second}
  - {id: third, type: agent, prompt: third}
""")
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    maximum = 0
    starts = []
    transitions = []

    def execute(invocation):
        nonlocal active, maximum
        with lock:
            starts.append(invocation.node.id)
            active += 1
            maximum = max(maximum, active)
        if invocation.node.id in {"first", "second"}:
            barrier.wait(timeout=2)
        time.sleep(0.01 if invocation.node.id == "first" else 0.02)
        with lock:
            active -= 1
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

    result = execute_concurrent_workflow(
        definition, {}, execute, max_parallel=2,
        on_transition=lambda *item: transitions.append(item),
    )

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert [item.node_id for item in result.nodes] == ["first", "second", "third"]
    assert starts[:2] == ["first", "second"]
    assert maximum == 2
    running = [item[0] for item in transitions if item[2] is WorkflowNodeStatus.RUNNING]
    assert running == ["first", "second", "third"]


def test_concurrent_scheduler_caps_per_run_workers_below_requested_parallelism(
    tmp_path: Path,
) -> None:
    nodes = "\n".join(
        f"  - {{id: node{i}, type: agent, prompt: work}}" for i in range(8)
    )
    definition = _definition(tmp_path, f"\n{nodes}\n")
    first_wave = threading.Barrier(MAX_WORKFLOW_WORKERS_PER_RUN)
    lock = threading.Lock()
    active = maximum = 0

    def execute(_invocation):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        first_wave.wait(timeout=2)
        time.sleep(0.01)
        with lock:
            active -= 1
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

    result = execute_concurrent_workflow(
        definition, {}, execute, max_parallel=100,
    )

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert maximum == MAX_WORKFLOW_WORKERS_PER_RUN


def test_shared_worker_pool_keeps_capacity_for_another_run(tmp_path: Path) -> None:
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    nodes = "\n".join(
        f"  - {{id: node{i}, type: agent, prompt: work}}" for i in range(8)
    )
    first = _definition(first_root, f"\n{nodes}\n")
    second = _definition(second_root, """
  - {id: peer, type: agent, prompt: work}
""")
    first_saturated = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    lock = threading.Lock()
    first_active = 0
    errors = []

    def execute_first(_invocation):
        nonlocal first_active
        with lock:
            first_active += 1
            if first_active == MAX_WORKFLOW_WORKERS_PER_RUN:
                first_saturated.set()
        assert release_first.wait(timeout=3)
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

    def execute_second(_invocation):
        second_started.set()
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

    def run(definition, executor):
        try:
            execute_concurrent_workflow(
                definition, {}, executor, max_parallel=100,
            )
        except BaseException as exc:
            errors.append(exc)

    first_thread = threading.Thread(target=run, args=(first, execute_first))
    second_thread = threading.Thread(target=run, args=(second, execute_second))
    first_thread.start()
    assert first_saturated.wait(timeout=2)
    second_thread.start()
    assert second_started.wait(timeout=2)
    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []


def test_scheduler_fails_output_that_exceeds_aggregate_buffer_limit(tmp_path: Path) -> None:
    definition = _definition(tmp_path, """
  - id: first
    type: agent
    prompt: first
    outputs:
      value: {type: string, source: final_response}
  - id: second
    type: agent
    prompt: second
    outputs:
      value: {type: string, source: final_response}
""")

    def execute(invocation):
        if invocation.node.id == "second":
            time.sleep(0.03)
        return WorkflowNodeReceipt(
            WorkflowNodeStatus.SUCCEEDED, {"value": "x" * 55},
        )

    result = execute_concurrent_workflow(
        definition, {}, execute, max_parallel=2, max_buffered_output_bytes=100,
    )

    assert result.node("first").status is WorkflowNodeStatus.SUCCEEDED
    assert result.node("second").status is WorkflowNodeStatus.FAILED
    assert "output buffer exceeds 100 bytes" in result.node("second").reason
    assert result.node("second").outputs == {}


def test_serial_scheduler_applies_the_same_output_buffer_limit(tmp_path: Path) -> None:
    definition = _definition(tmp_path, """
  - id: work
    type: agent
    prompt: work
    outputs:
      value: {type: string, source: final_response}
""")

    result = execute_serial_workflow(
        definition, {},
        lambda _invocation: WorkflowNodeReceipt(
            WorkflowNodeStatus.SUCCEEDED, {"value": "x" * 100},
        ),
        max_buffered_output_bytes=32,
    )

    assert result.node("work").status is WorkflowNodeStatus.FAILED
    assert "output buffer exceeds 32 bytes" in result.node("work").reason


def test_concurrent_scheduler_waits_for_dependencies_and_maps_typed_outputs(
    tmp_path: Path,
) -> None:
    definition = _definition(tmp_path, """
  - id: first
    type: agent
    prompt: first
    outputs:
      value: {type: string, source: final_response}
  - id: second
    type: agent
    prompt: second
    outputs:
      value: {type: string, source: final_response}
  - id: join
    type: agent
    prompt: join
    depends_on: [first, second]
    inputs:
      first: {type: string, value: "${{ nodes.first.outputs.value }}"}
      second: {type: string, value: "${{ nodes.second.outputs.value }}"}
""")
    calls = []

    def execute(invocation):
        calls.append(invocation.node.id)
        if invocation.node.id == "join":
            assert invocation.inputs == {"first": "one", "second": "two"}
            return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)
        return WorkflowNodeReceipt(
            WorkflowNodeStatus.SUCCEEDED,
            {"value": "one" if invocation.node.id == "first" else "two"},
        )

    result = execute_concurrent_workflow(
        definition, {}, execute, max_parallel=2,
    )

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert calls[-1] == "join"


def test_concurrent_scheduler_propagates_failure_and_runs_all_terminal_cleanup(
    tmp_path: Path,
) -> None:
    definition = _definition(tmp_path, """
  - {id: fail, type: agent, prompt: fail}
  - {id: peer, type: agent, prompt: peer}
  - {id: blocked, type: agent, prompt: blocked, depends_on: [fail]}
  - id: cleanup
    type: agent
    prompt: cleanup
    depends_on: [fail, peer]
    dependency_policy: all_terminal
""")
    calls = []

    def execute(invocation):
        calls.append(invocation.node.id)
        return WorkflowNodeReceipt(
            WorkflowNodeStatus.FAILED
            if invocation.node.id == "fail" else WorkflowNodeStatus.SUCCEEDED,
        )

    result = execute_concurrent_workflow(
        definition, {}, execute, max_parallel=2,
    )

    assert result.status is WorkflowRunStatus.FAILED
    assert result.node("blocked").status is WorkflowNodeStatus.SKIPPED
    assert calls[-1] == "cleanup"


def test_concurrent_scheduler_keeps_cancellation_and_budget_exhaustion_typed(
    tmp_path: Path,
) -> None:
    definition = _definition(tmp_path, """
  - {id: cancelled, type: agent, prompt: cancelled}
  - {id: exhausted, type: agent, prompt: exhausted}
""")
    barrier = threading.Barrier(2)

    def execute(invocation):
        barrier.wait(timeout=2)
        if invocation.node.id == "cancelled":
            raise CancelledError("stop")
        raise WorkflowBudgetExhaustedError(WorkflowBudgetReason.NODE_ATTEMPTS)

    result = execute_concurrent_workflow(
        definition, {}, execute, max_parallel=2,
    )

    assert result.status is WorkflowRunStatus.CANCELLED
    assert result.node("cancelled").status is WorkflowNodeStatus.CANCELLED
    assert result.node("exhausted").status is WorkflowNodeStatus.BUDGET_EXHAUSTED


def test_concurrent_scheduler_does_not_overlap_shared_workspace_mutations(
    tmp_path: Path,
) -> None:
    definition = _definition(tmp_path, """
  - id: first
    type: command
    command: {argv: [tool]}
  - id: second
    type: command
    command: {argv: [tool]}
""")
    lock = threading.Lock()
    active = 0
    maximum = 0

    def execute(_invocation):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

    result = execute_concurrent_workflow(
        definition, {}, execute, max_parallel=2,
    )

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert maximum == 1


def test_concurrent_scheduler_allows_mutations_only_in_distinct_validated_workspaces(
    tmp_path: Path, monkeypatch,
) -> None:
    definition = _definition(tmp_path, """
  - {id: first, type: command, command: {argv: [tool]}}
  - {id: second, type: command, command: {argv: [tool]}}
""")
    first, second = tmp_path / "first-worktree", tmp_path / "second-worktree"
    first.mkdir()
    second.mkdir()
    claims = _workspace_claims(
        monkeypatch, tmp_path, {"first": first, "second": second},
    )
    barrier = threading.Barrier(2)
    seen = {}

    def execute(invocation):
        seen[invocation.node.id] = invocation.workspace
        barrier.wait(timeout=2)
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

    result = execute_concurrent_workflow(
        definition, {}, execute, max_parallel=2, workspace_claims=claims,
    )

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert seen == {"first": first.resolve(), "second": second.resolve()}


def test_concurrent_scheduler_rejects_forged_claims_and_serializes_nested_paths(
    tmp_path: Path, monkeypatch,
) -> None:
    definition = _definition(tmp_path, """
  - {id: first, type: command, command: {argv: [tool]}}
  - {id: second, type: command, command: {argv: [tool]}}
""")
    with pytest.raises(TypeError, match="host-validated"):
        execute_concurrent_workflow(
            definition, {}, lambda _invocation: None, max_parallel=2,
            workspace_claims={"first": tmp_path},
        )

    parent, nested = tmp_path / "worktree", tmp_path / "worktree" / "nested"
    nested.mkdir(parents=True)
    claims = _workspace_claims(
        monkeypatch, tmp_path, {"first": parent, "second": nested},
    )
    lock = threading.Lock()
    active = maximum = 0

    def execute(_invocation):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

    result = execute_concurrent_workflow(
        definition, {}, execute, max_parallel=2, workspace_claims=claims,
    )
    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert maximum == 1


def test_concurrent_retry_backoff_does_not_block_an_independent_node(
    tmp_path: Path,
) -> None:
    definition = _definition(tmp_path, """
  - id: retrying
    type: approval
    prompt: retrying
    retry:
      max_attempts: 2
      error_classes: [network]
      initial_backoff_seconds: 0.1
  - {id: peer, type: agent, prompt: peer}
""")
    peer_done = threading.Event()
    retry_calls = 0

    def execute(invocation):
        nonlocal retry_calls
        if invocation.node.id == "peer":
            peer_done.set()
            return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)
        retry_calls += 1
        if retry_calls == 1:
            raise WorkflowTransientError("network", "offline")
        return WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED)

    def wait_for_retry(_delay):
        assert peer_done.wait(timeout=2)

    result = execute_concurrent_workflow(
        definition, {}, execute, max_parallel=2,
        retry_wait=wait_for_retry,
    )

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert retry_calls == 2
