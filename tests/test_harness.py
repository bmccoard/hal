import pytest

from hal.harness import (
    BUILTIN_CAPABILITIES, BudgetReason, Capability, RunBudgets, RunOutcome,
    RunStatus, compose_run_budgets, compose_tool_policy, resolve_capability,
)


@pytest.mark.parametrize("field,value", [
    ("provider_calls", 0),
    ("provider_calls", True),
    ("tool_calls", -1),
    ("input_tokens", 1.5),
    ("output_tokens", "10"),
    ("elapsed_seconds", 0),
    ("elapsed_seconds", False),
])
def test_run_budgets_reject_invalid_limits(field, value) -> None:
    values = {
        "provider_calls": None,
        "tool_calls": None,
        "elapsed_seconds": None,
        "input_tokens": None,
        "output_tokens": None,
        field: value,
    }

    with pytest.raises(ValueError, match=field):
        RunBudgets(**values)


def test_run_outcome_has_unique_id_and_running_default() -> None:
    first = RunOutcome()
    second = RunOutcome()

    assert first.run_id.startswith("run_")
    assert first.run_id != second.run_id
    assert first.status == RunStatus.RUNNING
    assert BudgetReason.TOOL_CALLS.code == "budget_tool_calls_exhausted"


def test_run_budget_composition_uses_strictest_limit_per_field() -> None:
    configured = RunBudgets(
        provider_calls=10, tool_calls=None, elapsed_seconds=30,
        input_tokens=100, output_tokens=None,
    )
    per_send = RunBudgets(
        provider_calls=None, tool_calls=4, elapsed_seconds=60,
        input_tokens=50, output_tokens=None,
    )

    assert compose_run_budgets(configured, per_send) == RunBudgets(
        provider_calls=10, tool_calls=4, elapsed_seconds=30,
        input_tokens=50, output_tokens=None,
    )
    assert compose_run_budgets(None, None) is None


def test_builtin_capabilities_have_expected_mutation_boundaries() -> None:
    inspect = resolve_capability("INSPECT")
    change = resolve_capability("change")

    assert inspect is BUILTIN_CAPABILITIES["inspect"]
    assert "read_file" in inspect.allowed_tools
    assert {"pdf_read", "docx_read"} <= inspect.allowed_tools
    assert "write_file" not in inspect.allowed_tools
    assert {"pdf_write", "pdf_form_write", "docx_write"}.isdisjoint(inspect.allowed_tools)
    assert "git_push" in change.denied_tools
    assert change.protect_existing_files is True


def test_unknown_capability_lists_available_names() -> None:
    with pytest.raises(ValueError, match="unknown capability.*inspect.*review"):
        resolve_capability("deploy")


def test_programmatic_custom_capabilities_cannot_override_builtins() -> None:
    with pytest.raises(ValueError, match="cannot be overridden"):
        resolve_capability(
            "inspect", {"inspect": Capability("inspect", "unsafe override")},
        )


def test_tool_policy_composition_only_narrows_access() -> None:
    base = Capability(
        "base", "base", allowed_tools=frozenset({"read_file", "grep", "bash"}),
        denied_tools=frozenset({"bash"}),
    )
    phase = Capability(
        "phase", "phase", allowed_tools=frozenset({"read_file", "bash"}),
        denied_tools=frozenset({"read_file"}), protect_existing_files=True,
    )

    policy = compose_tool_policy(
        base, phase, allowed_tools={"read_file", "grep"},
        denied_tools={"git_push"},
    )

    assert policy.allowed_tools == frozenset({"read_file"})
    assert policy.denied_tools == frozenset({"bash", "read_file", "git_push"})
    assert policy.protect_existing_files is True
