import io
import json
from types import SimpleNamespace

from neo.cli import main, run_headless


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
