from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import App
from textual.widgets import Button, Footer

from hal.agent import Agent
from hal.config import Config
from hal.sessions import SessionStore
from hal.tui import AssistantResponse, Composer, HalTui


@dataclass
class FakeEvent:
    kind: str
    text: str | None = None
    is_error: bool = False
    name: str | None = None
    args: dict | None = None
    tool_use_id: str | None = None
    verification: object | None = None
    provider_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    status: str = "succeeded"
    reason: str | None = None
    run_id: str = "run-1"


class FakeAgent(Agent):
    def __init__(self) -> None:
        config = Config(model="test-model")
        super().__init__(
            provider=None,
            model=config.model,
            system="",
            registry=None,
        )
        self.prompts: list[tuple[str, str]] = []

    def send(self, expanded: str, display: str | None = None, cancellation=None):
        self.prompts.append((expanded, display or ""))
        return "response"


def _app(tmp_path: Path) -> tuple[HalTui, FakeAgent, SessionStore]:
    agent = FakeAgent()
    cfg = Config(model="test-model")
    store = SessionStore(directory=tmp_path / "sessions")
    session = store.create(metadata=None)
    app = HalTui(agent, cfg, tmp_path, session, store, [], {}, branch="main")
    return app, agent, store


def test_large_paste_marker_uses_middle_dot(tmp_path: Path) -> None:
    app, agent, _store = _app(tmp_path)
    pasted = "x" * 6000

    async def scenario() -> None:
        async with app.run_test(size=(100, 32)) as pilot:
            composer = app.query_one("#composer", Composer)
            composer.post_message(events.Paste(pasted))
            await pilot.pause()
            assert composer.text.startswith("[Pasted block 1 · ")
            assert composer.text.endswith(" bytes]")
            assert composer.expand_pastes(composer.text) == pasted
            assert agent.prompts == []
            app.action_safe_quit()

    asyncio.run(scenario())
