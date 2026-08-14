# HAL Harness Status and Roadmap

Last updated: 2026-08-13

## Purpose

HAL's harness turns an open-ended model turn into a bounded execution with explicit
tool policy, usage accounting, stable failure reasons, and a structured outcome. It
is the control layer around the provider-neutral agent loop.

The concepts have separate responsibilities:

- A **tool** performs one action.
- A **skill** supplies task-specific instructions.
- A **phase** changes the instructions for one turn.
- A **workflow** sequences phases.
- A **capability** narrows the actions available to a run.
- The **harness** enforces capabilities and budgets and records the outcome.

The harness is a safety and reliability control. It is not an operating-system
sandbox, and it must not be described as one.

## Status summary

| Area | Status | Current state |
| --- | --- | --- |
| Run types and outcomes | Implemented | Typed budgets, counters, statuses, stable run IDs, and `Agent.last_outcome`. |
| Budget enforcement | Implemented | Provider-call, tool-call, elapsed-time, input-token, and output-token limits. |
| Budget configuration | Implemented | Optional `harness.budgets` applies to headless, REPL, TUI, workflows, and resumed sessions. |
| Capabilities | Implemented | Immutable built-in `inspect`, `plan`, `change`, and `review` policies. |
| Policy composition | Implemented | Capability and caller restrictions compose monotonically and cannot restore denied tools. |
| Workflow integration | Implemented | `feature` uses `inspect -> plan -> change -> review`. |
| Verification | Not implemented | Trusted deterministic checks and result records are still required. |
| Bounded repair | Not implemented | Failed verification cannot yet trigger controlled repair attempts. |
| Run journal | Not implemented | Outcomes are in memory and are not persisted as versioned run journals. |
| Harness UX | Partial | Budget events and capability attribution exist; dedicated progress and outcome views do not. |
| Parallel tools and subagents | Not implemented | These remain dependent on the earlier harness milestones. |

Current verification baseline: `200 passed, 1 skipped` on 2026-08-13.

## Implemented

### Run model

`src/hal/harness.py` provides the current run model:

```python
class BudgetReason(str, Enum):
    PROVIDER_CALLS = "provider_calls"
    TOOL_CALLS = "tool_calls"
    ELAPSED_SECONDS = "elapsed_seconds"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"

class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"

@dataclass(frozen=True, slots=True)
class RunBudgets:
    provider_calls: int | None = 50
    tool_calls: int | None = 200
    elapsed_seconds: float | None = 900
    input_tokens: int | None = None
    output_tokens: int | None = None

@dataclass(slots=True)
class RunCounters:
    provider_calls: int = 0
    tool_calls: int = 0
    elapsed_seconds: float = 0
    usage: Usage = field(default_factory=Usage)

@dataclass(slots=True)
class RunOutcome:
    run_id: str
    capability: str = ""
    status: RunStatus = RunStatus.RUNNING
    final_text: str = ""
    reason: str = ""
    counters: RunCounters = field(default_factory=RunCounters)
```

Each `Agent.send` creates new counters and a new outcome. Successful, failed,
cancelled, and budget-exhausted sends update `Agent.last_outcome` without changing
the existing text-returning API.

Stable budget reason codes use the form:

- `budget_provider_calls_exhausted`
- `budget_tool_calls_exhausted`
- `budget_elapsed_seconds_exhausted`
- `budget_input_tokens_exhausted`
- `budget_output_tokens_exhausted`

### Budget enforcement

Budgets are checked at deterministic continuation boundaries:

- Provider limits are checked before starting another provider request.
- Tool limits are checked before starting another valid tool execution.
- Elapsed and token limits are checked before subsequent provider or tool work.
- A call already in progress may finish unless cancellation interrupts it.
- Provider-reported usage is counted only for the current send, not restored session
  history.
- Budget exhaustion in a multi-tool response records one ordered result for every
  announced call before the turn stops.

`BUDGET_UPDATED` events expose current provider calls, tool calls, and token usage.
Budget failures include their stable reason code on the error event. All events emitted
during a capability-bound send carry the active capability label.

### Configuration and runtime coverage

Budgets are opt-in. Omitting `harness.budgets` preserves the prior unlimited behavior.
An empty mapping uses the defaults; an individual `null` disables that limit.

```yaml
harness:
  default_capability: change
  budgets:
    provider_calls: 50
    tool_calls: 200
    elapsed_seconds: 900
    input_tokens: null
    output_tokens: null
```

Configuration is parsed and validated at startup. Unknown harness keys, unknown budget
keys, invalid values, and unknown capability names are rejected. The shared agent
factory applies the configuration to:

- `hal run`
- The basic REPL
- The Textual TUI
- Each workflow phase
- Agents reconstructed while resuming or switching sessions

Callers may also provide `RunBudgets` directly:

```python
from hal import RunBudgets

result = agent.send(
    "Implement the requested change",
    budgets=RunBudgets(provider_calls=40, tool_calls=150, elapsed_seconds=900),
)
outcome = agent.last_outcome
```

### Capabilities

The implemented capability model is deliberately small:

```python
@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    description: str
    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] = frozenset()
    protect_existing_files: bool = False
```

Built-in policies:

| Capability | Available actions |
| --- | --- |
| `inspect` | Read-only file/search/status/diff/log/show tools. |
| `plan` | The same read-only boundary, intended for planning. |
| `change` | Coding tools, with Git initialization, index changes, commit, and push denied; existing files are protected from whole-file `write_file` replacement. |
| `review` | Review and in-scope fixes under the same mutation and Git restrictions as `change`. |

Caller restrictions, the configured default capability, and a per-send capability are
composed as follows:

- Allowed-tool sets are intersected.
- Denied-tool sets are unioned.
- Existing-file protection uses logical OR.

Therefore a later layer cannot restore a tool or mutation mode removed by an earlier
layer. Restrictions in `Registry`, including approvals, `bash_policy`, and
`only_write_locally`, remain authoritative and cannot be weakened by a capability.

The built-in feature workflow maps phases to capabilities:

```text
design -> inspect
plan   -> plan
build  -> change
review -> review
```

### Tests completed

Focused coverage currently includes:

- Validation of every budget type and invalid boundary values.
- Provider-call exhaustion before another request.
- Tool-call exhaustion in the middle of a batch with complete ordered results.
- Token and elapsed-time exhaustion before announced tool execution.
- Successful run outcomes and per-run usage.
- Stable budget event reason codes.
- Capability schema filtering and denied execution.
- Monotonic composition across caller, default, and per-send capabilities.
- Workflow phase-to-capability routing.
- Harness configuration defaults, null limits, unknown keys, and invalid capabilities.
- Shared agent-factory wiring.

The complete repository suite currently passes with one expected skip.

## Remaining work

### Milestone 1 remainder: explicit loop stages and budget precedence

Status: partial.

The public `Agent.send` wrapper and internal `_run_turn` boundary now exist, but the
large serial loop has not yet been separated into independently testable request,
validation, execution, commit, and finish stages.

Still required:

- Extract explicit stages while preserving transcript invariants.
- Define and implement monotonic composition between configured, capability, and
  per-send budgets. Currently a per-send budget replaces the configured budget for
  that send rather than taking the stricter value.
- Decide whether the legacy `max_turns` limit becomes a compatibility alias or remains
  independent after all harness budgets are active.
- Add property-style transcript tests across arbitrary failures and interruptions.

### Milestone 2 remainder: capability completion

Status: built-ins complete; extensibility incomplete.

Still required:

- Add custom capability definitions to configuration.
- Validate configured tool names against the final registry after extensions load.
- Decide how custom named phases select capabilities outside built-in workflows.
- Add optional capability-specific budgets without allowing them to weaken global
  budgets.
- Add a user-facing way to inspect resolved capability policy before a run.

### Milestone 3: deterministic verification and bounded repair

Status: not started.

Add trusted process checks from configuration rather than model output:

```python
@dataclass(frozen=True, slots=True)
class VerificationCheck:
    name: str
    command: str
    timeout_seconds: float = 120
    required: bool = True

@dataclass(slots=True)
class VerificationResult:
    name: str
    passed: bool
    output: str
    duration_ms: int
    required: bool = True
```

Required behavior:

- Run checks serially in the workspace using HAL's cross-platform process and
  cancellation primitives.
- Bound output with the same head/tail policy used for tool results.
- Record command-start failures, nonzero exits, timeouts, and cancellation distinctly.
- Mark success only when all required checks pass.
- If configured attempts remain, give the agent a bounded failure report and allow a
  repair under the original capability and remaining budgets.
- Never repair after cancellation, approval denial, or hard budget exhaustion.
- Add `verification_started`, `verification_finished`, and `repair_started` events.
- Extend `RunOutcome` with verification results and repair-attempt count.

### Milestone 4: durable journal and harness UX

Status: not started, except for budget events and capability labels.

Still required:

- Emit `run_started` and `run_finished` events with run IDs and terminal reasons.
- Add monotonic event sequence numbers.
- Persist a versioned run journal atomically alongside session data.
- Store resolved policy, budgets, counters, verification, repairs, and final outcome.
- Keep environment variables, credentials, and other secrets out of journals.
- Make corrupt journals warn without breaking intact sessions.
- Show concise capability, budget, verification, repair, and terminal status in the
  TUI; provide detailed records in verbose mode.
- Add stable harness status and reason fields to `hal run --json` with meaningful exit
  codes for failure, cancellation, and budget exhaustion.

The first journal version only needs post-run inspection. Mid-turn replay and resume
are separate later work.

### Milestone 5: harness-powered concurrency and delegation

Status: not started.

Still required:

- Mark tools as read-only, mutating, approval-gated, and parallel-safe.
- Execute adjacent parallel-safe reads with bounded workers.
- Resolve approvals serially and treat them as execution barriers.
- Keep writes and shell calls serial.
- Commit parallel results in the provider's original request order.
- Cancel tool groups safely without violating transcript structure.
- Give subagents strictly narrower capabilities, separate budgets, and no way to
  restore coordinator permissions.
- Attribute child events, usage, and outcomes to both child and parent runs.

This milestone should not begin until budget precedence, verification, repair, and
durable outcomes are complete.

## Target lifecycle

The completed harness should execute this lifecycle:

1. **Prepare**: resolve and validate capability, budgets, checks, provider/model, and
   workspace identity; create the run ID and emit `run_started`.
2. **Execute**: request, validate, execute, and commit while enforcing policy,
   cancellation, and budgets.
3. **Verify**: run trusted checks and record bounded results.
4. **Repair**: when permitted, use failed verification results for a bounded repair
   attempt under the same or narrower policy.
5. **Finish**: produce one structured outcome, emit `run_finished`, and atomically
   persist the run journal.

## Safety and transcript invariants

These requirements apply to every remaining milestone:

- Prompt text cannot select or relax capability policy.
- Workflow, capability, caller, registry, and approval restrictions may remove access
  but never restore it.
- Approvals remain serial barriers resolved before an action begins.
- Every committed tool use has exactly one ordered result, including skipped,
  cancelled, malformed, unknown, denied, failed, and budget-blocked calls.
- Cancellation takes precedence over verification and repair.
- Verification and repair do not delete or rewrite pre-existing user changes as a
  cleanup strategy.
- Extension tools execute only through the registry and the resolved capability.
- Persisted records never include environment variables or authentication material.

## Proposed code boundaries

- `src/hal/harness.py`: capability, policy, budget, state, lifecycle, and outcome types.
- `src/hal/verification.py`: trusted verification checks and results.
- `src/hal/agent.py`: staged provider/tool loop and transcript invariants.
- `src/hal/tools.py`: registry enforcement and parallel-safety metadata.
- `src/hal/workflows.py`: phase-to-capability mapping and harness composition.
- `src/hal/sessions.py`: journal references and durable outcomes.
- `src/hal/config.py`: harness configuration and validation.
- `src/hal/tui.py` and `src/hal/cli.py`: progress, receipts, and terminal status.

The harness may depend on staged agent primitives. The agent must not depend on CLI,
TUI, workflow, or session-storage implementations.

## Completion definition

The harness is complete when HAL can run a repository change under a named capability,
enforce the strictest applicable limits and permissions, verify the result, attempt
only the configured number of repairs, persist an inspectable outcome, and retain a
structurally valid transcript for every success, failure, denial, timeout, budget
exhaustion, and cancellation path.
