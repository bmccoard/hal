# Declarative workflow orchestration tasks

This checklist tracks implementation of the declarative orchestration design in
`docs/designs/workflows.md`. The design document defines behavior and invariants;
this file defines implementation order and completion evidence.

An item is checked only after its code, focused tests, failure-path tests, and relevant
documentation pass. A milestone is usable only when all of its required tasks and
exit criteria are complete. Task references in `Depends on` are hard prerequisites
unless the task explicitly says they may proceed in parallel.

## Implemented compatibility baseline

- [x] **WF-000** Keep `/workflow feature <request>` as the built-in serial
  `design -> plan -> build -> review` workflow.
- [x] **WF-001** Run each built-in phase as a separate harnessed agent turn with a
  phase capability and bounded final-response handoff.
- [x] **WF-002** Stop the built-in workflow on cancellation, provider/tool failure,
  missing phase, or empty final response.
- [x] **WF-003** Prevent the built-in workflow from granting Git publication tools.

## Milestone 0 — contracts and state-machine foundation

- [x] **WF-010 — workflow identity and locations.** Define canonical names,
  `.hal/workflows/*.yaml` discovery, repository identity, definition digest,
  precedence, and built-in workflow identity.
  - Depends on: none.
- [x] **WF-011 — versioned schema.** Define the version-one workflow schema for
  metadata, typed inputs, execution policy, aggregate budgets, and nodes. Reject
  unknown versions, unknown fields, duplicate IDs, and invalid identifiers.
  - Depends on: WF-010.
- [x] **WF-012 — node type registry.** Define the trusted registry contract for each
  node type's configuration schema, effects, input/output types, execution method,
  resumability, and idempotency behavior. Prevent workflow YAML from importing code
  or replacing built-in node types.
  - Depends on: WF-011.
- [x] **WF-013 — terminal state model.** Define workflow, node, and attempt states,
  including pending, ready, running, waiting, succeeded, failed, skipped, denied,
  cancelled, timed out, budget exhausted, and interrupted. Define legal atomic
  transitions and precedence between cancellation, denial, timeout, and failure.
  - Depends on: WF-011.
- [x] **WF-014 — expression language.** Specify and implement a small parser for
  conditions and references over inputs, status, outcomes, and typed outputs. It must
  not evaluate Python, invoke a shell, read files, access the network, or call a model.
  - Depends on: WF-011.
- [x] **WF-015 — effect and trust model.** Classify node effects such as read,
  workspace mutation, Git mutation, credential use, network access, and publication.
  Define repository/digest-pinned trust and which changes invalidate prior trust.
  - Depends on: WF-012.
- [x] **WF-016 — aggregate budget model.** Compose workflow-wide node-attempt,
  provider-call, tool-call, token, and elapsed-time budgets with per-node harness
  budgets so a child node, loop, retry, or subworkflow cannot reset usage.
  - Depends on: WF-011 and the existing harness budget implementation.

### Milestone 0 exit criteria

- [ ] Invalid definitions fail before worktree creation, commands, model calls, or
  any other external effect.
- [ ] State-transition, expression, effect, and budget contracts have table-driven
  tests independent of the CLI and TUI.
- [ ] The built-in `feature` workflow can be represented by the internal schema
  without changing its user-facing behavior.

## Milestone 1 — schema, DAG, and static inspection

- [x] **WF-100 — YAML loading.** Load definitions with source locations and produce
  actionable field/path errors without executing YAML-specific object constructors.
  - Depends on: WF-010, WF-011.
- [x] **WF-101 — DAG validation.** Reject missing dependencies, duplicate IDs,
  self-dependencies, cycles, impossible dependency policies, and references to
  outputs that cannot exist on the referenced path.
  - Depends on: WF-011, WF-014, WF-100.
- [x] **WF-102 — typed reference checking.** Validate node input mappings, conditions,
  and declared outputs against producer and consumer types before execution.
  - Depends on: WF-012, WF-014, WF-101.
- [x] **WF-103 — command schema.** Use argv execution by default. Require an explicit
  shell mode and native shell selection when shell parsing is necessary. Validate
  timeouts, working directories, environment allowlists, and output bounds.
  - Depends on: WF-011, WF-015.
- [x] **WF-104 — inspection command.** Add `hal workflow list`,
  `hal workflow inspect <name>`, and `--json` output showing the resolved graph,
  commands, capabilities, budgets, worktree policy, credentials, network effects,
  approvals, publication, definition digest, and validation errors without running it.
  - Depends on: WF-100 through WF-103, WF-015, WF-016.
- [x] **WF-105 — explicit invocation.** Add a CLI contract for starting a named
  repository workflow with validated typed inputs. Merely opening or inspecting a
  repository must never execute its workflows.
  - Depends on: WF-104.

### Milestone 1 exit criteria

- [ ] Golden valid/invalid workflow fixtures cover Windows and POSIX argv behavior,
  dependency cycles, type mismatches, expressions, unknown fields, and schema versions.
- [ ] Inspection output is deterministic and sufficient for a user to understand all
  declared local and external effects before granting trust.

## Milestone 2 — serial execution and typed artifacts

- [x] **WF-200 — serial scheduler.** Execute ready nodes in stable topological order
  with `all_succeeded` as the default dependency policy and explicit skipped states.
  - Depends on: Milestone 1, WF-013.
- [x] **WF-201 — agent node.** Run an `agent` node through the existing harness with
  explicit capability, remaining aggregate budget, fresh-context option, typed inputs,
  cancellation, and structured outcome.
  - Depends on: WF-012, WF-016, WF-200.
- [x] **WF-202 — command node.** Execute deterministic command nodes with process-tree
  cancellation, timeout, bounded output, stable exit status, and no model involvement.
  - Depends on: WF-103, WF-200.
- [x] **WF-203 — artifact store.** Persist content-addressed artifact metadata with
  type, producer, digest, size, media type, and storage location. Write atomically and
  reject path escape, stale digest, missing content, and oversized inline values.
  - Depends on: WF-102.
- [x] **WF-204 — typed node output.** Validate outputs before success and pass only
  explicitly mapped values downstream. Support initial scalar types plus `markdown`,
  `path`, `diff`, and `check_result` artifacts.
  - Depends on: WF-201 through WF-203.
- [x] **WF-205 — bounded model projection.** Give agent nodes bounded artifact
  summaries or policy-checked read handles instead of injecting unrestricted artifact
  bodies into prompts.
  - Depends on: WF-203, WF-204.
- [x] **WF-206 — nested workflow node.** Invoke a pinned versioned workflow with typed
  input/output mappings, cycle prevention across workflow definitions, inherited
  cancellation, and strictly remaining aggregate budgets.
  - Depends on: WF-016, WF-200, WF-204.

### Milestone 2 exit criteria

- [x] A repository YAML workflow can run `agent -> command -> agent` serially with
  typed artifacts and deterministic failure propagation.
- [x] Prompt text and model output cannot alter the graph, satisfy a typed check by
  assertion alone, or broaden the effective harness policy.

## Milestone 3 — durable runs and safe resume

- [x] **WF-300 — persistent run record.** Store workflow identity/digest, validated
  inputs, graph, aggregate counters, node attempts, artifacts, timestamps, approvals,
  workspace identity, and sanitized outcomes in an atomic versioned record.
  - Depends on: Milestone 2, WF-013.
- [x] **WF-301 — write-ahead transitions.** Persist intent before side effects and
  completion receipts afterward so restart can distinguish not-started, in-flight,
  completed, and indeterminate operations.
  - Depends on: WF-300.
- [x] **WF-302 — resume audit.** Before resuming, validate schema version, pinned
  workflow digest, repository/worktree identity, artifact digests, budgets, leases,
  and external receipts. Never silently apply a changed workflow definition.
  - Depends on: WF-301.
- [x] **WF-303 — node recovery policy.** Completed nodes remain completed. Safely
  checkpointable nodes may resume; other in-flight nodes become `interrupted` and
  require explicit retry, skip, or cancellation according to node policy.
  - Depends on: WF-302 and resumability metadata from WF-012.
- [x] **WF-304 — run commands.** Add list, status, events, resume, retry-node, cancel,
  and archive commands with structured JSON equivalents.
  - Depends on: WF-302, WF-303.
- [x] **WF-305 — definition migration.** Provide an explicit, validated migration
  mechanism for compatible schema or workflow revisions; retain the original digest
  and migration audit. Never auto-migrate an active run.
  - Depends on: WF-302.

### Milestone 3 exit criteria

- [ ] Failure-injection tests terminate the process before and after every transition
  and prove successful nodes do not rerun on resume.
- [ ] Corrupt, partial, unknown-version, stale-definition, and missing-artifact states
  fail closed with actionable recovery information.

## Milestone 4 — bounded control flow and concurrency

- [ ] **WF-400 — bounded node loops.** Implement `max_attempts`, finite timeout, typed
  `until`, per-attempt IDs, optional fresh context, and remaining-budget enforcement.
  Reject any loop with no finite bound.
  - Depends on: Milestone 3, WF-014, WF-016.
- [ ] **WF-401 — transient retry policy.** Separate infrastructure retry from semantic
  repair with declared error classes, capped attempts, cancellable exponential
  backoff, and no retry after denial or cancellation.
  - Depends on: WF-400.
- [ ] **WF-402 — dependency policies.** Implement and test `all_succeeded` and
  `all_terminal`; add any later policy only with explicit skipped/failed semantics.
  - Depends on: WF-200, WF-013.
- [ ] **WF-403 — concurrent scheduler.** Claim independent ready nodes up to the
  workflow limit while preserving event order, aggregate budgets, cancellation, and
  deterministic terminal results.
  - Depends on: WF-301, WF-402.
- [ ] **WF-404 — mutation barriers.** Run read-only nodes concurrently, but serialize
  workspace mutations unless nodes hold distinct validated workspaces.
  - Depends on: WF-015, WF-403.
- [ ] **WF-405 — fairness and backpressure.** Bound ready queues, output buffering,
  artifact writes, and per-run worker consumption so one graph cannot starve other
  runs or exhaust memory.
  - Depends on: WF-403.

### Milestone 4 exit criteria

- [ ] Race and property-style tests cover parallel success/failure, simultaneous
  budget exhaustion, cancellation, retry wake-up, skipped dependencies, and event
  sequence stability.
- [ ] No loop, retry, nested workflow, or parallel branch can exceed aggregate limits.

## Milestone 5 — worktree and branch isolation

- [x] **WF-500 — workspace preflight.** Resolve repository root, Git backend, HEAD,
  dirty state, ignored paths, worktree support, disk policy, and branch-name collision
  before mutation.
  - Depends on: WF-015, WF-300.
- [x] **WF-501 — isolated worktree creation.** Create a run-specific branch and
  worktree through deterministic Git code, then atomically attach their identities to
  the run before the first mutating node.
  - Depends on: WF-500.
- [x] **WF-502 — worktree validation on resume.** Detect moved, deleted, reused,
  externally modified, or wrong-HEAD worktrees and require explicit recovery.
  - Depends on: WF-302, WF-501.
- [x] **WF-503 — workspace locking.** Prevent two mutating attempts from sharing a
  worktree while permitting concurrency across isolated worktrees.
  - Depends on: WF-403, WF-501.
- [x] **WF-504 — conservative cleanup.** Define retention and recoverable cleanup.
  Never delete a dirty worktree, unpushed commit, branch, or user-owned change without
  an explicit reviewed action.
  - Depends on: WF-502.
- [x] **WF-505 — non-Git fallback.** Define whether workflows may use isolated copied
  workspaces or must reject `workspace: worktree` outside Git. Never silently downgrade
  isolation.
  - Depends on: WF-500.

### Milestone 5 exit criteria

- [ ] At least five mutating workflows can run concurrently without branch,
  worktree, artifact, journal, or cancellation cross-talk.
- [ ] Dirty source repositories and interrupted cleanup retain every pre-existing and
  generated change recoverably.

## Milestone 6 — durable human approval

- [x] **WF-600 — approval node.** Persist waiting state, prompt, bounded review
  artifacts, decision, typed feedback, authenticated approver identity, and timestamp.
  Client disconnect is not a decision.
  - Depends on: WF-204, WF-300.
- [x] **WF-601 — approval authorization.** Apply the same authorization policy across
  CLI, TUI, and later remote interfaces. Require an optimistic-concurrency token for
  the exact pending revision.
  - Depends on: WF-600.
- [x] **WF-602 — stale approval detection.** Digest the reviewed diff, commits, checks,
  workflow definition, and publication metadata. Any relevant change invalidates the
  decision and returns the node to waiting.
  - Depends on: WF-600, WF-203.
- [x] **WF-603 — approve/deny UX.** Show what is authorized, consequences, artifact
  changes, and typed feedback. Support durable approve, deny, request-changes, and
  cancel operations without treating UI closure as denial.
  - Depends on: WF-601, WF-602.
- [x] **WF-604 — trust confirmation.** Require repository/digest-pinned trust before
  the first execution of commands, credentials, network access, or publication, and
  invalidate it according to WF-015.
  - Depends on: WF-104, WF-015, WF-601.

### Milestone 6 exit criteria

- [x] Approval survives restart and client disconnect, rejects stale clients, and
  cannot authorize artifacts or effects not displayed to the approver.
- [x] Denial and cancellation prevent all dependent side effects and are never retried
  automatically.

## Milestone 7 — Git and publication nodes

- [x] **WF-700 — typed local Git nodes.** Implement status/diff, stage exact artifact
  set, commit, and branch preparation with structured receipts and repository identity.
  - Depends on: Milestone 5, WF-012.
- [x] **WF-701 — publication isolation.** Ensure ordinary agent and command nodes lack
  publication credentials and network access in publication-grade workflows, or
  report that the host cannot enforce the boundary. Tool-name denial is insufficient.
  - Depends on: WF-015, WF-604.
- [x] **WF-702 — push node.** Push an exact stored branch/commit to an allowed remote
  with scoped credentials, explicit approval policy, idempotency key, and receipt.
  Treat force push and branch deletion as separate unsupported/high-impact operations.
  - Depends on: WF-700, WF-701.
- [x] **WF-703 — pull-request node.** Consume typed title, body, base/head branches,
  commits, checks, and review artifacts; create or discover one PR idempotently and
  persist provider ID and URL.
  - Depends on: WF-702, WF-602.
- [x] **WF-704 — partial-effect recovery.** Reconcile timeout or crash after remote
  acceptance by querying stored idempotency/provider identity before retrying.
  - Depends on: WF-702, WF-703, WF-301.
- [x] **WF-705 — provider adapter boundary.** Keep GitHub, GitLab, or other publication
  APIs behind typed adapters with narrow credentials and provider-neutral outcomes.
  - Depends on: WF-703.

### Milestone 7 exit criteria

- [x] Restart and duplicate-delivery tests prove HAL cannot accidentally create a
  second push target, commit, or pull request.
- [x] No publication occurs without a validated workflow effect, current approval,
  exact artifact identity, allowed remote, and scoped credential.

## Milestone 8 — background workers and interface parity

- [ ] **WF-800 — worker protocol.** Claim ready nodes with renewable leases,
  heartbeats, expiry, fencing tokens, and at-most-one active attempt. Side effects
  still require idempotency because exactly-once process execution is impossible.
  - Depends on: Milestone 3, WF-403.
- [ ] **WF-801 — structured event stream.** Emit versioned workflow/node/attempt events
  with stable sequence numbers and sanitized payloads; support replay from an offset.
  - Depends on: WF-300.
- [ ] **WF-802 — headless/background CLI.** Start, attach, follow, detach, inspect,
  approve, cancel, and resume runs without changing orchestration semantics.
  - Depends on: WF-304, WF-800, WF-801.
- [ ] **WF-803 — TUI orchestration view.** Render DAG progress, active attempts,
  artifacts, budgets, approvals, worktree, failures, and recovery actions from the
  shared event/state APIs.
  - Depends on: WF-601, WF-801.
- [ ] **WF-804 — remote interface contract.** Define authentication, authorization,
  event subscription, artifact access, approval, and cancellation APIs so Web, chat,
  and CI integrations do not implement their own scheduler.
  - Depends on: WF-601, WF-801.
- [ ] **WF-805 — scheduling adapter boundary.** Allow external schedulers to request a
  workflow run with typed inputs and idempotency key; do not turn workflow state into
  a general-purpose business database or cron implementation.
  - Depends on: WF-800, WF-804.

### Milestone 8 exit criteria

- [ ] Interactive, detached, and remote clients observe and control the same persisted
  state machine without duplicate execution or interface-specific authorization gaps.
- [ ] Worker death, lease expiry, network partition, slow consumers, and reconnect are
  covered by deterministic integration tests.

## Milestone 9 — production hardening

- [ ] **WF-900 — compatibility suite.** Test schema migrations and the built-in
  `feature` workflow across supported Python, Windows/PowerShell, and POSIX platforms.
- [ ] **WF-901 — adversarial definitions.** Test YAML bombs, reference cycles, path
  escape, expression abuse, artifact substitution, secret exfiltration attempts,
  command injection, stale approvals, and publication bypass attempts.
- [ ] **WF-902 — scale and soak tests.** Exercise large DAGs, deep but bounded
  subworkflows, many concurrent runs, large artifacts, long waits, and journal replay.
- [ ] **WF-903 — observability and retention.** Add bounded metrics for queue time,
  node duration, attempts, budget consumption, artifact sizes, and terminal reasons;
  define journal/artifact/worktree retention without logging secrets or prompt bodies.
- [ ] **WF-904 — operator recovery guide.** Document safe recovery for corrupt state,
  missing worktrees, indeterminate external effects, expired credentials, stale
  approvals, and incompatible definitions.
- [ ] **WF-905 — end-to-end release gate.** Run idea-to-PR fixtures with injected
  failures at every side-effect boundary and require zero lost user changes, leaked
  secrets, unbounded retries, or duplicate publication effects.

## Global completion criteria

- [ ] Repository YAML controls process structure while the HAL harness controls every
  agent node's capabilities, tools, budgets, verification, cancellation, and outcome.
- [ ] Definitions are flexible and composable without allowing arbitrary node types,
  expressions, or implicit side effects.
- [ ] Every run is bounded, inspectable, resumable, recoverable, and attributable.
- [ ] Concurrent mutating runs are isolated and pre-existing user work is preserved.
- [ ] Human approval and publication are durable, exact-artifact, idempotent actions.
- [ ] CLI, TUI, headless workers, and remote integrations share one state machine and
  event stream.
