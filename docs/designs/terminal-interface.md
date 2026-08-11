# Terminal interface architecture

Status: accepted first slice, 2026-08-07.

HAL uses Textual for its full-screen interactive interface and retains the original
line-oriented REPL for redirected I/O, limited terminals, troubleshooting, and
accessibility. `hal` and `hal chat` select the TUI when stdin and stdout are terminals;
`hal chat --no-tui` always selects the basic interface, and `HAL_NO_TUI=1` disables
automatic TUI selection.

The provider-neutral `Agent` remains independent of Textual. Its synchronous send loop
runs in a Textual thread worker, and typed `Event` objects are marshalled to the UI
thread with `call_from_thread`. Provider adapters normalize native SSE formats into
shared text/commentary deltas while accumulating the complete response, tool calls,
stop reason, and usage needed by the agent transcript. This keeps provider calls and
tools from blocking input, scrolling, elapsed-time updates, or cancellation. The same
cooperative cancellation token reaches providers and tools and closes an active HTTP
stream when cancelled.

The first slice provides:

- a scrollable Markdown transcript whose active response card updates incrementally,
  plus restored session history;
- a multiline composer with portable terminal controls (`Enter` sends;
  `F3`, `Shift+Enter`, or the New line button inserts a newline; `F2` and
  `Ctrl+Enter` are additional send paths; Windows-reserved `Alt+Enter` is avoided);
- copy buttons on assistant response cards that copy the original Markdown source;
- concise tool receipts, with full calls/results when `output.verbose` is enabled;
- project, Git branch, provider/model, session, and elapsed-work status;
- `/help`, `/sessions`, `/resume`, `/clear`, `/model`, `/exit`, named phases, skills,
  and direct `!command` execution;
- modal tool approvals, active-turn cancellation, and save-before-quit behavior.
- one randomized startup quotation from the shared `hal.sayings` catalog in both
  the TUI and basic REPL, without changing headless output.

Quit during active work requests cancellation and waits for the worker to restore agent
invariants and save the session. A failed save leaves the application open so the user
can retry. Streaming rejection falls back only before any stream data is consumed;
successful buffered JSON returned to a streaming request is reused without duplicating
the model call. `features.streaming: false` forces buffered interactive responses, and
headless runs remain buffered. Later slices will add command/model/file pickers,
history navigation, steering, workflow/subagent views, and performance/golden tests.
