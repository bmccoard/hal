from __future__ import annotations

import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace

from textual.containers import VerticalScroll

from hal.agent import Event, EventKind
from hal.cancellation import CancellationToken
from hal.config import Config
from hal.models import ContentBlock, Message, Usage
from hal.sayings import HAL_SAYINGS
from hal.sessions import Metadata, SessionStore
from hal.tui import HalTui


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
            app._render_event(Event(EventKind.DONE))
            assert app.response_widget is None
            app.action_safe_quit()

    asyncio.run(scenario())


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
