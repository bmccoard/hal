from __future__ import annotations

import asyncio
from pathlib import Path

from textual import events
from textual.widgets import Button, Footer

from hal.agent import Agent
from hal.cancellation import CancellationToken
from hal.config import Config
from hal.models import ContentBlock, Message, Request, Response
from hal.providers import Provider
from hal.sayings import HAL_SAYINGS
from hal.sessions import Metadata, SessionStore
from hal.tools import Registry
from hal.tui import Composer, ConfirmScreen, HalTui


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


def test_tui_layout_uses_updated_copy_and_has_no_paste_button(tmp_path: Path) -> None:
    app, _agent = _app(tmp_path)

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)):
            assert app.startup_saying in HAL_SAYINGS
            composer = app.query_one("#composer", Composer)
            assert composer.placeholder == "Ask HAL…"
            assert str(app.query_one("#composer-hint").render()) == (
                "Enter/F2 send · Ctrl+J new line"
            )
            assert len(app.query("#paste")) == 0
            assert app.query_one("#newline", Button).label.plain == "Newline"
            footer = app.query_one(Footer)
            controls = app.query_one("#composer-controls")
            assert controls.region.bottom <= footer.region.y

    asyncio.run(scenario())


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
