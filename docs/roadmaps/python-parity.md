# Neo Python parity roadmap

| Field | Value |
| --- | --- |
| Status | In progress |
| Target | `neo-py` |
| Last reviewed | 2026-08-06 |
| Current milestone | Phase 1 - core correctness and safety |
| Next work | Stream and bound tool output before buffering it in memory |

Audit basis: compared `neo-py` with the Go `neo` tree and its developer docs on
2026-08-06. The Python port has the basic provider-neutral loop, four API-key
providers, native Windows/Unix shell selection, coding tools, configuration,
AGENTS.md, skills, named phases, headless mode, and resumable sessions. The work
below covers capabilities that are missing, inactive, or materially less robust.

This file is the implementation plan and completion record. Stable product
decisions belong in design documents; completed checkboxes must be backed by code,
tests, or documentation recorded in the implementation log.

## Delivery rules

- [ ] Preserve Python-specific improvements: Windows PowerShell support, custom
  OpenAI-compatible provider profiles, `.env` loading, and editable/package installs.
- [ ] Add characterization tests before changing transcript, provider, session,
  tool, or CLI behavior. Keep old session files and minimal `neo.yaml` files compatible.
- [ ] Implement each phase with focused tests, full `pytest`, documentation updates,
  and a manual Windows smoke test. Add Linux and macOS CI once CI exists.
- [ ] Maintain a checked feature-parity matrix linking each Go capability to its
  Python implementation and tests; intentional differences must be documented.

## Phase 1 - Core loop, cancellation, and safety foundation

- [x] Introduce cancellation-aware provider and tool interfaces plus a richer event
  model (assistant text/commentary, tool call/result, parallel group, steering,
  max-turn, error, and done events with timing and call identity).
  - [x] Replace string/dictionary callbacks with typed events carrying tool-call
    identity, elapsed/duration timing, structured errors, and distinct assistant text
    and commentary kinds. Reserve parallel and steering kinds for their later phases.
  - [x] Thread a cancellation/deadline object through the agent, providers, registry,
    tool and approval boundaries, retry waits, and the headless CLI. Provider reads
    are deadline-bounded and shell cancellation terminates the process tree.
- [x] Enforce tool-turn transcript invariants atomically: commit an assistant tool
  request only after every matching result is ready; create explicit error results for
  unknown, denied, and failed calls; retain provider request order.
- [x] When cancellation is implemented, create matching cancelled/skipped results for
  announced calls while keeping tool request/result pairs structurally valid.
- [x] Match Go stop handling: support `end_turn`, `stop_sequence`, `tool_use`,
  `refusal`, `pause_turn`, `max_tokens`, and context-window exhaustion; fail once on
  unknown reasons; return partial text with typed truncation/max-turn errors.
- [x] Make `neo run --timeout` a true wall-clock deadline covering retries, provider
  calls, tools, and the complete agent loop rather than only shortening one HTTP call.
- [x] Add signal handling and cancellation propagation. Interrupt provider requests,
  shell process trees, searches, reads, and pending calls without corrupting history.
- [x] Add structured Git status/diff/log/commit/push tools with native-Git preference,
  automatic Dulwich fallback, explicit backend configuration, local-only "check in"
  semantics, path-scoped commits, and doctor diagnostics.
- [ ] Stream/bound tool output before it can consume unbounded memory. Include useful
  truncation metadata and preserve both the beginning and end of shell output.
- [x] Make `write_file` and `edit_file` atomic and preserve existing file modes.
- [x] Harden `read_file`: avoid reading an entire large file before applying the cap,
  require paging for oversized files, validate windows, and report offsets past EOF.
- [ ] Bring `grep`/`glob` to schema and behavior parity: `path`, `max_matches`, result
  `count`, recursive `**`, grep context lines, binary detection, bounded match-centered
  excerpts, deterministic ordering, ignored heavy directories, and explicit I/O errors.
- [ ] Use rooted file access for recursive search and project AGENTS.md reads so a
  symlink swap cannot escape the workspace; retain safe in-workspace symlinks.
- [ ] Apply `tool_approvals` to direct `!command` execution as well as model tool calls,
  and keep approval matching literal, case-sensitive, and interactive-only.
- [x] Implement the runtime-context portion of the accepted [agent behavior and platform-awareness design](../designs/agent-behavior.md):
  inject OS, selected shell/version, cwd, and path conventions into a generated
  runtime-context block instead of letting models infer Bash from the tool name.
- [x] Add a system-prompt action policy: questions, explanations, reviews, and example
  requests are read-only by default; mutate files or environments only when the user
  explicitly requests that outcome.
- [x] Add a system-prompt dependency policy: installation, upgrade, and removal require
  an explicit request or user approval, use the intended environment, and update
  project dependency metadata where appropriate.
- [ ] Decide and implement any deterministic dependency-installation guard needed
  beyond prompt policy and optional literal approvals, including headless behavior.
- [x] Add documented optional approval examples for `pip`, `python -m pip`,
  `py -m pip`, `write_file`, and `edit_file`, including the limits of literal matching.
- [x] Add Windows PowerShell 5.1, PowerShell 7, Bash, and POSIX-shell tests covering
  generated runtime context, tool commands, and user-facing command examples.
- [ ] Add centralized recursive secret redaction for configuration inspection,
  diagnostics, provider errors, and structured logs; cover custom profiles and nested
  credentials with fictional test values.
- [ ] Expand core tests substantially: malformed tool calls, unknown tools, denials,
  failures, truncation, cancellation at every boundary, max turns, all stop reasons,
  transcript ordering, symlink races, large files/lines, and shell child cleanup.

## Phase 2 - Context management and provider fidelity

- [ ] Implement transcript compaction at 70% of
  `compaction.context_window_tokens`, retaining the latest 20 messages and splitting
  only at a safe user-turn boundary that cannot orphan tool calls/results.
- [ ] Count compaction usage exactly once, including failed/unusable summaries, and
  use the active model/provider after model switches. Cover coordinator and subagents.
- [ ] Add ordered system-prompt blocks: stable base instructions plus skill catalog,
  followed by dynamic AGENTS.md context.
- [ ] Implement `features.prompt_caching`; mark only the stable prefix cacheable and
  emit Anthropic cache controls while flattening blocks for unsupported providers.
- [ ] Harden shared HTTP retries with cancellation-aware waits, provider-specific
  transient classifications, bounded jitter/backoff, numeric and HTTP-date
  `Retry-After`, and secret-safe errors.
- [ ] Complete provider-native transcript replay: validate and preserve OpenAI
  encrypted reasoning items and replay only valid Gemini thought metadata/function
  calls while retaining portable text/tool history across provider switches.
- [ ] Add best-effort image attachments from interactive path input, sniff supported
  media types, persist image blocks, and test Anthropic/OpenAI/Gemini conversions.
- [ ] Implement OpenAI ChatGPT/Codex subscription authentication: `neo login`, device
  code flow, restrictive atomic credential storage, refresh, Codex transport, and
  `neo logout`. Keep API-key auth as the default.
- [ ] Implement live model discovery/curated model choices per provider and make
  `/model` select from them instead of accepting an unchecked arbitrary string.
- [ ] Make model switching update the active compactor, any following subagent
  backend, and saved session metadata.
- [ ] On resume, restore the saved backend only when its provider/auth credentials are
  available and compatible; otherwise warn and fall back safely to current config.
- [ ] Add comprehensive provider contract tests with fake HTTP servers: wire formats,
  tools, images, raw replay, refusals, truncation, usage/cache accounting, retries,
  redaction, authentication refresh, and malformed responses.

## Phase 3 - Parallel tools, workflow, and subagents

- [ ] Execute adjacent parallel-safe tool calls concurrently with a bounded default
  of eight. Resolve approvals serially, treat them as barriers, keep writes/shell
  serial, and emit/commit results in model request order.
- [ ] Add the interactive `workflow` tool and in-memory checklist state supporting
  create, start, complete, fail, skip, and clear; attach tool/subagent activity to the
  active item and reject unknown actions.
- [ ] Add the interactive `agent` tool with fresh child transcripts, no nested agent
  tool, event attribution, and supervisor limits (20 children/session, 15 minutes each).
- [ ] Support `work` children with serial coding tools and `inspect` children with only
  parallel-safe `read_file`, `grep`, and `glob`.
- [ ] Activate and validate `subagents.provider`/`subagents.model`; otherwise children
  follow the coordinator's current backend while running children retain snapshots.
- [ ] Support bounded retries for child execution failures without pretending to judge
  whether a child's answer is correct.
- [ ] Test concurrency limits, ordering, approval barriers, cancellation, child budgets,
  capability filtering, backend switching, event attribution, and supervisor cleanup.

## Phase 4 - Interactive terminal experience

- [ ] Choose and document a cross-platform Python TUI architecture (after a small
  Windows/Linux spike) with an event-driven UI separated from the core agent loop.
- [ ] Replace the blocking REPL with a responsive transcript/composer UI: styled
  Markdown, scrolling, multiline editing, history, paste handling, Unicode-aware
  layout, selectable text, status line, elapsed time, cwd, branch, provider, and model.
- [ ] Render live tool activity, concise completed receipts, errors, parallel groups,
  workflow progress, and a visible subagent tree.
- [ ] Implement `output.verbose`: concise mode by default; hide routine lines such as
  `-> glob` and `-> read_file`; verbose mode shows full call/result cards. Errors and
  direct shell output remain visible.
- [ ] Add slash-command autocomplete/help, configurable phase labels, a searchable
  model picker, and an `@` file picker rooted at the effective startup directory.
- [ ] Add active-turn steering at safe tool boundaries and one queued follow-up, with
  rejected/unapplied input restored to the composer.
- [ ] Add escape-to-interrupt and safe quit: cancel the active turn, wait for a valid
  transcript and session save, allow retry after save failure, and reserve a second
  interrupt for forced exit.
- [ ] Make `/clear` cancel pending activity and reset messages, usage, workflow,
  attachments, tool UI, subagent UI, steering, and queued input without changing the
  current backend or workspace.
- [ ] Add TUI unit/golden tests plus responsiveness and large-transcript performance
  budgets; keep a basic non-TUI fallback for unsupported terminals.

## Phase 5 - Skills, headless mode, sessions, and CLI polish

- [ ] Add one-shot skill invocation with `neo run --skill repo-summary`, generalized
  as `neo run --skill <name>`, with arguments, unknown-skill errors, feature-flag
  behavior, stdin composition, display text, and JSON-mode tests.
- [ ] Report malformed/unreadable skills and AGENTS.md files cleanly. Preserve project
  skill precedence and layered AGENTS.md order; warn and continue where safe.
- [ ] Improve saved-session labels: generate one very short sentence summarizing each
  conversation, and replace long numeric/hex IDs with collision-safe short
  alphanumeric IDs. Support old `sess_<hex>` IDs indefinitely.
- [ ] Make session search report corrupt/unreadable-session warnings rather than
  silently skipping them, and keep Unicode-safe titles/excerpts.
- [ ] Make session saving resilient after every completed send and on quit. Surface
  failures without discarding the live conversation, and allow retry.
- [ ] Keep provider-specific opaque data, image blocks, tool history, usage, visible
  phase/skill invocations, cwd, provider, model, and auth mode compatible on resume.
- [ ] Improve `neo doctor` with independent checks even after config failure, portable
  Python/launcher diagnostics, writable session/auth checks, and no secret disclosure.
- [ ] Add build-derived version metadata and consistent CLI parsing/error/exit-code
  tests across `chat`, `run`, `sessions`, `doctor`, `resume`, `login`, and `logout`.

## Phase 6 - Observability, packaging, documentation, and release parity

- [ ] Add opt-in structured debug logging with separate payload controls and recursive
  credential/token/API-key redaction for every provider and custom profile.
- [ ] Add architecture, CLI, configuration, provider, agent-loop, compaction, session,
  skills/phases, tools, permissions, and TUI developer docs kept in sync with behavior.
- [ ] Add a changelog, contribution commands, type checking, linting, formatting,
  coverage thresholds, and focused performance regression tests.
- [ ] Add CI for supported Python versions on Windows, Linux, and macOS, including
  packaging/install smoke tests and console-encoding tests.
- [ ] Produce versioned wheel/sdist releases and a verified portable installation path
  so users do not need an editable checkout. Document where the `neo.exe` launcher and
  source package live on Windows.
- [ ] Decide whether release channels, installer checksum verification, generated docs
  site, and nightly builds are required for full project-level parity; implement and
  document the accepted scope.

## Implementation log

| Date | Completed capability | Evidence | Verification |
| --- | --- | --- | --- |
| 2026-08-06 | Atomic tool-turn transcripts and explicit provider stop outcomes | `src/neo/agent.py`, `tests/test_agent.py` | Full suite: 37 passed |
| 2026-08-06 | Atomic, mode-preserving file writes and edits | `src/neo/tools.py`, `tests/test_tools.py` | Full suite: 37 passed |
| 2026-08-06 | Bounded and pageable `read_file` behavior | `src/neo/tools.py`, `tests/test_tools.py` | Full suite: 37 passed |
| 2026-08-06 | Normalize Chat Completions `stop`, `length`, and tool-call finish reasons | `src/neo/providers.py`, `tests/test_providers.py` | Full suite: 39 passed |
| 2026-08-06 | Add host/native-shell context, read-only action policy, dependency policy, approval guidance, and cross-shell tests | `src/neo/context.py`, `src/neo/tools.py`, `tests/test_context.py`, `tests/test_tools.py`, `README.md`, `neo.yaml.example` | Full suite: 44 passed |
| 2026-08-07 | Add typed agent events with commentary/text separation, call identity, timing, and structured failures | `src/neo/agent.py`, `src/neo/cli.py`, `tests/test_agent.py` | Full suite: 45 passed; Windows help/version smoke passed |
| 2026-08-07 | Propagate cooperative cancellation and deadlines through the core loop, provider I/O/retries, tools, and headless mode; preserve cancelled tool transcripts and terminate shell process trees | `src/neo/cancellation.py`, `src/neo/agent.py`, `src/neo/providers.py`, `src/neo/tools.py`, `src/neo/cli.py`, `tests/test_agent.py`, `tests/test_providers.py`, `tests/test_tools.py`, `tests/test_cli.py`, `README.md` | Full suite: 49 passed; compileall and Windows help/version/shell-cancellation smoke passed |
| 2026-08-07 | Convert interactive SIGINT into active-turn cancellation, restore idle Ctrl-C behavior, save valid cancelled transcripts, and retain live state after save failures | `src/neo/cancellation.py`, `src/neo/cli.py`, `tests/test_cli.py`, `README.md` | Full suite: 51 passed; compileall and Windows help/version smoke passed |
| 2026-08-07 | Add safe structured Git tools with native/Dulwich backends, automatic no-binary fallback, local-only check-ins, explicit pushes, doctor integration, configuration, and packaging | `src/neo/git.py`, `src/neo/git_tools.py`, `src/neo/tools.py`, `src/neo/config.py`, `src/neo/context.py`, `src/neo/cli.py`, `tests/test_git.py`, `tests/test_config.py`, `tests/test_context.py`, `tests/test_cli.py`, `tests/test_tools.py`, `pyproject.toml`, `README.md`, `neo.yaml.example`, `docs/designs/git-integration.md` | Full suite: 66 passed; compileall, pip check, wheel build, native doctor, native/Dulwich commits, and Dulwich local-remote push passed on Windows |

## Final parity gate

- [ ] Run the complete Python suite on Windows, Linux, and macOS; run provider contract
  tests without live credentials; then perform opt-in live smoke tests for each auth
  path/provider.
- [ ] Exercise create/edit/test, long-session compaction, resume/fallback, images,
  parallel inspection, writable subagents, workflow, steering, cancellation, verbose
  mode, headless JSON, and one-shot skills in end-to-end tests.
- [ ] Re-audit against the then-current Go implementation and close or explicitly
  document every remaining difference before declaring feature parity.
