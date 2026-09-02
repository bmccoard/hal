from __future__ import annotations

import asyncio
import ctypes
from pathlib import Path

import pytest
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual import events
from textual.widgets import Button, Footer

from hal.agent import Agent, Event, EventKind
from hal.cancellation import CancellationToken
from hal.config import Config
from hal.models import ContentBlock, Message, Request, Response
from hal.providers import Provider
from hal.sayings import HAL_SAYINGS
from hal.sessions import Metadata, SessionStore
from hal.tools import Registry
from hal.tui import (
    _MAX_TRANSCRIPT_ENTRIES,
    AssistantResponse,
    Composer,
    ConfirmScreen,
    HalTui,
)


class FakeProvider(Provider):
    name = "test"

    def complete(
        self, request: Request, cancellation: CancellationToken | None = None,
    ) -> Response:
        raise AssertionError("FakeProvider should not be called by TUI tests")


class FakeAgent(Agent):
    def __init__(self) -> None:
        super().__init__(FakeProvider(), "test-model", "system", Registry([]))
        self.prompts: list[tuple[str, str]] = []

    def send(
        self,
        text: str,
        display_text: str = "",
        cancellation: CancellationToken | None = None,
        **_kwargs: object,
    ) -> str:
        self.prompts.append((text, display_text))
        self.messages.append(
            Message("user", [ContentBlock("text", text=text)], display_text)
        )
        return ""


def _app(tmp_path: Path) -> tuple[HalTui, FakeAgent]:
    config = Config(model="test-model")
    store = SessionStore(directory=tmp_path / "sessions")
    session = store.create(Metadata(
        cwd=str(tmp_path), provider=config.provider, model=config.model,
    ))
    agent = FakeAgent()
    app = HalTui(
        agent, config, tmp_path, session, store, [], {}, branch="main",
    )
    return app, agent


def test_tui_layout_has_paste_button_next_to_newline(tmp_path: Path) -> None:
    app, _agent = _app(tmp_path)

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)):
            assert app.startup_saying in HAL_SAYINGS
            composer = app.query_one("#composer", Composer)
            assert composer.placeholder == "Ask HAL…"
            assert str(app.query_one("#composer-hint").render()) == (
                "Enter/F2 send · Ctrl+J new line · F4 paste"
            )
            assert app.query_one("#paste", Button).label.plain == "Paste"
            assert app.query_one("#newline", Button).label.plain == "Newline"
            buttons = list(app.query("#composer-controls Button"))
            assert buttons.index(app.query_one("#paste")) + 1 == buttons.index(
                app.query_one("#newline")
            )
            footer = app.query_one(Footer)
            controls = app.query_one("#composer-controls")
            assert controls.region.bottom <= footer.region.y

    asyncio.run(scenario())


def test_paste_button_reads_native_windows_clipboard(monkeypatch, tmp_path: Path) -> None:
    app, agent = _app(tmp_path)

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            monkeypatch.setattr("hal.tui.os.name", "nt")
            monkeypatch.setattr(
                "hal.tui.read_windows_unicode", lambda: "clipboard ✓",
            )
            await pilot.click("#paste")

            assert app.query_one("#composer", Composer).text == "clipboard ✓"
            assert agent.prompts == []

    asyncio.run(scenario())


def test_f4_reads_native_windows_clipboard(monkeypatch, tmp_path: Path) -> None:
    app, _agent = _app(tmp_path)

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            monkeypatch.setattr("hal.tui.os.name", "nt")
            monkeypatch.setattr("hal.tui.read_windows_unicode", lambda: "via F4")
            await pilot.press("f4")

            assert app.query_one("#composer", Composer).text == "via F4"

    asyncio.run(scenario())


def test_windows_clipboard_reader_preserves_unicode(monkeypatch) -> None:
    from hal.tui import read_windows_unicode

    encoded = "pasted → data ✓\0".encode("utf-16-le")
    memory = ctypes.create_string_buffer(encoded)

    class Function:
        def __init__(self, callback):
            object.__setattr__(self, "callback", callback)

        def __call__(self, *args):
            return self.callback(*args)

    class User32:
        IsClipboardFormatAvailable = Function(lambda _format_id: 1)
        OpenClipboard = Function(lambda _owner: 1)
        GetClipboardData = Function(lambda _format_id: memory)
        CloseClipboard = Function(lambda: 1)

    class Kernel32:
        GlobalSize = Function(lambda _memory: len(encoded))
        GlobalLock = Function(lambda value: ctypes.addressof(value))
        GlobalUnlock = Function(lambda _memory: 1)

    libraries = iter([User32(), Kernel32()])
    monkeypatch.setattr(
        "hal.tui.ctypes.WinDLL", lambda *_args, **_kwargs: next(libraries),
        raising=False,
    )

    assert read_windows_unicode() == "pasted → data ✓"


def test_small_paste_stays_editable_in_composer(tmp_path: Path) -> None:
    app, agent = _app(tmp_path)
    pasted = "first\n/this-is-data\n!also-data\nlast"

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            composer = app.query_one("#composer", Composer)
            composer.post_message(events.Paste(pasted))
            await pilot.pause()
            assert composer.text == pasted
            assert agent.prompts == []

    asyncio.run(scenario())


def test_large_paste_is_inserted_only_after_confirmation(tmp_path: Path) -> None:
    app, agent = _app(tmp_path)
    pasted = ("Unicode → data ✓\n" * 400) + "tail"

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            composer = app.query_one("#composer", Composer)
            composer.text = "Before\nAfter"
            composer.cursor_location = (1, 0)
            composer.post_message(events.Paste(pasted))
            await pilot.pause()

            assert isinstance(app.screen, ConfirmScreen)
            assert composer.text == "Before\nAfter"
            prompt = str(app.screen.query_one("#confirm-prompt").render())
            assert prompt == (
                "You are about to paste text that is longer than 5 KiB. "
                "Do you wish to continue?"
            )

            app.screen.dismiss(True)
            await pilot.pause()
            assert app.screen is app.screen_stack[0]
            assert composer.text.startswith("Before\n[Pasted block 1 · ")
            assert composer.text.endswith(" bytes]After")
            assert composer.expand_pastes(composer.text) == f"Before\n{pasted}After"
            assert agent.prompts == []

    asyncio.run(scenario())


def test_declining_large_paste_leaves_composer_unchanged(tmp_path: Path) -> None:
    app, _agent = _app(tmp_path)
    pasted = "界" * 2_000
    assert len(pasted) < 5_120
    assert len(pasted.encode("utf-8")) > 5_120

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            composer = app.query_one("#composer", Composer)
            composer.text = "keep me"
            composer.post_message(events.Paste(pasted))
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            assert composer.text == "keep me"

            app.screen.action_deny()
            await pilot.pause()
            assert composer.text == "keep me"
            assert composer._pasted_blocks == {}

    asyncio.run(scenario())


def test_stream_events_are_coalesced_before_reaching_ui(monkeypatch, tmp_path: Path) -> None:
    app, _agent = _app(tmp_path)
    callbacks = []
    monkeypatch.setattr(
        app, "call_from_thread",
        lambda callback, *args: callbacks.append((callback, args)),
    )

    for _ in range(1_000):
        app._event_from_worker(Event(EventKind.ASSISTANT_TEXT, text="x"))
    app._event_from_worker(Event(EventKind.DONE))

    assert len(callbacks) == 1
    assert len(app._pending_events) == 2
    assert app._pending_events[0].kind == EventKind.ASSISTANT_TEXT
    assert app._pending_events[0].text == "x" * 1_000
    assert app._pending_events[1].kind == EventKind.DONE


@pytest.mark.parametrize(
    "terminal_event",
    [
        Event(EventKind.DONE),
        Event(EventKind.ERROR),
        Event(EventKind.TOOL_CALL, name="read_file", tool_use_id="call-1"),
    ],
)
def test_streaming_response_renders_markdown_only_when_card_finishes(
    terminal_event: Event, tmp_path: Path,
) -> None:
    app, _agent = _app(tmp_path)

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)):
            app._render_event(Event(EventKind.ASSISTANT_TEXT, text="**hel"))
            response = app.response_widget
            assert response is not None
            assert response.streaming
            visual = response.body.render()
            panel = getattr(visual, "_renderable", visual)
            assert isinstance(panel, Panel)
            assert isinstance(panel.renderable, Text)

            app._render_event(Event(EventKind.ASSISTANT_TEXT, text="lo**"))
            assert response.response_text == "**hello**"
            app._render_event(terminal_event)

            assert not response.streaming
            visual = response.body.render()
            panel = getattr(visual, "_renderable", visual)
            assert isinstance(panel, Panel)
            assert isinstance(panel.renderable, Markdown)
            assert response.response_text == "**hello**"

    asyncio.run(scenario())


def test_transcript_does_not_force_scroll_after_user_moves_up(tmp_path: Path) -> None:
    app, _agent = _app(tmp_path)

    async def scenario() -> None:
        async with app.run_test(size=(100, 20)) as pilot:
            for number in range(40):
                app._write(Text(f"line {number}"))
            await pilot.pause()
            transcript = app.query_one("#transcript")
            assert transcript.max_scroll_y > 0

            transcript.scroll_to(y=0, animate=False)
            await pilot.pause()
            app._write(Text("new output while reading history"))
            await pilot.pause()
            assert transcript.scroll_y == 0

            transcript.scroll_end(animate=False)
            await pilot.pause()
            app._write(Text("new output at the tail"))
            await pilot.pause()
            assert transcript.scroll_y == transcript.max_scroll_y

    asyncio.run(scenario())


def test_long_session_mounts_only_recent_transcript_entries(tmp_path: Path) -> None:
    app, _agent = _app(tmp_path)
    total = _MAX_TRANSCRIPT_ENTRIES + 12
    app.session.messages = [
        Message("assistant", [ContentBlock("text", text=f"answer {number}")])
        for number in range(total)
    ]

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()

            assert len(app.session.messages) == total
            assert len(app._transcript_widgets) == _MAX_TRANSCRIPT_ENTRIES
            assert app._hidden_transcript_entries == 13
            assert app._history_notice is not None
            assert "complete conversation remains saved" in str(
                app._history_notice.render()
            )
            responses = list(app.query(AssistantResponse))
            assert responses[-1].response_text == f"answer {total - 1}"

    asyncio.run(scenario())
