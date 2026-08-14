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
| Budget enforcement | Implemented | Provider-call, tool-call, elapsed-time, input-token, and output-token limits; applicable layers compose to the strictest limit. |
| Budget configuration | Implemented | Optional `harness.budgets` applies to headless, REPL, TUI, workflows, and resumed sessions. |
| Capabilities | Implemented | Immutable built-ins plus validated custom policies, phase selection, and capability-specific budgets. |
| Policy composition | Implemented | Capability and caller restrictions compose monotonically and cannot restore denied tools. |
| Workflow integration | Implemented | `feature` uses `inspect -> plan -> change -> review`. |
| Verification | Implemented | Trusted checks produce bounded typed results and can trigger controlled repair under remaining policy and budgets. |
| Bounded repair | Implemented | Required check failures can trigger configured repair turns under the original policy and remaining budgets. |
| Run journal | Implemented | Sanitized versioned outcomes are persisted atomically alongside session data; corrupt records warn and are skipped. |
| Harness UX | Implemented | TUI progress and outcomes, verbose details, structured headless results, lifecycle events, and journals are available. |
| Parallel tools and subagents | Implemented | Mixed-batch tool concurrency and trusted model-facing child profiles preserve policy, budget, and attribution boundaries. |

Current verification baseline: `247 passed` on 2026-08-13.

## Usage reference

### Basic setup

Configure the harness in the project `hal.yaml`. This enables the built-in `change`
policy, bounded execution, trusted verification, and one repair attempt:

```yaml
harness:
  default_capability: change
  budgets:
    provider_calls: 50
    tool_calls: 200
    elapsed_seconds: 900
    input_tokens: null
    output_tokens: null
  verification:
    - name: tests
      command: .venv/bin/pytest -q
      timeout_seconds: 120
      required: true
  repair_attempts: 1
```

Omitting `harness.budgets` preserves unlimited legacy behavior. An empty mapping uses
the defaults. Setting one field to `null` disables only that limit. Applicable budget
layers always resolve to the smallest finite value.

Inspect the fully resolved policy without contacting a model:

```bash
hal harness
hal harness change --json
```

Without a capability argument, this uses `default_capability`, or `change` when no
default is configured. It reports the workspace, available and denied tools, tool
effects, approval metadata, effective budgets, checks, repairs, shell policy, and
existing-file protection.

### Running HAL

Run one headless task:

```bash
hal run "Implement the requested change"
hal run --json "Implement the requested change"
```

Use an interactive interface:

```bash
hal
hal chat
hal tui
```

For each send, the harness resolves policy and budgets, executes the agent, runs
trusted checks, performs at most the configured number of repairs, emits a structured
outcome, and writes a sanitized journal.

`hal run --json` includes the run ID, capability, terminal status and reason, counters,
token usage, verification summaries, and repair count.

| Exit code | Meaning |
| --- | --- |
| `0` | Run succeeded. |
| `1` | Provider, configuration, process, or other run failure. |
| `3` | Harness budget exhausted. |
| `4` | Required verification failed after allowed repairs. |
| `130` | Run cancelled. |

### Custom capabilities

Custom capabilities may narrow tools, protect existing files, and impose stricter
budgets. Built-ins cannot be redefined, and tool names are validated after extensions
load.

```yaml
harness:
  default_capability: docs
  capabilities:
    docs:
      description: Edit documentation only
      allowed_tools:
        - glob
        - grep
        - read_file
        - write_file
        - edit_file
      denied_tools:
        - bash
      protect_existing_files: true
      budgets:
        provider_calls: 10
        tool_calls: 20
```

Inspect it with `hal harness docs --json`.

Allowed-tool sets are intersected, denied-tool sets are combined, existing-file
protection uses logical OR, and budget fields use the smallest finite value. A later
layer can never restore access or budget removed earlier.

Configured phases can select a built-in or custom capability:

```yaml
phases:
  build:
    capability: docs
```

### Workflows

Run the built-in feature workflow from the REPL or TUI:

```text
/workflow feature Add retry handling to API requests
```

It executes these phase and capability pairs:

```text
design -> inspect
plan   -> plan
build  -> change
review -> review
```

Workflow policies compose with the configured default and cannot weaken it.

### Trusted verification and repair

Verification commands come only from configuration, never from model output. Checks
run serially in the workspace with bounded head/tail output. A required nonzero exit,
timeout, or command-start failure fails verification; optional failures are recorded
without changing an otherwise successful outcome.

When `repair_attempts` is greater than zero, HAL gives the agent a bounded failure
report and permits repair under the original policy and remaining budgets. It never
repairs after cancellation, approval denial, or hard budget exhaustion.

### Trusted subagents

Define model-facing child profiles in configuration:

```yaml
subagents:
  researcher:
    description: Inspect relevant code without modifying it
    model: small-model
    capability: inspect
    budgets:
      provider_calls: 10
      tool_calls: 20
      elapsed_seconds: 300
```

When profiles exist, HAL registers the serial `delegate` tool. The model may choose
only a configured profile and provide task text. Model, capability, tools, and budgets
cannot be supplied through tool arguments. Child policy is strictly narrower than the
parent, and child budgets are capped by the parent's remaining limits.

Programmatic integrations may use:

```python
from hal import RunBudgets, resolve_capability

text, child_outcome = agent.run_subagent(
    "Inspect the authentication flow",
    capability=resolve_capability("inspect"),
    budgets=RunBudgets(provider_calls=10, tool_calls=20, elapsed_seconds=300),
)
```

Child events and journals contain both run IDs. Child counters remain independently
inspectable and are also attributed to the parent.

### Journals and TUI status

CLI-created agents write versioned post-run journals under:

```text
~/.hal/sessions/runs/
```

Journals include resolved policy, budgets, counters, verification, repairs, child-run
summaries, event count, and terminal outcome. They intentionally omit prompts,
transcripts, final model text, environment variables, and credentials. Corrupt or
unsupported journals warn and are skipped without breaking session data.

The TUI shows verification progress, repair attempts, and terminal status. Verbose
mode additionally shows budget updates, bounded check output, and repair reports.

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
- Trusted verification parsing, bounded execution, typed failure modes, events, and
  required-versus-optional outcome behavior.

The complete repository suite currently passes.

## Remaining work

### Milestone 1: explicit loop stages and budget precedence

Status: implemented, with continued invariant fuzzing encouraged.

The public `Agent.send` wrapper and internal `_run_turn` orchestration boundary are
supplemented by independently testable request preparation, provider execution,
response accounting, validation, single-tool execution, ordered batch commit, and
successful-finish stages.

Implemented:

- Extract explicit stages while preserving transcript invariants.
- Add capability-specific budgets to the monotonic composition already used for
  configured and per-send budgets.
- Keep legacy `max_turns` as an independent compatibility safety backstop; harness
  provider-call budgets remain the stable configurable limit and reason code.

Ongoing hardening:

- Continue extending the property-style transcript matrix beyond mixed tool failures;
  cancellation, interruption, malformed, denied, unknown, failed, and budget-blocked
  paths already have invariant coverage.

### Milestone 2 remainder: capability completion

Status: complete.

Implemented:

- Add custom capability definitions to configuration.
- Validate configured tool names against the final registry after extensions load.
- Allow configured phases to select built-in or custom capabilities.
- Add optional capability-specific budgets without allowing them to weaken global
  budgets.

- Add a user-facing way to inspect resolved capability policy before a run.

### Milestone 3: deterministic verification and bounded repair

Status: implemented.

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

Implemented behavior:

- Run checks serially in the workspace using HAL's cross-platform process and
  cancellation primitives.
- Bound output with the same head/tail policy used for tool results.
- Record command-start failures, nonzero exits, timeouts, and cancellation distinctly.
- Mark success only when all required checks pass.
- Add `verification_started` and `verification_finished` events.
- Extend `RunOutcome` with verification results and repair-attempt count.
- If configured attempts remain, give the agent a bounded failure report and allow a
  repair under the original capability and remaining budgets.
- Never repair after cancellation, approval denial, or hard budget exhaustion.
- Emit `repair_started` for each attempt.

### Milestone 4: durable journal and harness UX

Status: implemented.

Implemented:

- Emit `run_started` and `run_finished` events with run IDs and terminal reasons.
- Add monotonic event sequence numbers.
- Persist a versioned run journal atomically alongside session data.
- Store resolved policy, budgets, counters, verification, repairs, event count, and
  terminal outcome without prompts, transcripts, environment data, or final model text.
- Make corrupt or unsupported journals warn without breaking intact session data.
- Add stable harness status and reason fields to `hal run --json`, including counters,
  verification results, repair count, and distinct exit codes.

Implemented UX also includes concise verification, repair, and terminal status in the
TUI, with budget and check-output details in verbose mode.

The first journal version only needs post-run inspection. Mid-turn replay and resume
are separate later work.

### Milestone 5: harness-powered concurrency and delegation

Status: implemented.

Implemented:

- Use explicit `parallel_safe` metadata and registry policy checks before concurrency.
- Classify built-in tools as read-only, mutating, or external and expose resolved
  approval gating; unknown extension metadata remains conservative and serial.
- Execute batches containing only parallel-safe tools with bounded workers.
- Split mixed tool batches into adjacent parallel-safe groups around serial barriers.
- Resolve approvals serially and treat them as execution barriers.
- Keep writes and shell calls serial.
- Commit parallel results in the provider's original request order.
- Cancel tool groups safely without violating transcript structure.
- Run programmatic child agents under strictly narrower inherited policy and budgets
  capped by the parent's remaining limits.
- Attribute child events and journals with parent and child run IDs, retain child
  outcomes on the parent, and aggregate child usage into parent counters.
- Expose configured subagent profiles through a model-facing delegation tool without
  allowing prompts or tool arguments to select broader policy.

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
