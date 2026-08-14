import stat
from pathlib import Path

import pytest

from hal.agent import Agent
from hal.harness import (
    RunBudgets, RunCounters, RunOutcome, RunStatus, ToolPolicy,
)
from hal.journal import JOURNAL_VERSION, RunJournalStore
from hal.models import ContentBlock, Response, Usage
from hal.tools import Registry
from hal.verification import VerificationResult, VerificationStatus


def test_run_journal_round_trip_is_versioned_sanitized_and_private(
    tmp_path: Path,
) -> None:
    store = RunJournalStore(tmp_path / "runs")
    outcome = RunOutcome(
        capability="change", status=RunStatus.SUCCEEDED,
        reason="completed", final_text="API_KEY=must-not-be-persisted",
        counters=RunCounters(
            provider_calls=2, tool_calls=3, elapsed_seconds=1.5,
            usage=Usage(input_tokens=10, output_tokens=4),
        ),
        verification=[VerificationResult(
            "tests", True, "ok", 12, True, VerificationStatus.PASSED, 0,
        )],
        repair_attempts=1,
    )
    policy = ToolPolicy(
        frozenset({"read_file", "grep"}), frozenset({"bash"}), True,
    )
    budgets = RunBudgets(provider_calls=4, tool_calls=8, elapsed_seconds=30)

    path = store.save(outcome, policy, budgets, tmp_path, event_count=9)
    payload = store.load(outcome.run_id)

    assert payload is not None
    assert payload["version"] == JOURNAL_VERSION
    assert payload["run_id"] == outcome.run_id
    assert payload["status"] == "succeeded"
    assert payload["policy"]["allowed_tools"] == ["grep", "read_file"]
    assert payload["verification"][0]["status"] == "passed"
    assert payload["repair_attempts"] == 1
    assert payload["event_count"] == 9
    assert "final_text" not in payload
    assert "must-not-be-persisted" not in path.read_text(encoding="utf-8")
    if hasattr(stat, "S_IMODE"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_corrupt_run_journal_warns_without_returning_partial_data(
    tmp_path: Path,
) -> None:
    store = RunJournalStore(tmp_path)
    path = tmp_path / "run_broken.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.warns(RuntimeWarning, match="could not read run journal"):
        assert store.load("run_broken") is None


def test_agent_journals_terminal_failure_without_replacing_exception(
    tmp_path: Path,
) -> None:
    class FailingProvider:
        name = "test"

        def complete(self, _request, cancellation=None):
            raise RuntimeError("provider failed")

    store = RunJournalStore(tmp_path / "runs")
    agent = Agent(
        FailingProvider(), "model", "system", Registry([]),
        journal_store=store, workspace=tmp_path,
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        agent.send("start")

    assert agent.last_outcome is not None
    payload = store.load(agent.last_outcome.run_id)
    assert payload is not None
    assert payload["status"] == "failed"
    assert payload["reason"] == "agent_error"


def test_journal_write_failure_does_not_change_successful_result(tmp_path: Path) -> None:
    class Provider:
        name = "test"

        def complete(self, _request, cancellation=None):
            return Response([ContentBlock("text", text="Done.")], "end_turn")

    class BrokenStore:
        def save(self, *_args, **_kwargs):
            raise OSError("disk full")

    agent = Agent(
        Provider(), "model", "system", Registry([]),
        journal_store=BrokenStore(), workspace=tmp_path,
    )

    assert agent.send("start") == "Done."
    assert len(agent.journal_errors) == 1
    assert str(agent.journal_errors[0]) == "disk full"
