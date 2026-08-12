# TUI input reliability tasks

This checklist tracks unresolved composer/input problems in `src/hal/tui.py`.
Implementation and automated-test items are checked when their focused tests pass;
terminal-dependent acceptance stays unchecked until a real-terminal smoke test.
These issues have survived earlier attempted fixes, so broader compatibility work
must be based on observed input events rather than another binding-only change.

## Priority 0 — reproduce before changing bindings

- [ ] Record the affected OS, terminal application, shell, `$TERM`, Textual version,
  and whether the terminal supports the Kitty keyboard protocol or CSI-u.
- [ ] Add a temporary opt-in key/paste diagnostic that records event names, encoded
  key identity, modifiers, paste-event count, and pasted character count without
  recording the user's actual text.
- [ ] Capture what HAL receives for `Enter`, `Ctrl+Enter`, `Shift+Enter`, `F2`, `F3`,
  a short paste, and a large multiline paste in the affected terminal.
- [ ] Build a terminal matrix covering at least Windows Terminal/PowerShell, a Linux
  terminal/Bash, and Textual's test driver. Document terminals that cannot distinguish
  `Ctrl+Enter` from `Enter` at the protocol level.

## Priority 0 — dependable keyboard newline

Ctrl+Enter proved indistinguishable from Enter in common terminals. HAL therefore
uses Ctrl+J, whose LF encoding remains distinct from plain Enter's CR encoding.

- [x] Set the product contract to `Enter`/`F2` = send and
  `Ctrl+J`/`Shift+Enter`/`F3`/Newline button = insert newline.
- [x] Remove all Ctrl+Enter bindings and visible guidance.
- [x] Bind Ctrl+J to `action_insert_newline` with priority over widget defaults.
- [x] Update the visible composer hint and terminal-interface documentation to match
  the final behavior.
- [x] Add a Textual test that positions the cursor in the middle of text, presses
  `Ctrl+J`, and verifies a newline is inserted at that position without submitting.
- [x] Add a regression test proving plain `Enter` and `F2` still submit exactly once.
- [ ] Verify the mapping manually in every terminal from the reproduction matrix.

### Keyboard-newline acceptance criteria

1. `Ctrl+J` inserts one newline at the cursor and does not start an agent turn.
2. Plain `Enter` submits the complete composer contents exactly once.
3. Unsupported terminals retain working, visible newline alternatives.
4. Tests, bindings, visible hints, and documentation do not advertise Ctrl+Enter.

## Priority 0 — large paste must be complete and responsive

Textual already enables bracketed paste and delivers an `events.Paste` event to
`TextArea`. The failure could therefore occur in the terminal protocol, input parser,
widget insertion/rendering, or HAL's submission path; measure each boundary.

- [x] Define reproducible paste fixtures up to 1 MiB containing
  multiline text, Unicode, tabs, and lines that begin with slash commands or `!`.
- [ ] Determine whether each failed paste arrives as one bracketed `Paste` event,
  several events, individual key events, truncated content, or no event.
- [x] Confirm automated round-trip tests preserve the exact payload before any
  rendering or submission logic runs.
- [x] Add a composer-level paste handler that keeps large payloads out of the render
  document while preserving the cursor position, selection replacement, Unicode,
  tabs, and newlines.
- [x] Ensure pasted newlines never trigger the app-level `Enter` submit binding.
- [x] Keep paste insertion off expensive per-character paths; update the document in
  one operation or bounded chunks and yield to the UI between large chunks if needed.
- [ ] Make the composer vertically scrollable or dynamically taller enough to inspect
  a large paste without destabilizing the transcript/footer layout.
- [x] Show non-destructive feedback for very large input (byte count in the compact
  marker and a completion notification).
- [x] Never impose a silent size limit. If an explicit safety limit becomes necessary,
  preserve the composer text and ask before truncating or rejecting anything.
- [x] Add automated paste tests asserting exact round-trip text through a 1 MiB fixture,
  no accidental submit, UI responsiveness, and successful later submission.
- [x] Test clipboard paste and terminal bracketed paste separately; they use different
  Textual paths.
- [ ] Verify large paste manually in every terminal from the reproduction matrix.

### Large-paste acceptance criteria

1. A 1 MiB multiline Unicode paste is either accepted byte-for-byte or rejected with
   an explicit message while leaving existing composer contents intact.
2. Pasting never submits, interprets `/commands`, or executes `!commands` until the
   user deliberately presses a send control.
3. The application remains cancellable and visibly responsive during insertion and
   rendering.
4. The exact pasted text reaches `Agent.send`; leading/trailing whitespace policy is
   documented and tested rather than being silently altered.

## Priority 1 — composer correctness discovered during investigation

- [x] Reconsider `text = composer.text.strip()` in `action_submit`: submitted pasted
  material now retains intentional leading and trailing whitespace.
- [ ] Preserve an unsent draft across recoverable UI errors and failed submission
  startup instead of clearing the composer first.
- [ ] Add explicit input-size and render-time telemetry to performance tests without
  logging input contents.
- [ ] Add a manual TUI smoke-test document that release checks can repeat on Windows,
  Linux, and macOS.

## Transcript selection and copying

- [x] Bind `Ctrl+Shift+C` to copy Textual's current mouse selection.
- [x] Use the native Windows clipboard API for selected transcript text rather than
  relying on terminal-native selection or OSC 52 support.
- [ ] Verify mouse selection and `Ctrl+Shift+C` in Windows Terminal.

## Verification log

- [x] Focused automated TUI tests pass.
- [ ] Full test suite passes.
- [ ] Windows terminal smoke test passes.
- [ ] Linux terminal smoke test passes.
- [x] Documentation and visible shortcut hints match tested behavior.
