"""HAL: a local, provider-neutral coding agent."""

from .agent import (
    Agent,
    AgentError,
    BudgetExhaustedError,
    ContextWindowExceededError,
    MaxOutputTokensError,
    MaxTurnsError,
    UnexpectedStopReasonError,
    VerificationError,
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
    SubagentProfile,
    ToolPolicy,
    compose_run_budgets,
    compose_tool_policy,
    resolve_capability,
)
from .verification import VerificationCheck, VerificationResult, VerificationStatus
from .journal import JOURNAL_VERSION, RunJournalStore
from .tools import ToolEffect

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
    "JOURNAL_VERSION",
    "RunBudgets",
    "RunCounters",
    "RunOutcome",
    "RunJournalStore",
    "RunStatus",
    "SubagentProfile",
    "ToolPolicy",
    "ToolEffect",
    "UnexpectedStopReasonError",
    "VerificationCheck",
    "VerificationError",
    "VerificationResult",
    "VerificationStatus",
    "compose_run_budgets",
    "compose_tool_policy",
    "load_config",
    "resolve_capability",
]
__version__ = "0.4"
