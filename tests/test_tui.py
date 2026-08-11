from __future__ import annotations

import asyncio
import ctypes
from pathlib import Path
import threading
from types import SimpleNamespace

from textual.containers import VerticalScroll
from textual.widgets import Button

from hal.agent import Event, EventKind
from hal.cancellation import CancellationToken
from hal.config import Config
from hal.models import ContentBlock, Message, Usage
from hal.sayings import HAL_SAYINGS
from hal.sessions import Metadata, SessionStore
from hal.tui import AssistantResponse, HalTui


class FakeAgent:
    def __init__(self) -> None:
        self.model = "test/model"
        self.messages: list[Message] = []
        self.usage = Usage()
        self.on_event = lambda _event: None
        self.tools = SimpleNamespace(confirm=None)
        self.prompts: list[tuple[str, str]] = []

    def send(self, text: str, display_text: str = "",
             cancellation: CancellationToken | None = None) -> str:
        self.prompts.append((text, display_text))
        self.messages.append(Message("user", [ContentBlock("text", text=text)], display_text))
        self.on_event(Event(EventKind.TOOL_CALL, name="read_file", tool_use_id="call-1"))
        self.on_event(Event(
            EventKind.TOOL_RESULT, name="read_file", tool_use_id="call-1",
            text="contents", duration_ms=4,
        ))
        self.messages.append(Message("assistant", [ContentBlock("text", text="Done.")]))
        self.on_event(Event(EventKind.ASSISTANT_TEXT, text="Done."))
        self.on_event(Event(EventKind.DONE))
        return "Done."


def _app(tmp_path: Path) -> tuple[HalTui, FakeAgent, SessionStore]:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(Metadata(
        cwd=str(tmp_path), provider="openrouter", model="test/model",
    ))
    agent = FakeAgent()
    app = HalTui(
        agent, Config(provider="openrouter", model="test/model"), tmp_path,
        session, store, [], {}, branch="main",
    )
    return app, agent, store


def test_tui_submits_in_worker_renders_events_and_saves_session(tmp_path: Path) -> None:
    app, agent, store = _app(tmp_path)

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            assert app.startup_saying in HAL_SAYINGS
            app.query_one("#composer").text = "check the project"
            app.action_submit()
            for _ in range(40):
                await pilot.pause(0.025)
                if not app.busy:
                    break
            assert agent.prompts == [("check the project", "")]
            assert not app.busy
            assert "main" in str(app.query_one("#status").render())
            assert store.load(app.session.metadata.id).messages[-1].content[0].text == "Done."
            app.action_safe_quit()

    asyncio.run(scenario())


def test_tui_clear_resets_live_and_saved_conversation(tmp_path: Path) -> None:
    app, agent, store = _app(tmp_path)
    agent.messages.append(Message("user", [ContentBlock("text", text="old")]))

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)):
            app.action_clear_conversation()
            assert agent.messages == []
            assert store.load(app.session.metadata.id).messages == []
            app.action_safe_quit()

    asyncio.run(scenario())


def test_tui_updates_one_response_card_for_multiple_stream_deltas(tmp_path: Path) -> None:
    app, _agent, _store = _app(tmp_path)

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            initial = len(list(app.query(".transcript-entry")))
            app._render_event(Event(EventKind.ASSISTANT_TEXT, text="Hel"))
            app._render_event(Event(EventKind.ASSISTANT_TEXT, text="lo"))
            await pilot.pause()
            assert len(list(app.query(".transcript-entry"))) == initial + 1
            assert app.response_text == "Hello"
            response = app.query_one(AssistantResponse)
            assert response.response_text == "Hello"
            assert response.query_one(".copy-response", Button).label.plain == "Copy"
            app._render_event(Event(EventKind.DONE))
            assert app.response_widget is None
            app.action_safe_quit()

    asyncio.run(scenario())


def test_tui_copy_button_copies_raw_response_markdown(tmp_path: Path) -> None:
    app, _agent, _store = _app(tmp_path)
    copied: list[str] = []
    markdown = "## Result\n\n- **answer:** `42`"

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            app._render_event(Event(EventKind.ASSISTANT_TEXT, text=markdown))
            await pilot.pause()
            app._copy_response = copied.append  # type: ignore[method-assign]
            await pilot.click(".copy-response")
            assert copied == [markdown]
            app.action_safe_quit()

    asyncio.run(scenario())


def test_windows_clipboard_accepts_unicode(monkeypatch) -> None:
    from hal.tui import copy_windows_unicode

    clipboard: dict[str, object] = {}

    class Function:
        def __init__(self, callback):
            object.__setattr__(self, "callback", callback)

        def __call__(self, *args):
            return self.callback(*args)

        def __setattr__(self, _name, _value):
            pass

    class User32:
        OpenClipboard = Function(lambda _owner: 1)
        EmptyClipboard = Function(lambda: 1)
        SetClipboardData = Function(
            lambda format_id, memory: clipboard.update(
                format=format_id, memory=memory
            ) or memory
        )
        CloseClipboard = Function(lambda: 1)

    class Kernel32:
        GlobalAlloc = Function(lambda _flags, size: ctypes.create_string_buffer(size))
        GlobalLock = Function(lambda memory: ctypes.addressof(memory))
        GlobalUnlock = Function(lambda _memory: 1)
        GlobalFree = Function(lambda _memory: None)

    libraries = iter([User32(), Kernel32()])
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *_args, **_kwargs: next(libraries), raising=False
    )
    copy_windows_unicode("step → done ✓")

    assert clipboard["format"] == 13
    memory = clipboard["memory"]
    assert isinstance(memory, ctypes.Array)
    assert memory.raw.decode("utf-16-le").rstrip("\0") == "step → done ✓"


def test_tui_portable_submit_and_multiline_keys(tmp_path: Path) -> None:
    app, agent, _store = _app(tmp_path)

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            composer = app.query_one("#composer")
            composer.text = "two lines"
            await pilot.press("f3")
            assert "\n" in composer.text
            assert agent.prompts == []

            composer.text = "send with enter"
            await pilot.press("enter")
            for _ in range(40):
                await pilot.pause(0.025)
                if not app.busy:
                    break
            assert agent.prompts == [("send with enter", "")]
            app.action_safe_quit()

    asyncio.run(scenario())


def test_tui_cancel_interrupts_active_worker_and_returns_to_composer(tmp_path: Path) -> None:
    app, agent, _store = _app(tmp_path)
    started = threading.Event()

    def slow_send(text: str, display_text: str = "",
                  cancellation: CancellationToken | None = None) -> str:
        started.set()
        assert cancellation is not None
        cancellation.wait(30)
        return "unreachable"

    agent.send = slow_send  # type: ignore[method-assign]

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            app.query_one("#composer").text = "long task"
            app.action_submit()
            for _ in range(40):
                await pilot.pause(0.025)
                if started.is_set():
                    break
            assert app.busy
            app.action_cancel_turn()
            for _ in range(40):
                await pilot.pause(0.025)
                if not app.busy:
                    break
            assert not app.busy
            assert app.cancellation is None
            app.action_safe_quit()

    asyncio.run(scenario())


def test_tui_workflows_command_displays_phases(tmp_path: Path) -> None:
    app, _agent, _store = _app(tmp_path)

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            composer = app.query_one("#composer")
            composer.text = "/workflows"
            await pilot.press("enter")
            await pilot.pause(0.1)
            transcript = app.query_one("#transcript", VerticalScroll)
            entries = transcript.query(".transcript-entry")
            text = " ".join(str(entry.render()) for entry in entries)
            assert "feature" in text
            assert "design -> plan -> build -> review" in text
            app.action_safe_quit()

    asyncio.run(scenario())
