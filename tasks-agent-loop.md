# Agent loop improvement tasks

This checklist tracks reliability work for the provider-neutral loop in
`src/hal/agent.py`. An item is checked only after its focused tests pass.

## Priority 0 — transcript and failure integrity

- [x] Commit a structurally valid tool request/result pair when any tool in a batch
  raises `BaseException`; preserve completed results and mark remaining calls skipped.
- [x] Preserve streamed assistant text/commentary when the provider fails after
  emitting deltas, and expose visible text through a typed error's `partial_text`.
- [x] Validate tool-call identity and stop-reason invariants before executing tools.
- [x] Prevent event-handler failures from corrupting or aborting an agent turn.

## Priority 1 — bounded progress

- [x] Replace `repr`-based tool signatures with canonical, deterministic JSON.
- [x] Detect short repeating tool-call cycles and consecutive no-progress responses.
  - [x] Detect cycles of one to four tool calls repeated three times.
  - [x] Stop consecutive empty `pause_turn` responses that make no progress.
- [ ] Add configurable provider-call, tool-call, elapsed-time, and token budgets.
- [ ] Reduce the default provider-turn ceiling after the additional budgets exist.

## Priority 2 — context and execution architecture

- [ ] Compact history before the configured context threshold while preserving
  complete tool-call/result groups and recent turns.
- [ ] Refactor `Agent.send` into explicit request, validation, execution, commit, and
  finish stages with independently testable transcript invariants.
- [ ] Execute explicitly parallel-safe read tools concurrently with bounded workers,
  serial approval/mutation barriers, ordered results, and group cancellation.
- [ ] Apply queued steering only at safe provider-response or completed-tool-batch
  boundaries.

## Verification backlog

- [ ] Cover cancellation before request, during streaming, between tool calls, and
  during each supported tool class.
- [ ] Add property-style transcript tests ensuring every committed tool use has one
  ordered result under failures and interruption.
- [ ] Add performance tests for long transcripts, large tool batches, and event-heavy
  streaming responses.
