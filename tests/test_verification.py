from pathlib import Path

import pytest

from hal.agent import Agent, BudgetExhaustedError, EventKind, VerificationError
from hal.harness import RunBudgets, RunStatus, resolve_capability
from hal.cancellation import CancelledError, CancellationToken
from hal.models import ContentBlock, Response, ToolSpec
from hal.process import ProcessResult, ProcessTimeout
from hal.tools import Registry, Tool
from hal.verification import (
    VerificationCheck, VerificationResult, VerificationStatus,
    run_verification_check,
)


class Provider:
    name = "test"

    def complete(self, _request, cancellation=None):
        return Response([ContentBlock("text", text="Implemented.")], "end_turn")


class NoopTool(Tool):
    @property
    def spec(self):
        return ToolSpec("noop", "noop", {"type": "object"})

    def run(self, arguments, cancellation=None):
        return "ok"


def test_verification_check_records_exit_status_and_bounded_output(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr("hal.verification.run_bounded_process", lambda *args, **kwargs: ProcessResult(
        args[0], 3, "stdout\n", "stderr\n", False, False,
    ))

    result = run_verification_check(
        VerificationCheck("tests", "pytest -q"), tmp_path,
    )

    assert result.status == VerificationStatus.FAILED
    assert result.passed is False
    assert result.returncode == 3
    assert result.output == "stdout\nstderr\n"


def test_verification_check_distinguishes_timeout_and_start_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    def timeout(*_args, **_kwargs):
        raise ProcessTimeout(2, "partial out", "partial err")

    monkeypatch.setattr("hal.verification.run_bounded_process", timeout)
    timed_out = run_verification_check(
        VerificationCheck("tests", "pytest", timeout_seconds=2), tmp_path,
    )
    assert timed_out.status == VerificationStatus.TIMED_OUT
    assert "partial outpartial err" in timed_out.output

    monkeypatch.setattr(
        "hal.verification.run_bounded_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cannot start")),
    )
    start_failed = run_verification_check(
        VerificationCheck("tests", "pytest"), tmp_path,
    )
    assert start_failed.status == VerificationStatus.START_FAILED
    assert start_failed.output == "cannot start"


def test_required_verification_failure_fails_run_and_emits_events(
    tmp_path: Path, monkeypatch,
) -> None:
    failed = VerificationResult(
        "tests", False, "one failed", 12, True,
        VerificationStatus.FAILED, 1,
    )
    monkeypatch.setattr("hal.agent.run_verification_check", lambda *_args: failed)
    events = []
    agent = Agent(
        Provider(), "model", "system", Registry([]), workspace=tmp_path,
        verification_checks=[VerificationCheck("tests", "pytest")],
        on_event=events.append,
    )

    with pytest.raises(VerificationError, match="tests"):
        agent.send("implement")

    assert agent.last_outcome is not None
    assert agent.last_outcome.reason == "verification_failed"
    assert agent.last_outcome.verification == [failed]
    assert [event.kind for event in events[-3:]] == [
        EventKind.VERIFICATION_FINISHED,
        EventKind.ERROR,
        EventKind.RUN_FINISHED,
    ]


def test_optional_verification_failure_does_not_fail_run(
    tmp_path: Path, monkeypatch,
) -> None:
    failed = VerificationResult(
        "lint", False, "warning", 1, False, VerificationStatus.FAILED, 1,
    )
    monkeypatch.setattr("hal.agent.run_verification_check", lambda *_args: failed)
    agent = Agent(
        Provider(), "model", "system", Registry([]), workspace=tmp_path,
        verification_checks=[VerificationCheck("lint", "lint", required=False)],
    )

    assert agent.send("implement") == "Implemented."
    assert agent.last_outcome is not None
    assert agent.last_outcome.verification == [failed]


def test_verification_cancellation_is_recorded_and_takes_precedence(
    tmp_path: Path, monkeypatch,
) -> None:
    cancelled = VerificationResult(
        "tests", False, "stopped", 2, True, VerificationStatus.CANCELLED,
    )

    def cancel(*_args):
        error = CancelledError("stopped")
        error.verification_result = cancelled
        raise error

    monkeypatch.setattr("hal.agent.run_verification_check", cancel)
    agent = Agent(
        Provider(), "model", "system", Registry([]), workspace=tmp_path,
        verification_checks=[VerificationCheck("tests", "pytest")],
    )

    with pytest.raises(CancelledError):
        agent.send("implement", cancellation=CancellationToken())

    assert agent.last_outcome is not None
    assert agent.last_outcome.reason == "cancelled"
    assert agent.last_outcome.verification == [cancelled]


def test_failed_verification_triggers_bounded_repair_under_same_policy(
    tmp_path: Path, monkeypatch,
) -> None:
    results = iter([
        VerificationResult(
            "tests", False, "assertion failed", 1, True,
            VerificationStatus.FAILED, 1,
        ),
        VerificationResult(
            "tests", True, "ok", 1, True,
            VerificationStatus.PASSED, 0,
        ),
    ])
    monkeypatch.setattr(
        "hal.agent.run_verification_check", lambda *_args: next(results),
    )
    events = []
    provider = Provider()
    agent = Agent(
        provider, "model", "system", Registry([]), workspace=tmp_path,
        capability=resolve_capability("change"), repair_attempts=1,
        verification_checks=[VerificationCheck("tests", "pytest")],
        on_event=events.append,
    )

    assert agent.send("implement") == "Implemented."

    assert agent.last_outcome is not None
    assert agent.last_outcome.status == RunStatus.SUCCEEDED
    assert agent.last_outcome.repair_attempts == 1
    assert [item.passed for item in agent.last_outcome.verification] == [False, True]
    repair = next(event for event in events if event.kind == EventKind.REPAIR_STARTED)
    assert repair.args == {"attempt": 1}
    assert "assertion failed" in repair.text
    assert all(event.capability == "change" for event in events)


def test_repair_attempts_are_bounded(
    tmp_path: Path, monkeypatch,
) -> None:
    failed = VerificationResult(
        "tests", False, "still failing", 1, True,
        VerificationStatus.FAILED, 1,
    )
    monkeypatch.setattr("hal.agent.run_verification_check", lambda *_args: failed)
    agent = Agent(
        Provider(), "model", "system", Registry([]), workspace=tmp_path,
        repair_attempts=1,
        verification_checks=[VerificationCheck("tests", "pytest")],
    )

    with pytest.raises(VerificationError):
        agent.send("implement")

    assert agent.last_outcome is not None
    assert agent.last_outcome.repair_attempts == 1
    assert len(agent.last_outcome.verification) == 2


def test_hard_budget_exhaustion_prevents_repair_from_starting(
    tmp_path: Path, monkeypatch,
) -> None:
    failed = VerificationResult(
        "tests", False, "failed", 1, True, VerificationStatus.FAILED, 1,
    )
    monkeypatch.setattr("hal.agent.run_verification_check", lambda *_args: failed)
    events = []
    agent = Agent(
        Provider(), "model", "system", Registry([]), workspace=tmp_path,
        budgets=RunBudgets(
            provider_calls=1, tool_calls=None, elapsed_seconds=None,
        ),
        repair_attempts=1,
        verification_checks=[VerificationCheck("tests", "pytest")],
        on_event=events.append,
    )

    with pytest.raises(BudgetExhaustedError):
        agent.send("implement")

    assert agent.last_outcome is not None
    assert agent.last_outcome.status == RunStatus.BUDGET_EXHAUSTED
    assert agent.last_outcome.repair_attempts == 0
    assert not any(event.kind == EventKind.REPAIR_STARTED for event in events)


def test_approval_denial_prevents_repair_from_starting(
    tmp_path: Path, monkeypatch,
) -> None:
    class ApprovalProvider:
        name = "test"

        def __init__(self):
            self.calls = 0

        def complete(self, _request, cancellation=None):
            self.calls += 1
            if self.calls == 1:
                return Response([
                    ContentBlock("tool_use", id="call-1", name="noop", input={}),
                ], "tool_use")
            return Response([ContentBlock("text", text="Done.")], "end_turn")

    failed = VerificationResult(
        "tests", False, "failed", 1, True, VerificationStatus.FAILED, 1,
    )
    monkeypatch.setattr("hal.agent.run_verification_check", lambda *_args: failed)
    events = []
    registry = Registry(
        [NoopTool()], approvals=["noop"], confirm=lambda _prompt: False,
    )
    agent = Agent(
        ApprovalProvider(), "model", "system", registry, workspace=tmp_path,
        repair_attempts=1,
        verification_checks=[VerificationCheck("tests", "pytest")],
        on_event=events.append,
    )

    with pytest.raises(VerificationError):
        agent.send("implement")

    assert agent.last_outcome is not None
    assert agent.last_outcome.repair_attempts == 0
    assert not any(event.kind == EventKind.REPAIR_STARTED for event in events)
