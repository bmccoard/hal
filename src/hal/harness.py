"""Typed state for bounded HAL harness runs."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import secrets

from .models import Usage
from .verification import VerificationCheck, VerificationResult


READ_ONLY_TOOLS = frozenset({
    "glob", "grep", "read_file", "git_status", "git_diff", "git_log",
    "git_show",
})
DANGEROUS_GIT_TOOLS = frozenset({
    "git_init", "git_stage", "git_unstage", "git_commit", "git_push",
})


@dataclass(frozen=True, slots=True)
class Capability:
    """An immutable policy that can only narrow an agent's available actions."""

    name: str
    description: str
    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] = frozenset()
    protect_existing_files: bool = False
    budgets: RunBudgets | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("capability name must not be empty")


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Effective intersection of caller and capability restrictions."""

    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] = frozenset()
    protect_existing_files: bool = False


BUILTIN_CAPABILITIES = {
    "inspect": Capability(
        "inspect", "Inspect and explain without changing the workspace",
        allowed_tools=READ_ONLY_TOOLS,
    ),
    "plan": Capability(
        "plan", "Plan work without changing the workspace",
        allowed_tools=READ_ONLY_TOOLS,
    ),
    "change": Capability(
        "change", "Implement a repository change without Git publication actions",
        denied_tools=DANGEROUS_GIT_TOOLS,
        protect_existing_files=True,
    ),
    "review": Capability(
        "review", "Review and fix findings without Git publication actions",
        denied_tools=DANGEROUS_GIT_TOOLS,
        protect_existing_files=True,
    ),
}


def resolve_capability(
    name: str, custom: dict[str, Capability] | None = None,
) -> Capability:
    collisions = set(custom or {}) & set(BUILTIN_CAPABILITIES)
    if collisions:
        names = ", ".join(sorted(collisions))
        raise ValueError(f"built-in capability cannot be overridden: {names}")
    capabilities = {**BUILTIN_CAPABILITIES, **(custom or {})}
    try:
        return capabilities[name.strip().lower()]
    except KeyError as exc:
        available = ", ".join(sorted(capabilities))
        raise ValueError(
            f"unknown capability {name!r} (available: {available})"
        ) from exc


def compose_tool_policy(
    *capabilities: Capability | None,
    allowed_tools: set[str] | frozenset[str] | None = None,
    denied_tools: set[str] | frozenset[str] | None = None,
    protect_existing_files: bool = False,
) -> ToolPolicy:
    """Intersect policies so no later layer can restore removed access."""
    allowed = None if allowed_tools is None else frozenset(allowed_tools)
    denied = frozenset(denied_tools or ())
    protect = protect_existing_files
    for capability in capabilities:
        if capability is None:
            continue
        if capability.allowed_tools is not None:
            allowed = (
                capability.allowed_tools if allowed is None
                else allowed & capability.allowed_tools
            )
        denied |= capability.denied_tools
        protect = protect or capability.protect_existing_files
    return ToolPolicy(allowed, denied, protect)


class BudgetReason(str, Enum):
    PROVIDER_CALLS = "provider_calls"
    TOOL_CALLS = "tool_calls"
    ELAPSED_SECONDS = "elapsed_seconds"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"

    @property
    def code(self) -> str:
        return f"budget_{self.value}_exhausted"


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class RunBudgets:
    """Hard limits for one call to :meth:`Agent.send`.

    ``None`` disables an individual limit. Limits count only work performed by the
    current send, not usage restored from an earlier session.
    """

    provider_calls: int | None = 50
    tool_calls: int | None = 200
    elapsed_seconds: float | None = 900
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in ("provider_calls", "tool_calls", "input_tokens", "output_tokens"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} must be a positive integer or null")
        elapsed = self.elapsed_seconds
        if elapsed is not None and (
            isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed <= 0
        ):
            raise ValueError("elapsed_seconds must be a positive number or null")


@dataclass(frozen=True, slots=True)
class SubagentProfile:
    """Trusted configuration for one model-facing child-agent choice."""

    name: str
    capability: Capability
    budgets: RunBudgets
    model: str = ""
    description: str = ""


def compose_run_budgets(*budgets: RunBudgets | None) -> RunBudgets | None:
    """Resolve the strictest limit from every applicable budget layer.

    A ``None`` budget layer supplies no restrictions. Within a supplied layer, a
    ``None`` field is unbounded and therefore cannot relax a finite limit from
    another layer.
    """
    layers = tuple(budget for budget in budgets if budget is not None)
    if not layers:
        return None

    def strictest(name: str) -> int | float | None:
        limits = [getattr(budget, name) for budget in layers]
        finite = [limit for limit in limits if limit is not None]
        return min(finite) if finite else None

    return RunBudgets(
        provider_calls=strictest("provider_calls"),
        tool_calls=strictest("tool_calls"),
        elapsed_seconds=strictest("elapsed_seconds"),
        input_tokens=strictest("input_tokens"),
        output_tokens=strictest("output_tokens"),
    )


@dataclass(slots=True)
class RunCounters:
    provider_calls: int = 0
    tool_calls: int = 0
    elapsed_seconds: float = 0
    usage: Usage = field(default_factory=Usage)


@dataclass(slots=True)
class RunOutcome:
    run_id: str = field(default_factory=lambda: f"run_{secrets.token_hex(8)}")
    parent_run_id: str = ""
    capability: str = ""
    status: RunStatus = RunStatus.RUNNING
    final_text: str = ""
    reason: str = ""
    counters: RunCounters = field(default_factory=RunCounters)
    verification: list[VerificationResult] = field(default_factory=list)
    repair_attempts: int = 0
    child_outcomes: list[RunOutcome] = field(default_factory=list)
