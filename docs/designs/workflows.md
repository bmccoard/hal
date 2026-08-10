# Workflow orchestration design

| Field | Value |
| --- | --- |
| Status | Initial implementation complete |
| Target | HAL interactive CLI and TUI |
| Last reviewed | 2026-08-09 |

## Summary

HAL distinguishes a phase, a workflow, and the agent loop:

- A **phase** is one model turn with a named instruction mode. `/design`, `/plan`,
  `/build`, and `/review` remain independently invocable.
- A **workflow** deterministically orders several phases around one user request.
- The **agent** retains discretion inside a phase: it reasons, calls tools, handles
  tool results, and decides when that phase is complete.

The initial built-in workflow is:

```text
/workflow feature <request>
        |
        +--> design --> plan --> build --> review
```

This is deliberately a bounded orchestrator, not a general scheduler or durable
business-process engine.

## User contract

`/workflows` lists available workflows. `/workflow feature <request>` runs all four
steps in order in the current session. Each step is a separate agent turn, so its
output and tool calls are preserved in the ordinary transcript and later steps can
use earlier results. Cancellation stops the current step and prevents later steps
from starting.

Invoking the `feature` workflow explicitly requests implementation. The design and
plan steps do not mutate production code; the build and review steps may make the
smallest coherent changes authorized by the original request. Existing tool
approvals still apply. The workflow grants no authority to install dependencies,
push changes, access unrelated data, or perform external side effects.

Design and plan receive only the read-only `glob`, `grep`, `read_file`, `git_status`,
`git_diff`, and `git_log` tools. Build and review retain the normal configured tool
set. A model call to a tool outside the phase allowlist is rejected even if the model
invents the hidden tool name.

## State and failure behavior

Workflow state is intentionally transient and consists of the workflow name,
original request, ordered phase names, and current step. Durable history remains the
normal HAL session transcript.

- Steps execute serially and exactly once.
- A missing configured phase fails before the first step begins.
- Provider, tool, or cancellation errors stop the workflow; remaining steps do not
  run. The interactive shell then performs its normal session snapshot, preserving
  transcript messages completed before the failure when saving succeeds.
- A phase that ends without a final textual response fails instead of being recorded
  as successfully completed.
- Three malformed argument calls to the same tool stop the active phase. Each
  rejected call receives a matching error result and is never executed.
- A resumed session retains completed step messages, but version one does not
  automatically resume a partially completed workflow.
- Review uses HAL's existing review phase, which may fix valid in-scope findings and
  rerun affected checks. There is no unbounded review/fix loop.

## Configuration and extension boundary

The initial workflow is built in rather than YAML-defined. Configured overrides of
the four named phase prompts are honored. Future configuration may add declarative
workflows only after validation, approval, resumption, and compatibility semantics
are established.

Domain workflows such as Jellyfin playlist construction belong in deterministic
extension code. HAL may invoke those tools from an agent phase, but agent workflow
state must not replace domain databases, task state, schedulers, or audit records.

## Security

Workflows are orchestration, not a sandbox. They use the same tools, process
credentials, filesystem access, extension code, and approval behavior as ordinary
HAL turns. Operators must continue to use reviewed packages, least-privilege
credentials, approved model endpoints, and OS/container isolation appropriate to
the workspace.

## Acceptance criteria

1. Both the basic REPL and TUI recognize `/workflows` and
   `/workflow feature <request>`.
2. The feature workflow executes `design`, `plan`, `build`, and `review` in order.
3. Every step uses the original request and preserves its result in the session.
4. Configured phase overrides are honored.
5. Invalid workflow names or missing requests produce actionable errors.
6. Cancellation or a step failure prevents later steps from executing.
7. Existing single-phase commands remain unchanged.

## Deferred capabilities

- User-defined workflow schemas
- Conditional branches and explicit output contracts
- Approval gates between steps
- Persisted workflow checkpoints and automatic resumption
- Retry policies and bounded review/fix cycles
- Headless workflow execution and structured JSON receipts
- Background scheduling or concurrent workflow steps
