"""HAL: a local, provider-neutral coding agent."""

from .agent import (
    Agent,
    AgentError,
    BudgetExhaustedError,
    ContextWindowExceededError,
    MaxOutputTokensError,
    MaxTurnsError,
    UnexpectedStopReasonError,
)
from .config import Config, load_config
from .harness import (
    BUILTIN_CAPABILITIES,
    BudgetReason,
    Capability,
    RunBudgets,
    RunCounters,
    RunOutcome,
    RunStatus,
    ToolPolicy,
    compose_tool_policy,
    resolve_capability,
)

__all__ = [
    "Agent",
    "AgentError",
    "BudgetExhaustedError",
    "BudgetReason",
    "BUILTIN_CAPABILITIES",
    "Capability",
    "Config",
    "ContextWindowExceededError",
    "MaxOutputTokensError",
    "MaxTurnsError",
    "RunBudgets",
    "RunCounters",
    "RunOutcome",
    "RunStatus",
    "ToolPolicy",
    "UnexpectedStopReasonError",
    "compose_tool_policy",
    "load_config",
    "resolve_capability",
]
__version__ = "0.1.0"
