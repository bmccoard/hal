import io
import json
import signal
from types import SimpleNamespace

from neo.agent import Agent
from neo.cli import _save_live_session, main, run_chat, run_headless
from neo.models import ContentBlock, Response, ToolSpec, Usage
from neo.sessions import Session
from neo.tools import Registry, Tool


class CP1252Buffer(io.StringIO):
    def write(self, value: str) -> int:
        value.encode("cp1252")
        return super().write(value)


def test_help_is_windows_console_safe() -> None:
    output = CP1252Buffer()
    assert main(["help"], stdout=output) == 0
    assert "Interactive chat mode" in output.getvalue()


def test_unknown_command_returns_usage_error() -> None:
    output, error = io.StringIO(), io.StringIO()
    assert main(["wat"], stdout=output, stderr=error) == 2
    assert "unknown command: wat" in error.getvalue()


def test_headless_timeout_covers_the_complete_agent_loop(monkeypatch) -> None:
    class SlowAgent:
        def send(self, prompt, display_text="", cancellation=None):
            assert prompt == "work"
            assert cancellation is not None
            cancellation.wait(1)

    config = SimpleNamespace(provider="fake", model="model")
    monkeypatch.setattr("neo.cli._load", lambda _cwd, _stderr: config)
    monkeypatch.setattr(
        "neo.cli._make_agent",
        lambda *_args, **_kwargs: (SlowAgent(), [], {}),
    )
    output, error = io.StringIO(), io.StringIO()

    assert run_headless(
        ["--json", "--timeout", "0.01s", "work"],
        io.StringIO(""), output, error,
    ) == 1

    result = json.loads(output.getvalue())
    assert result["ok"] is False
    assert result["error"] == "operation timed out"
    assert result["elapsed_ms"] < 1000


def test_ctrl_c_cancels_turn_commits_results_and_saves_valid_session(monkeypatch) -> None:
    class InterruptTool(Tool):
        @property
        def spec(self):
            return ToolSpec("interrupt", "Raise SIGINT.", {"type": "object"})

        def run(self, arguments, cancellation=None):
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)

    class Provider:
        name = "fake"

        def complete(self, request, cancellation=None):
            return Response([
                ContentBlock("tool_use", id="call-1", name="interrupt", input={}),
                ContentBlock("tool_use", id="call-2", name="missing", input={}),
            ], "tool_use")

    class Store:
        def __init__(self):
            self.snapshots = []

        def create(self, metadata):
            session = Session(metadata)
            self.save(session)
            return session

        def save(self, session):
            self.snapshots.append([message.to_dict() for message in session.messages])

    agent = Agent(Provider(), "model", "system", Registry([InterruptTool()]))
    store = Store()
    config = SimpleNamespace(
        provider="fake", model="model", openai_auth="api_key",
    )
    inputs = iter(["work", "/exit"])
    previous_handler = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr("neo.cli.SessionStore", lambda: store)
    monkeypatch.setattr("neo.cli.load_config", lambda _cwd: config)
    monkeypatch.setattr(
        "neo.cli._make_agent",
        lambda *_args, **_kwargs: (agent, [], {}),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))
    output, error = io.StringIO(), io.StringIO()

    assert run_chat(output, error) == 0

    assert signal.getsignal(signal.SIGINT) == previous_handler
    assert "interrupted" in error.getvalue()
    results = agent.messages[-1].content
    assert [result.tool_use_id for result in results] == ["call-1", "call-2"]
    assert all(result.is_error for result in results)
    assert any(len(snapshot) == 3 for snapshot in store.snapshots)


def test_failed_session_save_keeps_live_state_for_retry() -> None:
    class FailingStore:
        def save(self, _session):
            raise OSError("disk unavailable")

    messages = [SimpleNamespace()]
    usage = Usage(input_tokens=3)
    agent = SimpleNamespace(messages=messages, usage=usage)
    session = Session(SimpleNamespace())
    error = io.StringIO()

    assert _save_live_session(FailingStore(), session, agent, error) is False
    assert session.messages is messages
    assert session.usage is usage
    assert "disk unavailable" in error.getvalue()
