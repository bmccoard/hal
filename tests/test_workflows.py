import pytest

from hal.cancellation import CancellationToken
from hal.config import Config
from hal.context import resolve_phases
from hal.workflows import WORKFLOWS, parse_workflow_command, run_workflow


def test_parse_feature_workflow() -> None:
    workflow, request = parse_workflow_command("/workflow feature add retries")
    assert workflow is WORKFLOWS["feature"]
    assert request == "add retries"
    assert parse_workflow_command("ordinary request") is None


@pytest.mark.parametrize("text", ["/workflow", "/workflow feature"])
def test_workflow_requires_name_and_request(text: str) -> None:
    with pytest.raises(ValueError, match="usage"):
        parse_workflow_command(text)


def test_workflow_runs_phases_in_order_and_honors_overrides() -> None:
    calls = []

    class Agent:
        def send(self, prompt, display, cancellation, allowed_tools=None,
                 denied_tools=None):
            calls.append((
                prompt, display, cancellation, allowed_tools, denied_tools,
            ))
            return display

    phases = resolve_phases(Config(phases={
        "design": {"prompt": "CUSTOM DESIGN"},
    }))
    progress = []
    results = run_workflow(
        Agent(), WORKFLOWS["feature"], "add retries", phases,
        CancellationToken(),
        lambda index, total, name: progress.append((index, total, name)),
    )

    assert [item[2] for item in progress] == ["design", "plan", "build", "review"]
    assert "CUSTOM DESIGN" in calls[0][0]
    assert all("Original workflow request:\nadd retries" in call[0] for call in calls)
    assert "bash" not in calls[0][3]
    assert calls[2][3] is None
    assert all("git_commit" in call[4] for call in calls)
    assert all("git_push" in call[4] for call in calls)
    assert len(results) == 4


def test_workflow_stops_after_step_failure() -> None:
    class Agent:
        def __init__(self):
            self.calls = 0

        def send(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("provider failed")
            return "ok"

    agent = Agent()
    with pytest.raises(RuntimeError, match="provider failed"):
        run_workflow(
            agent, WORKFLOWS["feature"], "change", resolve_phases(Config()),
            CancellationToken(),
        )
    assert agent.calls == 2


def test_workflow_stops_when_phase_has_no_final_response() -> None:
    class Agent:
        def send(self, *_args, **_kwargs):
            return ""

    with pytest.raises(RuntimeError, match="design.*without a final response"):
        run_workflow(
            Agent(), WORKFLOWS["feature"], "change", resolve_phases(Config()),
            CancellationToken(),
        )
