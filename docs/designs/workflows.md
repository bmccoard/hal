# Workflow orchestration design

| Field | Value |
| --- | --- |
| Status | Initial implementation complete; declarative orchestration target designed |
| Target | HAL CLI, TUI, headless workers, and future remote interfaces |
| Last reviewed | 2026-08-14 |

Implementation is tracked in [`tasks-workflows.md`](../../tasks-workflows.md).

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

The next orchestration generation expands this boundary. Repository-defined YAML
workflows will provide a durable, dependency-aware control plane around harnessed
agent runs, deterministic commands, human gates, isolated worktrees, and explicit
publication. The harness remains the enforcement layer for each agent node; a
workflow can narrow harness policy but can never broaden it.

## User contract

`/workflows` lists available workflows. `/workflow feature <request>` runs all four
steps in order in the current session. Each step is a separate agent turn. Its full
output and tool calls are preserved in the ordinary transcript, but they are not
replayed to the provider in later steps. Instead, each later phase receives the
bounded final responses of completed phases as handoffs and inspects the workspace
for authoritative state. Cancellation stops the current step and prevents later
steps from starting.

Invoking the `feature` workflow explicitly requests implementation. The design and
plan steps do not mutate production code; the build and review steps may make the
smallest coherent changes authorized by the original request. Existing tool
approvals still apply. The workflow grants no authority to install dependencies,
push changes, access unrelated data, or perform external side effects.

Design and plan receive only the read-only `glob`, `grep`, `read_file`, `git_status`,
`git_diff`, and `git_log` tools. Build and review retain the normal configured tool
set except for repository mutation: `git_init`, `git_stage`, `git_unstage`,
`git_commit`, and `git_push` are unavailable throughout the workflow. Committing or
pushing requires a separate explicit user request after the workflow completes. A
model call to a tool outside the phase policy is rejected even if the model invents
the hidden tool name.

## State and failure behavior

Workflow state is intentionally transient and consists of the workflow name,
original request, ordered phase names, and current step. Durable history remains the
normal HAL session transcript.

- Steps execute serially and exactly once.
- Provider context is isolated per phase. Earlier final responses are included as
  handoffs capped at 4,000 characters per phase; earlier tool calls and results stay
  in the saved session but do not consume later-phase model input.
- A missing configured phase fails before the first step begins.
- Provider, tool, or cancellation errors stop the workflow; remaining steps do not
  run. The interactive shell then performs its normal session snapshot, preserving
  transcript messages completed before the failure when saving succeeds.
- A phase that ends without a final textual response fails instead of being recorded
  as successfully completed.
- Three malformed argument calls to the same tool stop the active phase. Each
  rejected call receives a matching error result and is never executed.
- Three consecutive identical calls with identical results stop the active phase,
  preventing unproductive status, diff, or inspection loops.
- Build and review cannot use `write_file` to replace an existing file. New files
  remain supported; existing files require an exact-match `edit_file` operation.
- A resumed session retains completed step messages, but version one does not
  automatically resume a partially completed workflow.
- Review uses HAL's existing review phase, which may fix valid in-scope findings and
  rerun affected checks. There is no unbounded review/fix loop.

## Configuration and extension boundary

The initial workflow is built in rather than YAML-defined. Configured overrides of
the four named phase prompts are honored. Declarative workflows must be versioned,
validated completely before execution, and committed under `.hal/workflows/` so the
development process is reviewable with the code it governs. User-level workflows may
be added later, but repository definitions win only through an explicit and
documented precedence rule; silently shadowing a repository workflow is forbidden.

Domain workflows such as Jellyfin playlist construction belong in deterministic
extension code. HAL may invoke those tools from an agent phase, but agent workflow
state must not replace domain databases, task state, schedulers, or audit records.

## Security

Workflows are orchestration, not a sandbox. They use the same tools, process
credentials, filesystem access, extension code, and approval behavior as ordinary
HAL turns. Operators must continue to use reviewed packages, least-privilege
credentials, approved model endpoints, and OS/container isolation appropriate to
the workspace.

## Declarative orchestration target

HAL's target is a small, typed workflow language rather than unrestricted YAML that
is interpreted ad hoc by the model. YAML describes the graph and policy; HAL validates
and executes it. Prompt text cannot add nodes, change dependencies, relax capability
or budget limits, skip required gates, or authorize publication.

The initial declarative schema should resemble:

```yaml
version: 1
name: idea-to-pr
description: Plan, implement, verify, approve, and publish one repository change

inputs:
  request:
    type: string
    required: true

execution:
  workspace: worktree
  max_parallel: 2
  timeout_seconds: 3600
  budgets:
    node_attempts: 20
    provider_calls: 100
    tool_calls: 400

nodes:
  - id: plan
    type: agent
    capability: plan
    prompt: |
      Explore the repository and produce an implementation plan for:
      ${{ inputs.request }}
    outputs:
      plan:
        type: markdown
        source: final_response

  - id: implement
    type: agent
    depends_on: [plan]
    capability: change
    fresh_context: true
    inputs:
      plan:
        type: markdown
        value: ${{ nodes.plan.outputs.plan }}
    prompt: "Implement the next incomplete part of the plan and report structured progress."
    outputs:
      progress:
        type: json
        source: structured_response
    loop:
      max_attempts: 4
      until: ${{ node.outputs.progress.complete == true }}

  - id: tests
    type: command
    depends_on: [implement]
    command:
      argv: [python, -m, pytest, -q]
    timeout_seconds: 300
    outputs:
      report:
        type: check_result
        source: result

  - id: review
    type: agent
    depends_on: [tests]
    capability: review
    fresh_context: true
    inputs:
      plan:
        type: markdown
        value: ${{ nodes.plan.outputs.plan }}
      tests:
        type: check_result
        value: ${{ nodes.tests.outputs.report }}
    prompt: "Review the complete work against the plan and verification results."

  - id: approve
    type: approval
    depends_on: [review]
    prompt: "Approve these changes for publication?"
    feedback_output: review_feedback

  - id: publish
    type: publish
    depends_on: [approve]
    condition: ${{ nodes.approve.outcome == 'approved' }}
    provider: github
    operation: pull_request
    title: ${{ inputs.request }}
```

This is the version 1 configuration shape implemented by the inert loader and static
validator. Execution support is delivered separately so loading or inspecting this
definition cannot create a worktree, invoke a model, or run a command.

### Typed node kinds

Version one should support a deliberately small set of node kinds:

| Type | Responsibility |
| --- | --- |
| `agent` | Run one model turn through the HAL harness with explicit capability, budgets, inputs, and outputs. |
| `command` | Run a repository-authored deterministic argv command without asking a model to invent it; shell interpretation requires an explicit opt-in. |
| `approval` | Pause durably for an authorized human decision and optional typed feedback. |
| `git` | Perform a narrow local Git operation such as diff, commit, or branch preparation. |
| `publish` | Perform an explicitly authorized external action such as push or pull-request creation. |
| `workflow` | Invoke another versioned workflow with mapped typed inputs and outputs. |

Node implementations must register a schema, execution effects, resumability rules,
and output types. Unknown node types or fields fail validation. Extensions may add
node types only through the trusted extension registry; a workflow file cannot load
arbitrary Python or redefine a built-in type.

### Graph and dependency semantics

- `depends_on` forms a directed acyclic graph. HAL rejects missing references,
  duplicate IDs, self-dependencies, and cycles before creating a run.
- A node becomes eligible only when its dependency policy is satisfied. The default
  is `all_succeeded`; alternatives such as `all_terminal` must be explicit.
- Independent eligible nodes may run concurrently up to `execution.max_parallel`.
  Mutating nodes also acquire a workspace lock unless each operates in a distinct
  worktree.
- Conditions use a small, side-effect-free expression language over declared inputs,
  node status, outcomes, and typed outputs. No Python, shell expansion, network
  lookup, filesystem read, or model evaluation is allowed in expressions.
- A skipped node has a stable `skipped` terminal state. Downstream behavior must be
  determined by dependency policy rather than treating skipped as silently
  successful.
- Failure, cancellation, denial, timeout, and budget exhaustion remain distinct
  terminal states and are never collapsed into a generic false value.
- Workflow-wide budgets cap the aggregate graph, including parallel branches,
  subworkflows, retries, and loops. Node budgets may only narrow the remaining
  workflow allowance; starting a fresh node or context never resets aggregate usage.

### Bounded loops and retries

Loops are node-local and always bounded. Every loop declares `max_attempts`, a finite
timeout, or both; configuration with no finite bound is invalid. `until` evaluates
only typed attempt outputs and status. Each attempt receives a distinct ID and journal
entry, and may request `fresh_context` without losing workflow artifacts.

Infrastructure retry is separate from semantic repair. Retry policies may rerun a
node after declared transient failures with capped exponential backoff. Agent repair
uses a new harnessed attempt and remains under the original or narrower capability,
remaining workflow budget, and workspace policy. Neither mechanism may repeat after
human denial or cancellation unless the user explicitly resumes it.

### Typed inputs, outputs, and artifacts

Nodes exchange declared values rather than depending primarily on prose transcript
handoffs. Initial scalar and artifact types should include `string`, `boolean`,
`integer`, `json`, `markdown`, `path`, `diff`, `check_result`, and collections of
those types.

- Inputs are validated before a node starts; outputs are validated before the node is
  marked successful.
- Artifact references contain identity, type, producer, digest, size, and storage
  location. Large artifacts are referenced, not copied into prompts or journals.
- Passing an artifact to an agent is explicit. HAL renders a bounded summary or gives
  the agent a policy-checked read handle; it does not inject unlimited content.
- Secret values use a separate opaque secret-reference type, are resolved only for
  authorized node kinds, and are never serialized into workflow state or prompts.
- Final model text remains available as an output but is not the implicit source of
  truth for task state, test status, approval, or publication.

### Durable state, restart, and resume

Every workflow run receives a stable run ID and persists an atomic, versioned state
record after each transition. The record includes the workflow identity and digest,
validated inputs, graph, node and attempt states, artifact references, worktree and
branch identity, approvals, budgets, timestamps, and sanitized outcomes. It excludes
credentials, environment values, and unrestricted transcript content.

On restart, HAL validates the stored schema version, workflow digest, repository
identity, worktree, and artifacts before offering resume. Completed nodes are not
rerun. An interrupted node resumes only when its node type defines safe checkpoint
semantics; otherwise it becomes `interrupted` and requires an explicit retry or
operator decision. Definition changes never silently alter an active run: users must
resume against the pinned definition or explicitly migrate/cancel it.

### Worktree, branch, and concurrency lifecycle

`execution.workspace: worktree` creates an isolated Git worktree and run-specific
branch before the first mutating node. HAL records the original repository HEAD,
branch name, worktree path, and pre-existing state. A dirty source worktree is never
silently copied, reset, cleaned, or discarded.

Multiple runs may execute concurrently only in separate worktrees or under a
read-only capability. Worktree creation, branch names, concurrency limits, disk
limits, cancellation cleanup, retention, and recovery are deterministic host
operations—not model instructions. Cleanup is recoverable by default and never
deletes a worktree containing uncommitted changes without explicit confirmation.

### Human approval gates

Approval is a durable node, not a transient tool prompt. It records the requested
decision, bounded review artifacts, approver identity supplied by the authenticated
front end, decision, feedback, and timestamp. Closing a client leaves the run waiting;
it does not imply approval or denial.

CLI, TUI, and future remote interfaces must apply the same authorization rules and
optimistic-concurrency token so a stale screen cannot approve a newer revision.
Approval authorizes only the node and artifacts shown. If relevant code, diff, tests,
or publication metadata change afterward, the approval becomes stale and the gate
must run again.

### Git and publication boundaries

Git mutation and external publication are typed operations with explicit effects.
Agent nodes may prepare content but cannot smuggle a push or PR through shell access
when workflow policy denies publication. Tool-name denial alone is not a security
boundary: a publication-grade run must also withhold remote credentials and network
access from ordinary agent and command nodes, or execute them inside an appropriate
OS/container sandbox. HAL must report when it cannot enforce that separation rather
than presenting prompt policy as isolation.

- Local commit nodes require a declared message source and exact artifact/diff set.
- Push and pull-request operations require explicit workflow nodes, repository policy,
  credentials scoped to the target, and any configured approval gate.
- Publication is idempotent: resume discovers an existing branch or PR by stored
  identity rather than creating duplicates.
- A pull-request node consumes typed title, body, base/head branches, commits, checks,
  and review artifacts. It records the returned provider ID and URL.
- Force push, branch deletion, merge, and closing a PR are separate high-impact
  operations and are never implied by `publish`.

The initial local Git node supports only `status`, `diff`, `stage`, `commit`, and
`prepare_branch`. Mutating operations require an isolated workflow worktree. Stage
and commit normalize repository-relative paths and verify the exact staged set;
receipts include the backend, branch, HEAD commit, workspace identity, and resulting
status. Remote push is intentionally not a local Git operation.

The current host-process runner cannot enforce a network namespace and separate
publication-credential boundary for ordinary agent and command nodes. Workflow
inspection reports this limitation, and workflows containing publication nodes fail
closed at both initial invocation and resume. A later publication adapter must supply
that enforceable boundary before push or pull-request nodes can run.

A push is expressed as a `publish` node with `operation: push`. It names a trusted
adapter provider, a symbolic remote, an exact branch and full commit ID, and an
ancestor approval node. The approval must review `remote`, `branch`, and `commit` as
typed string inputs using the same values as the push node:

```yaml
  - id: approve-push
    type: approval
    prompt: Approve this exact branch update?
    inputs:
      remote: {type: string, value: origin}
      branch: {type: string, value: "${{ nodes.commit.outputs.branch }}"}
      commit: {type: string, value: "${{ nodes.commit.outputs.commit }}"}

  - id: push
    type: publish
    depends_on: [approve-push]
    provider: git
    operation: push
    remote: origin
    branch: "${{ nodes.commit.outputs.branch }}"
    commit: "${{ nodes.commit.outputs.commit }}"
    approval: approve-push
    outputs:
      receipt: {type: check_result, source: result}
```

The trusted adapter retains the credential and exposes only a non-secret scope ID,
canonical allowed remote identities, and an enforceable isolation assertion. HAL
verifies the approval outcome, active branch, exact HEAD, remote allowlist, and
credential scope before adapter invocation. The idempotency key is derived from the
provider, canonical remote, branch, and commit. The sanitized external receipt is
persisted with that key and the provider identity. Adapter requests cannot express
force push, ref deletion, or an arbitrary refspec.

A pull request uses `operation: pull_request` and a fixed typed input contract:
`title` (`string`), `body` (`markdown`), `base` and `head` (`string`), `commits`
(`json` list of full object IDs), `checks` (`check_result`), and `review`
(`markdown`). The approval node must review the symbolic remote and the exact same
typed values. This ensures stored body, check, and review artifact digests are part of
the human decision instead of relying on an unstructured prompt summary.

Before adapter invocation, HAL validates the current head branch, the complete
declared trailing commit sequence, successful checks, artifact integrity, the remote
allowlist, and `pull_request` credential scope. The create-or-discover adapter receives
a deterministic idempotency key derived from all PR metadata and artifact identities.
It must return the same canonical remote, base, head, commit sequence, a provider ID,
and an HTTPS URL. HAL accepts only `created` or `existing` outcomes and durably stores
the sanitized provider receipt. A repeated delivery therefore discovers the same PR
rather than requesting another one.

Publication uses a write-ahead external-effect journal. After all local identity,
approval, artifact, and credential checks pass—but before any provider call—HAL
atomically stores a sanitized intent containing the operation, provider, canonical
remote, exact effect identity, and deterministic idempotency key. Credentials and
publication content are never written to this journal.

Push and pull-request adapters expose reconciliation separately from mutation. Every
attempt calls `find_push` before `push`, or `find_pull_request` before
`create_pull_request`. If a process crashes or times out after remote acceptance but
before its receipt is stored, resume automatically starts a reconciliation attempt
for that publication node. The new attempt must reproduce the persisted intent
exactly; a changed key, branch, commit, PR metadata, or artifact identity fails closed.
When the provider returns the existing effect, HAL validates it against the intent and
persists the recovered receipt. Only a confirmed absence permits a new mutation.

Provider implementations are installed through a publication adapter registry keyed
by provider name and operation. The scheduler, state machine, and publication nodes
consume only provider-neutral push and pull-request requests/results; they contain no
GitHub, GitLab, or vendor-specific branches. An adapter key must match its declared
provider, and an absent provider fails closed.

The initial concrete adapter supports GitHub pull requests. Its REST client owns the
Bearer credential and exposes only repository-scoped methods. Workflow remote aliases
map to canonical `github:owner/repository` identities, which also define the credential
scope. Discovery lists open PRs for the exact base/head pair and requires HAL's hidden
idempotency marker; ambiguous matches fail. Both discovered and created PRs must match
the requested base, head, and head commit SHA before their provider-neutral receipt is
accepted. GitHub responses are bounded, URLs and repository components are validated,
and credentials are redacted from provider errors. A future GitLab adapter implements
the same registry contracts without changing workflow YAML or orchestration logic.

### Background execution and observability

Headless runs use the same state machine as interactive runs. A local worker or future
service may claim ready nodes with a renewable lease, heartbeat while executing, and
release or expire the lease after failure. At-most-one active attempt is required;
side-effecting nodes additionally rely on idempotency keys because process-level
exactly-once execution cannot be guaranteed.

Every transition emits a structured event with workflow run ID, node ID, attempt,
sequence number, timestamp, and sanitized status. CLI, TUI, Web, chat integrations,
and CI consume this event stream rather than implementing their own orchestration
semantics. Logs and artifacts have bounded retention and never include secrets by
default.

## Compatibility and migration

The built-in `feature` workflow remains available while declarative workflows are
introduced. Its behavior should eventually be expressible by the same versioned
schema and execution engine, then shipped as a built-in read-only definition. Existing
`/design`, `/plan`, `/build`, `/review`, and `/workflow feature` commands retain their
user-facing behavior.

Repository workflows require an explicit invocation in version one; merely checking
out a repository must not execute its YAML. Before first execution HAL displays the
workflow source, requested capabilities, commands, external effects, worktree policy,
and approval gates through `hal workflow inspect <name>`. A remembered trust decision
is pinned to the repository identity and workflow digest; changing the definition,
referenced subworkflow, command, capability, or external effect invalidates it.

## Acceptance criteria

1. Both the basic REPL and TUI recognize `/workflows` and
   `/workflow feature <request>`.
2. The feature workflow executes `design`, `plan`, `build`, and `review` in order.
3. Every step uses the original request and preserves its result in the session.
4. Configured phase overrides are honored.
5. Invalid workflow names or missing requests produce actionable errors.
6. Cancellation or a step failure prevents later steps from executing.
7. Existing single-phase commands remain unchanged.

The declarative orchestration generation adds these acceptance criteria:

8. A repository workflow is schema-validated and cycle-checked completely before any
   node or worktree side effect begins.
9. Independent nodes respect dependencies, conditions, workspace locks, and bounded
   concurrency with deterministic terminal states.
10. Every loop and retry is finite, journaled per attempt, cancellable, and constrained
    by remaining harness and workflow budgets.
11. A killed process can reconstruct a run without rerunning successful nodes or
    duplicating a commit, push, approval, or pull request.
12. Typed artifact validation prevents missing, stale, oversized, or incompatible
    outputs from silently reaching downstream nodes.
13. Approval remains pending across client disconnects and becomes stale whenever its
    reviewed artifact set changes.
14. Concurrent mutating runs use separate validated worktrees and never alter or
    clean pre-existing user changes.
15. Publication cannot occur through an agent, command, or resumed node unless the
    validated workflow and current approval state explicitly authorize it.
16. The same persisted state and event stream drive interactive, headless, background,
    and future remote interfaces.

## Delivery roadmap

1. **Schema and local DAG:** versioned repository YAML, `agent` and `command` nodes,
   dependencies, conditions, typed outputs, static inspection, and serial execution.
2. **Durability:** atomic state transitions, artifact storage, attempt journals,
   restart inspection, explicit retry, and safe resume.
3. **Control flow:** bounded loops, transient retry policy, dependency failure modes,
   and parallel read-only nodes.
4. **Isolation:** per-run branches and worktrees, workspace locking, concurrent runs,
   recovery, and conservative cleanup.
5. **Human gates:** durable approvals, feedback artifacts, stale-approval detection,
   and consistent CLI/TUI authorization.
6. **Publication:** typed Git commit/push and provider-backed PR nodes with
   idempotency, receipts, and narrowly scoped credentials.
7. **Background operation:** worker leases, event subscriptions, structured headless
   commands, scheduling adapters, and remote interface parity.

Each milestone must ship with failure-injection tests for cancellation, restart,
corrupt state, stale artifacts, budget exhaustion, approval denial, duplicate worker
claims, and partial external side effects. Later milestones may extend earlier schemas
only through explicit versioning and migration.
