from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Static, TextArea

from .agent import Agent, Event, EventKind
from .cancellation import CancelledError, CancellationToken
from .config import Config
from .context import expand_user_input
from .models import Message
from .providers import ProviderError
from .sayings import startup_saying
from .sessions import Session, SessionStore, short_session_id
from .tools import BashTool
from .workflows import WORKFLOWS, parse_workflow_command, run_workflow


_COLLAPSED_PASTE_CHARS = 8_192


class Composer(TextArea):
    """Text area that keeps large pasted payloads out of the render document."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pasted_blocks: dict[str, str] = {}
        self._paste_number = 0

    async def _on_paste(self, event: events.Paste) -> None:
        if self.read_only:
            return
        event.stop()
        event.prevent_default()
        self._insert_paste(event.text)

    def action_paste(self) -> None:
        """Paste from the native clipboard, retaining Textual's fallback."""
        if self.read_only:
            return
        try:
            text = read_windows_unicode() if os.name == "nt" else self.app.clipboard
        except OSError as exc:
            self.app.notify(f"Could not read clipboard: {exc}", severity="error")
            return
        if text:
            self._insert_paste(text)

    def _insert_paste(self, text: str) -> None:
        """Insert a small paste or a compact marker for a large payload."""
        if len(text) < _COLLAPSED_PASTE_CHARS:
            start, end = self.selection
            result = self.replace(text, start, end, maintain_selection_offset=False)
            self.move_cursor(result.end_location)
            self.focus()
            return
        self._paste_number += 1
        size = len(text.encode("utf-8"))
        marker = f"[Pasted block {self._paste_number} · {size:,} bytes]"
        # A single replacement keeps selection/cursor behavior and avoids an
        # expensive render/document update for every pasted character.
        start, end = self.selection
        result = self.replace(marker, start, end, maintain_selection_offset=False)
        self.move_cursor(result.end_location)
        self.focus()
        self._pasted_blocks[marker] = text
        self.post_message(self.LargePaste(self, marker, size))

    def expand_pastes(self, text: str) -> str:
        """Expand intact visible markers and discard this draft's paste storage."""
        if self._pasted_blocks:
            pattern = re.compile("|".join(
                re.escape(marker) for marker in self._pasted_blocks
            ))
            # Expand in one pass so text inside one payload cannot accidentally
            # be interpreted as another block's marker.
            text = pattern.sub(lambda match: self._pasted_blocks[match.group()], text)
        self._pasted_blocks.clear()
        return text

    @dataclass
    class LargePaste(TextArea.Changed):
        marker: str
        size: int

        @property
        def control(self) -> TextArea:
            return self.text_area


def copy_windows_unicode(text: str) -> None:
    """Copy arbitrary Unicode text with the native Windows clipboard API."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    encoded = (text + "\0").encode("utf-16-le")
    if not user32.OpenClipboard(None):
        raise ctypes.WinError(ctypes.get_last_error())
    memory = None
    transferred = False
    try:
        if not user32.EmptyClipboard():
            raise ctypes.WinError(ctypes.get_last_error())
        memory = kernel32.GlobalAlloc(0x0002, len(encoded))  # GMEM_MOVEABLE
        if not memory:
            raise ctypes.WinError(ctypes.get_last_error())
        destination = kernel32.GlobalLock(memory)
        if not destination:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(destination, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(memory)
        if not user32.SetClipboardData(13, memory):  # CF_UNICODETEXT
            raise ctypes.WinError(ctypes.get_last_error())
        transferred = True
    finally:
        if memory and not transferred:
            kernel32.GlobalFree(memory)
        user32.CloseClipboard()


def read_windows_unicode() -> str:
    """Read Unicode text from the native Windows clipboard."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t

    if not user32.IsClipboardFormatAvailable(13):  # CF_UNICODETEXT
        return ""
    if not user32.OpenClipboard(None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        memory = user32.GetClipboardData(13)
        if not memory:
            raise ctypes.WinError(ctypes.get_last_error())
        size = kernel32.GlobalSize(memory)
        source = kernel32.GlobalLock(memory)
        if not source:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(source, size).decode("utf-16-le").rstrip("\0")
        finally:
            kernel32.GlobalUnlock(memory)
    finally:
        user32.CloseClipboard()


class ConfirmScreen(ModalScreen[bool]):
    """A blocking-looking approval dialog whose result is consumed by a worker."""

    BINDINGS = [Binding("escape", "deny", "Deny", priority=True)]

    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-dialog { width: 72; height: auto; padding: 1 2; border: round $warning; background: $surface; }
    #confirm-prompt { height: auto; margin-bottom: 1; }
    #confirm-buttons { height: 3; align-horizontal: right; }
    #confirm-buttons Button { margin-left: 1; }
    """

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(self.prompt, id="confirm-prompt")
            with Horizontal(id="confirm-buttons"):
                yield Button("Deny", id="deny")
                yield Button("Allow", variant="warning", id="allow")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow")

    def action_deny(self) -> None:
        self.dismiss(False)


class AssistantResponse(Vertical):
    """An assistant Markdown response with its original text available to copy."""

    def __init__(self, text: str) -> None:
        super().__init__(classes="assistant-response transcript-entry")
        self.response_text = text
        self.body = Static(self._panel(text), classes="response-content")

    @staticmethod
    def _panel(text: str) -> Panel:
        return Panel(Markdown(text), title="HAL", border_style="green")

    def compose(self) -> ComposeResult:
        yield self.body
        yield Button("Copy", classes="copy-response")

    def update_response(self, text: str) -> None:
        self.response_text = text
        self.body.update(self._panel(text))


class HalTui(App[int]):
    """Responsive terminal front end over HAL's synchronous agent core."""

    TITLE = "HAL"
    SUB_TITLE = "coding agent"
    CSS = """
    Screen { layout: vertical; }
    #status { height: 1; padding: 0 1; color: $text-muted; background: $boost; }
    #transcript { height: 1fr; padding: 1 2; scrollbar-gutter: stable; }
    .transcript-entry { height: auto; margin-bottom: 1; }
    .assistant-response { height: auto; }
    .response-content { height: auto; }
    .copy-response { width: 8; min-width: 8; height: 1; min-height: 1; border: none; padding: 0 1; }
    #composer-frame { height: 8; border-top: solid $primary; padding: 0 1; }
    #composer { height: 6; border: none; background: $surface; }
    #composer-controls { height: 1; align-horizontal: right; }
    #composer-hint { width: 1fr; content-align: left middle; color: $text-muted; }
    #composer-controls Button { width: 10; min-width: 10; height: 1; min-height: 1; margin-left: 1; border: none; padding: 0 1; }
    #composer-controls #newline { width: 12; min-width: 12; }
    """

    BINDINGS = [
        Binding("enter", "submit", "Send", priority=True),
        # Ctrl+J is LF in terminal input and Textual exposes it distinctly from
        # plain Enter (CR), making it a dependable multiline shortcut.
        Binding("ctrl+j", "insert_newline", "", show=False, priority=True),
        Binding("f2", "submit", "Send", priority=True),
        Binding("f3", "insert_newline", "New line", priority=True),
        Binding("shift+enter", "insert_newline", "", show=False, priority=True),
        Binding("ctrl+c", "cancel_turn", "Cancel", priority=True),
        Binding("ctrl+shift+c", "copy_selection", "Copy selection", priority=True),
        Binding("escape", "cancel_turn", "Cancel", priority=True),
        Binding("ctrl+q", "safe_quit", "Quit", priority=True),
        Binding("ctrl+l", "clear_conversation", "Clear", priority=True),
    ]

    def __init__(
        self,
        agent: Agent,
        config: Config,
        cwd: Path,
        session: Session,
        store: SessionStore,
        skills: list,
        phases: dict,
        *,
        branch: str = "-",
        session_factory: Callable[[Path, Session], tuple[Config, Agent, list, dict, str]] | None = None,
    ) -> None:
        super().__init__()
        self.agent = agent
        self.config = config
        self.cwd = cwd
        self.session = session
        self.store = store
        self.skills = skills
        self.phases = phases
        self.branch = branch or "-"
        self.session_factory = session_factory
        self.cancellation: CancellationToken | None = None
        self.busy = False
        self.quit_when_idle = False
        self.turn_started = 0.0
        self.tool_names: dict[str, str] = {}
        self.response_text = ""
        self.response_widget: AssistantResponse | None = None
        self.commentary_text = ""
        self.commentary_widget: Static | None = None
        self.startup_saying = startup_saying()
        self.agent.on_event = self._event_from_worker

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield VerticalScroll(id="transcript")
        with Vertical(id="composer-frame"):
            yield Composer(soft_wrap=True, placeholder="Ask HAL…", id="composer")
            with Horizontal(id="composer-controls"):
                yield Static("Enter/F2 send · Ctrl+J new line", id="composer-hint")
                yield Button("Newline", id="newline")
                yield Button("Cancel", id="cancel", disabled=True)
                yield Button("Send", id="send", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._render_history()
        self._write(Text(f"“{self.startup_saying}”", style="italic cyan"))
        self._update_status()
        self.set_interval(0.25, self._update_status)
        self.query_one("#composer", TextArea).focus()

    def _render_history(self) -> None:
        if not self.session.messages:
            self._write(Text(
                "HAL is ready. Enter sends; Ctrl+J adds a line; Shift+drag selects "
                "text; Ctrl+Shift+C copies; Ctrl-C or Escape cancels active work.",
                style="dim",
            ))
            return
        for message in self.session.messages:
            self._render_message(message)

    def _render_message(self, message: Message) -> None:
        if message.role == "user":
            text = message.display_text or "\n".join(
                block.text for block in message.content if block.type == "text"
            )
            if text:
                self._write(Panel(Markdown(text), title="You", border_style="cyan"))
        elif message.role == "assistant":
            for block in message.content:
                if block.type == "text" and block.text:
                    self._write_response(block.text)

    def _write(self, content: object) -> Static:
        transcript = self.query_one("#transcript", VerticalScroll)
        widget = Static(content, classes="transcript-entry")
        transcript.mount(widget)
        transcript.scroll_end(animate=False)
        return widget

    def _write_response(self, text: str) -> AssistantResponse:
        transcript = self.query_one("#transcript", VerticalScroll)
        widget = AssistantResponse(text)
        transcript.mount(widget)
        transcript.scroll_end(animate=False)
        return widget

    def _clear_transcript(self) -> None:
        self.response_text = ""
        self.response_widget = None
        self.commentary_text = ""
        self.commentary_widget = None
        self.query_one("#transcript", VerticalScroll).remove_children()

    def _finish_response_card(self) -> None:
        self.response_text = ""
        self.response_widget = None

    def _finish_commentary_card(self) -> None:
        self.commentary_text = ""
        self.commentary_widget = None

    def _update_status(self) -> None:
        elapsed = ""
        if self.busy:
            elapsed = f" · working {time.monotonic() - self.turn_started:.1f}s"
        project = self.cwd.name or str(self.cwd)
        session = short_session_id(self.session.metadata.id)
        self.query_one("#status", Static).update(
            f"{self.config.provider}/{self.agent.model} · {project} · {self.branch} · {session}{elapsed}"
        )

    def _set_busy(self, value: bool) -> None:
        self.busy = value
        self.query_one("#send", Button).disabled = value
        self.query_one("#cancel", Button).disabled = not value
        self._update_status()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            self.action_submit()
        elif event.button.id == "newline":
            self.action_insert_newline()
        elif event.button.id == "cancel":
            self.action_cancel_turn()
        elif event.button.has_class("copy-response"):
            response = event.button.parent
            if isinstance(response, AssistantResponse):
                self._copy_response(response.response_text)

    def _copy_response(self, text: str) -> None:
        self._copy_text(text, "Response Markdown copied to clipboard.")

    def _copy_text(self, text: str, success: str) -> None:
        """Copy through the native Windows API or Textual's terminal clipboard."""
        try:
            if os.name == "nt":
                copy_windows_unicode(text)
            else:
                self.copy_to_clipboard(text)
        except OSError as exc:
            self.notify(f"Could not copy response: {exc}", severity="error")
            return
        self.notify(success)

    def action_copy_selection(self) -> None:
        """Copy Textual's current mouse selection without terminal interception."""
        selected = self.screen.get_selected_text()
        if not selected:
            self.notify("Select transcript text first.")
            return
        self._copy_text(selected, "Selected text copied to clipboard.")

    def action_submit(self) -> None:
        if isinstance(self.screen, ConfirmScreen):
            return
        if self.busy:
            self.notify("HAL is already working; cancel the turn before sending another message.")
            return
        composer = self.query_one("#composer", Composer)
        display = composer.text
        if not display.strip():
            return
        composer.clear()
        if self._handle_command(display.strip()):
            composer.expand_pastes("")
            return
        text = composer.expand_pastes(display)
        self._write(Panel(Markdown(display), title="You", border_style="cyan"))
        self._start_turn(text)

    def on_composer_large_paste(self, event: Composer.LargePaste) -> None:
        self.notify(f"Stored large paste as {event.marker}")

    def action_insert_newline(self) -> None:
        if isinstance(self.screen, ConfirmScreen):
            return
        composer = self.query_one("#composer", TextArea)
        composer.insert("\n")
        composer.focus()

    def _handle_command(self, text: str) -> bool:
        if text in {"/exit", "/quit"}:
            self.action_safe_quit()
            return True
        if text == "/help":
            phase_names = ", ".join(f"/{name}" for name in self.phases)
            skill_names = ", ".join(f"/{item.name}" for item in self.skills)
            extras = ", ".join(value for value in (phase_names, skill_names) if value)
            self._write(Text(
                "Commands: /help, /workflows, /workflow <name> <request>, "
                "/sessions, /resume <short-id>, /clear, /model <id>, /exit"
                + (f" · phases/skills: {extras}" if extras else ""),
                style="bold",
            ))
            return True
        if text == "/workflows":
            lines = "\n".join(
                f"{workflow.name}  {' -> '.join(workflow.phases)}  {workflow.description}"
                for workflow in WORKFLOWS.values()
            )
            self._write(Text(lines, style="cyan"))
            return True
        if text == "/clear":
            self.action_clear_conversation()
            return True
        if text == "/sessions":
            try:
                items = self.store.list()
                lines = ["Saved sessions:"] + [
                    f"{short_session_id(item.id)}  {item.updated_at[:16].replace('T', ' ')}  "
                    f"{item.model.rsplit('/', 1)[-1] or '-'}  {Path(item.cwd).name or '-'}"
                    for item in items
                ]
                self._write(Text("\n".join(lines if items else ["No saved sessions."])))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._write(Text(f"sessions: {exc}", style="bold red"))
            return True
        if text == "/resume" or text.startswith("/resume "):
            selector = text[7:].strip()
            if not selector:
                self._write(Text("usage: /resume <short-id-or-full-id>", style="yellow"))
            else:
                self._resume(selector)
            return True
        if text.startswith("/model "):
            model = text[7:].strip()
            if model:
                self.agent.model = model
                self.session.metadata.model = model
                self._write(Text(f"Model: {model}", style="bold"))
                self._save_session()
                self._update_status()
            return True
        return False

    def _resume(self, selector: str) -> None:
        if self.session_factory is None:
            self._write(Text("Session resume is unavailable in this interface.", style="yellow"))
            return
        try:
            target = self.store.load(selector)
            if target.metadata.id == self.session.metadata.id:
                self.notify("That session is already active.")
                return
            if not self._save_session():
                return
            saved = Path(target.metadata.cwd)
            target_cwd = saved if saved.is_dir() else self.cwd
            cfg, agent, skills, phases, branch = self.session_factory(target_cwd, target)
            agent.on_event = self._event_from_worker
            if hasattr(agent.tools, "confirm"):
                agent.tools.confirm = self.confirm_tool
            self.agent, self.skills, self.phases = agent, skills, phases
            self.config, self.cwd, self.session = cfg, target_cwd, target
            self.branch = branch
            self._clear_transcript()
            self._render_history()
            self._write(Text(f"Resumed {short_session_id(target.metadata.id)}.", style="bold green"))
            self._update_status()
        except (OSError, ValueError, json.JSONDecodeError, ProviderError) as exc:
            self._write(Text(f"resume: {exc}", style="bold red"))

    def _start_turn(self, text: str) -> None:
        self.cancellation = CancellationToken()
        self.turn_started = time.monotonic()
        self._set_busy(True)
        if text.startswith("!"):
            self._run_shell(text[1:].strip(), self.cancellation)
        else:
            try:
                parsed = parse_workflow_command(text)
            except ValueError as exc:
                self._write_error(str(exc), "yellow")
                self._finish_turn()
                return
            if parsed:
                workflow, request = parsed
                self._run_workflow(workflow, request, self.cancellation)
            else:
                expanded, display = expand_user_input(text, self.skills, self.phases)
                self._run_agent(expanded, display, self.cancellation)

    @work(thread=True, exclusive=True, group="turn")
    def _run_workflow(self, workflow, request: str, cancellation: CancellationToken) -> None:
        try:
            run_workflow(
                self.agent, workflow, request, self.phases, cancellation,
                lambda index, total, name: self.call_from_thread(
                    self._write,
                    Text(
                        f"Workflow {workflow.name} · {index}/{total} · {name}",
                        style="bold cyan",
                    ),
                ),
            )
        except CancelledError as exc:
            self.call_from_thread(self._write_error, f"Interrupted: {exc}", "yellow")
        except (ProviderError, OSError, ValueError, RuntimeError) as exc:
            self.call_from_thread(self._write_error, f"Workflow: {exc}", "bold red")
        finally:
            self.call_from_thread(self._finish_turn)

    @work(thread=True, exclusive=True, group="turn")
    def _run_agent(self, expanded: str, display: str, cancellation: CancellationToken) -> None:
        try:
            self.agent.send(expanded, display, cancellation)
        except CancelledError as exc:
            self.call_from_thread(self._write_error, f"Interrupted: {exc}", "yellow")
        except (ProviderError, OSError, ValueError, RuntimeError) as exc:
            self.call_from_thread(self._write_error, f"Error: {exc}", "bold red")
        finally:
            self.call_from_thread(self._finish_turn)

    @work(thread=True, exclusive=True, group="turn")
    def _run_shell(self, command: str, cancellation: CancellationToken) -> None:
        try:
            result = BashTool(self.cwd).run({"command": command}, cancellation)
            self.call_from_thread(
                self._write,
                Panel(Text(result or "(no output)"), title="Shell", border_style="magenta"),
            )
        except CancelledError as exc:
            self.call_from_thread(self._write_error, f"Interrupted: {exc}", "yellow")
        except (OSError, ValueError, RuntimeError) as exc:
            self.call_from_thread(self._write_error, f"Shell: {exc}", "bold red")
        finally:
            self.call_from_thread(self._finish_turn)

    def _event_from_worker(self, event: Event) -> None:
        self.call_from_thread(self._render_event, event)

    def _render_event(self, event: Event) -> None:
        if event.kind == EventKind.ASSISTANT_TEXT and event.text:
            self._finish_commentary_card()
            self.response_text += event.text
            if self.response_widget is None:
                self.response_widget = self._write_response(self.response_text)
            else:
                self.response_widget.update_response(self.response_text)
                self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)
        elif event.kind == EventKind.ASSISTANT_COMMENTARY and event.text:
            self.commentary_text += event.text
            content = Text(self.commentary_text, style="italic dim")
            if self.commentary_widget is None:
                self.commentary_widget = self._write(content)
            else:
                self.commentary_widget.update(content)
        elif event.kind == EventKind.TOOL_CALL:
            self._finish_response_card()
            self._finish_commentary_card()
            self.tool_names[event.tool_use_id] = event.name
            if self.config.verbose:
                self._write(Panel(Text(json.dumps(event.args, indent=2)), title=f"→ {event.name}", border_style="blue"))
        elif event.kind == EventKind.TOOL_RESULT:
            name = event.name or self.tool_names.pop(event.tool_use_id, "tool")
            if event.is_error:
                self._write(Panel(Text(event.text), title=f"✗ {name}", border_style="red"))
            elif self.config.verbose:
                self._write(Panel(Text(event.text or "(no output)"), title=f"✓ {name} · {event.duration_ms}ms", border_style="green"))
            else:
                self._write(Text(f"✓ {name} · {event.duration_ms}ms", style="dim green"))
        elif event.kind == EventKind.VERIFICATION_STARTED:
            self._finish_response_card()
            self._finish_commentary_card()
            self._write(Text(f"Verifying: {event.name}", style="dim cyan"))
        elif event.kind == EventKind.VERIFICATION_FINISHED:
            status = event.verification.status.value if event.verification else (
                "failed" if event.is_error else "passed"
            )
            style = "red" if event.is_error else "green"
            self._write(Text(
                f"{'✗' if event.is_error else '✓'} {event.name} · "
                f"{status} · {event.duration_ms}ms",
                style=style,
            ))
            if self.config.verbose and event.text:
                self._write(Panel(
                    Text(event.text), title=f"Verification · {event.name}",
                    border_style=style,
                ))
        elif event.kind == EventKind.REPAIR_STARTED:
            attempt = event.args.get("attempt", "?")
            self._write(Text(f"Repair attempt {attempt} started", style="bold yellow"))
            if self.config.verbose and event.text:
                self._write(Panel(
                    Text(event.text), title="Verification failures",
                    border_style="yellow",
                ))
        elif event.kind == EventKind.BUDGET_UPDATED and self.config.verbose:
            self._write(Text(
                f"Budget usage · providers {event.provider_calls} · tools "
                f"{event.tool_calls} · tokens {event.input_tokens} in / "
                f"{event.output_tokens} out",
                style="dim",
            ))
        elif event.kind == EventKind.RUN_FINISHED:
            style = "green" if event.status == "succeeded" else "red"
            detail = f" · {event.reason}" if event.reason else ""
            self._write(Text(
                f"Run {event.status}{detail} · {event.run_id}", style=f"dim {style}",
            ))
        elif event.kind in {EventKind.DONE, EventKind.ERROR, EventKind.MAX_TURNS_REACHED}:
            self._finish_response_card()
            self._finish_commentary_card()

    def _write_error(self, message: str, style: str) -> None:
        self._write(Text(message, style=style))

    def _finish_turn(self) -> None:
        saved = self._save_session()
        self.cancellation = None
        self._set_busy(False)
        if self.quit_when_idle and saved:
            self.exit(0)
        else:
            if self.quit_when_idle:
                self.quit_when_idle = False
                self.notify("Session save failed; fix the error and quit again.", severity="error")
            self.query_one("#composer", TextArea).focus()

    def _save_session(self) -> bool:
        self.session.messages, self.session.usage = self.agent.messages, self.agent.usage
        try:
            self.store.save(self.session)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._write_error(f"Could not save session: {exc}", "bold red")
            return False
        return True

    def confirm_tool(self, prompt: str) -> bool:
        """Ask on the UI thread and synchronously return the result to a tool worker."""
        completed = threading.Event()
        answer = False

        def receive(result: bool | None) -> None:
            nonlocal answer
            answer = bool(result)
            completed.set()

        self.call_from_thread(self.push_screen, ConfirmScreen(prompt), receive)
        while not completed.wait(0.1):
            if self.cancellation:
                try:
                    self.cancellation.raise_if_cancelled()
                except CancelledError:
                    return False
        return answer

    def action_cancel_turn(self) -> None:
        if self.cancellation is None:
            self.notify("No active turn to cancel.")
            return
        self.cancellation.cancel("operation interrupted by user")
        self.notify("Cancelling active work…")

    def action_clear_conversation(self) -> None:
        if self.busy:
            self.notify("Cancel the active turn before clearing the conversation.", severity="warning")
            return
        self.agent.messages.clear()
        self.agent.usage = type(self.agent.usage)()
        self._clear_transcript()
        self._write(Text("Conversation cleared.", style="dim"))
        self._save_session()

    def action_safe_quit(self) -> None:
        if self.busy:
            self.quit_when_idle = True
            self.action_cancel_turn()
            self.notify("HAL will exit after cancellation and session save.")
            return
        if self._save_session():
            self.exit(0)


def run_tui(
    agent: Agent,
    config: Config,
    cwd: Path,
    session: Session,
    store: SessionStore,
    skills: list,
    phases: dict,
    *,
    branch: str = "-",
    session_factory: Callable[[Path, Session], tuple[Config, Agent, list, dict, str]] | None = None,
) -> int:
    app = HalTui(
        agent, config, cwd, session, store, skills, phases,
        branch=branch, session_factory=session_factory,
    )
    if hasattr(agent.tools, "confirm"):
        agent.tools.confirm = app.confirm_tool
    result = app.run()
    return int(result or 0)
