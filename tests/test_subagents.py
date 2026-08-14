import json
from types import SimpleNamespace

import pytest

from hal.cancellation import CancellationToken
from hal.harness import RunBudgets, RunStatus, SubagentProfile, resolve_capability
from hal.subagents import DelegateTool


class Parent:
    def __init__(self) -> None:
        self.calls = []

    def run_subagent(
        self, prompt, capability, budgets, *, provider=None, model=None,
        system=None, cancellation=None,
    ):
        self.calls.append((
            prompt, capability, budgets, provider, model, system, cancellation,
        ))
        return "inspected", SimpleNamespace(
            run_id="run_child", status=RunStatus.SUCCEEDED,
        )


def test_delegate_tool_exposes_only_profile_and_task_and_uses_fixed_policy() -> None:
    profile = SubagentProfile(
        "researcher", resolve_capability("inspect"),
        RunBudgets(provider_calls=3, tool_calls=4, elapsed_seconds=30),
        "small-model", "Inspect code",
    )
    tool = DelegateTool({"researcher": profile})
    parent = Parent()
    tool.bind_agent(parent)
    cancellation = CancellationToken()

    assert tool.spec.input_schema["properties"]["profile"]["enum"] == [
        "researcher",
    ]
    assert set(tool.spec.input_schema["properties"]) == {"profile", "task"}
    assert tool.spec.input_schema["additionalProperties"] is False
    result = json.loads(tool.run(
        {"profile": "researcher", "task": "inspect auth"}, cancellation,
    ))

    assert result == {
        "profile": "researcher", "run_id": "run_child",
        "status": "succeeded", "result": "inspected",
    }
    assert parent.calls == [(
        "inspect auth", profile.capability, profile.budgets,
        None, "small-model", None, cancellation,
    )]


def test_delegate_tool_rejects_model_selected_unknown_policy_fields() -> None:
    tool = DelegateTool({
        "safe": SubagentProfile(
            "safe", resolve_capability("inspect"), RunBudgets(),
        ),
    })
    tool.bind_agent(Parent())

    with pytest.raises(ValueError, match="unknown subagent profile"):
        tool.run({"profile": "admin", "task": "work"})
    with pytest.raises(ValueError, match="non-empty"):
        tool.run({"profile": "safe", "task": ""})
