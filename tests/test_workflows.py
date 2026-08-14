import pytest

from hal.cancellation import CancellationToken
from hal.config import Config
from hal.context import resolve_phases
from hal.harness import Capability
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
                 denied_tools=None, protect_existing_files=False,
                 include_history=True, capability=None):
            calls.append((
                prompt, display, cancellation, allowed_tools, denied_tools,
                protect_existing_files, include_history, capability,
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
    assert calls[0][7].name == "inspect"
    assert calls[1][7].name == "plan"
    assert calls[2][7].name == "change"
    assert calls[3][7].name == "review"
    assert all(call[3] is None for call in calls)
    assert all(call[4] is None for call in calls)
    assert all(call[5] is False for call in calls)
    assert all(call[6] is False for call in calls)
    assert "Prior phase handoffs" not in calls[0][0]
    assert "### design\n/workflow feature [design] add retries" in calls[1][0]
    assert "### plan\n/workflow feature [plan] add retries" in calls[2][0]
    assert len(results) == 4


def test_workflow_bounds_phase_handoffs() -> None:
    prompts = []

    class Agent:
        def send(self, prompt, *_args, **_kwargs):
            prompts.append(prompt)
            return "x" * 5_000

    run_workflow(
        Agent(), WORKFLOWS["feature"], "change", resolve_phases(Config()),
        CancellationToken(),
    )

    assert "[handoff truncated]" in prompts[1]
    assert "x" * 4_001 not in prompts[1]


def test_configured_phase_can_select_custom_capability() -> None:
    calls = []

    class Agent:
        def send(self, *_args, **kwargs):
            calls.append(kwargs["capability"])
            return "ok"

    config = Config(
        phases={"build": {"capability": "docs"}},
        capabilities={"docs": Capability("docs", "docs")},
    )

    run_workflow(
        Agent(), WORKFLOWS["feature"], "change", resolve_phases(config),
        CancellationToken(),
    )

    assert calls[2].name == "docs"


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
