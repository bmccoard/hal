import io
import json
import signal
from types import SimpleNamespace

from hal.agent import Agent
from hal.cli import (
    _save_live_session, main, run_chat, run_harness, run_headless, run_sessions,
)
from hal.config import parse_config
from hal.harness import Capability, RunBudgets
from hal.models import ContentBlock, Response, ToolSpec, Usage
from hal.sayings import HAL_SAYINGS
from hal.sessions import Metadata, Session, SessionStore
from hal.tools import Registry, Tool


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


def test_harness_inspection_resolves_policy_and_budgets_as_json(
    monkeypatch, tmp_path,
) -> None:
    config = parse_config({"harness": {
        "budgets": {
            "provider_calls": 10, "tool_calls": None,
            "elapsed_seconds": None,
        },
        "verification": [{"name": "tests", "command": "pytest"}],
        "repair_attempts": 1,
        "capabilities": {"docs": {
            "allowed_tools": ["read_file"],
            "denied_tools": ["bash"],
            "budgets": {
                "provider_calls": 3, "tool_calls": 4,
                "elapsed_seconds": None,
            },
        }},
    }})
    registry = Registry([])
    registry._tools = {
        "read_file": SimpleNamespace(spec=ToolSpec("read_file", "read", {})),
        "bash": SimpleNamespace(spec=ToolSpec("bash", "bash", {})),
    }
    monkeypatch.setattr("hal.cli._load", lambda _cwd, _stderr: config)
    monkeypatch.setattr("hal.cli.workspace_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("hal.cli._make_registry", lambda *_args: registry)
    output = io.StringIO()

    assert run_harness(["docs", "--json"], output, io.StringIO()) == 0

    payload = json.loads(output.getvalue())
    assert payload["capability"] == "docs"
    assert payload["available_tools"] == ["read_file"]
    assert payload["tool_metadata"]["read_file"]["effect"] == "unknown"
    assert payload["denied_tools"] == ["bash"]
    assert payload["budgets"]["provider_calls"] == 3
    assert payload["budgets"]["tool_calls"] == 4
    assert payload["verification"] == ["tests"]
    assert payload["repair_attempts"] == 1


def test_repl_workflows_displays_ordered_phases(monkeypatch, tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions")
    config = SimpleNamespace(
        provider="fake", model="model", openai_auth="api_key",
    )
    agent = SimpleNamespace(messages=[], usage=Usage(), model="model")
    monkeypatch.setattr("hal.cli.SessionStore", lambda: store)
    monkeypatch.setattr("hal.cli.load_config", lambda _cwd: config)
    monkeypatch.setattr(
        "hal.cli._make_agent", lambda *_args, **_kwargs: (agent, [], {}),
    )
    inputs = iter(["/workflows", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))
    output, error = io.StringIO(), io.StringIO()

    assert run_chat(output, error) == 0
    assert (
        "feature\tdesign -> plan -> build -> review\t"
        "Design, plan, build, and review one requested repository change"
    ) in output.getvalue()
    assert error.getvalue() == ""


def test_interactive_cli_selects_tui_for_terminal_and_honors_fallback(
    monkeypatch,
) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    called = []
    monkeypatch.setattr("hal.cli.run_tui_chat", lambda _stderr, _session=None: called.append("tui") or 0)
    monkeypatch.setattr("hal.cli.run_chat", lambda _stdout, _stderr, _session=None: called.append("repl") or 0)

    assert main(["chat"], stdin=Terminal(), stdout=Terminal(), stderr=io.StringIO()) == 0
    assert main(["chat", "--no-tui"], stdin=Terminal(), stdout=Terminal(), stderr=io.StringIO()) == 0
    monkeypatch.setenv("HAL_NO_TUI", "1")
    assert main([], stdin=Terminal(), stdout=Terminal(), stderr=io.StringIO()) == 0
    monkeypatch.delenv("HAL_NO_TUI")
    monkeypatch.setattr("hal.cli._missing_tui_dependencies", lambda: ["rich"])
    error = io.StringIO()
    assert main([], stdin=Terminal(), stdout=Terminal(), stderr=error) == 0
    assert "missing rich" in error.getvalue()
    assert "pip install -e \".[tui]\"" in error.getvalue()
    assert called == ["tui", "repl", "repl", "repl"]


def test_required_tui_reports_missing_dependencies_without_starting_repl(
    monkeypatch,
) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("hal.cli._missing_tui_dependencies", lambda: ["rich", "textual"])
    monkeypatch.setattr("hal.cli.run_chat", lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected REPL")))
    error = io.StringIO()

    assert main(["tui"], stdin=Terminal(), stdout=Terminal(), stderr=error) == 1
    assert "missing rich, textual" in error.getvalue()


def test_headless_timeout_covers_the_complete_agent_loop(monkeypatch) -> None:
    class SlowAgent:
        def send(self, prompt, display_text="", cancellation=None):
            assert prompt == "work"
            assert cancellation is not None
            cancellation.wait(1)

    config = SimpleNamespace(provider="fake", model="model")
    monkeypatch.setattr("hal.cli._load", lambda _cwd, _stderr: config)
    monkeypatch.setattr(
        "hal.cli._make_agent",
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


def test_headless_json_exposes_harness_outcome_and_budget_exit_code(monkeypatch) -> None:
    class Provider:
        name = "fake"

        def complete(self, _request, cancellation=None):
            return Response([ContentBlock("text", text="Working.")], "pause_turn")

    config = SimpleNamespace(provider="fake", model="model")
    agent = Agent(
        Provider(), "model", "system", Registry([]),
        budgets=RunBudgets(
            provider_calls=1, tool_calls=None, elapsed_seconds=None,
        ),
    )
    monkeypatch.setattr("hal.cli._load", lambda _cwd, _stderr: config)
    monkeypatch.setattr(
        "hal.cli._make_agent", lambda *_args, **_kwargs: (agent, [], {}),
    )
    output = io.StringIO()

    assert run_headless(
        ["--json", "work"], io.StringIO(""), output, io.StringIO(),
    ) == 3

    result = json.loads(output.getvalue())
    assert result["ok"] is False
    assert result["harness"]["status"] == "budget_exhausted"
    assert result["harness"]["reason"] == "budget_provider_calls_exhausted"
    assert result["harness"]["provider_calls"] == 1
    assert result["harness"]["run_id"].startswith("run_")


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
    monkeypatch.setattr("hal.cli.SessionStore", lambda: store)
    monkeypatch.setattr("hal.cli.load_config", lambda _cwd: config)
    monkeypatch.setattr(
        "hal.cli._make_agent",
        lambda *_args, **_kwargs: (agent, [], {}),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))
    output, error = io.StringIO(), io.StringIO()

    assert run_chat(output, error) == 0

    assert signal.getsignal(signal.SIGINT) == previous_handler
    assert "interrupted" in error.getvalue()
    assert any(f"“{saying}”" in output.getvalue() for saying in HAL_SAYINGS)
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


def test_session_list_is_compact_by_default_and_verbose_on_request(
    monkeypatch, tmp_path,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.create(Metadata(
        id="sess_ae5f63c2dd8b4abd", cwd=str(tmp_path / "my-project"),
        model="poolside/laguna-s-2.1:free", provider="openrouter",
    ))
    monkeypatch.setattr("hal.cli.SessionStore", lambda: store)
    compact, verbose = io.StringIO(), io.StringIO()

    assert run_sessions([], compact, io.StringIO()) == 0
    assert run_sessions(["--verbose"], verbose, io.StringIO()) == 0

    assert "SHORT\tID\tUPDATED\tMODEL\tPROJECT" in compact.getvalue()
    assert "ae5f63c2\tsess_ae5f63c2dd8b4abd" in compact.getvalue()
    assert "laguna-s-2.1:free\tmy-project" in compact.getvalue()
    assert "PROVIDER\tMODEL\tCWD\tTITLE" in verbose.getvalue()
    assert "openrouter\tpoolside/laguna-s-2.1:free" in verbose.getvalue()


def test_interactive_sessions_show_current_id_and_resume_by_short_selector(
    monkeypatch, tmp_path,
) -> None:
    first_dir = tmp_path / "first"; second_dir = tmp_path / "second"
    first_dir.mkdir(); second_dir.mkdir()
    store = SessionStore(tmp_path / "sessions")
    first = store.create(Metadata(
        id="sess_aaaaaaaa11111111", cwd=str(first_dir), model="first-model", provider="fake",
    ))
    second = store.create(Metadata(
        id="sess_bbbbbbbb22222222", cwd=str(second_dir), model="second-model", provider="fake",
    ))
    agents = []

    def make_agent(_config, _cwd, session=None, **_kwargs):
        agent = SimpleNamespace(
            messages=session.messages if session else [],
            usage=session.usage if session else Usage(), model=session.metadata.model if session else "model",
        )
        agents.append(agent)
        return agent, [], {}

    inputs = iter(["/sessions", "/resume bbbbbbbb", "/exit"])
    monkeypatch.setattr("hal.cli.SessionStore", lambda: store)
    monkeypatch.setattr(
        "hal.cli.load_config",
        lambda _cwd: SimpleNamespace(provider="fake", model="model", openai_auth="api_key"),
    )
    monkeypatch.setattr("hal.cli._make_agent", make_agent)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))
    output, error = io.StringIO(), io.StringIO()

    assert run_chat(output, error, first.metadata.id) == 0

    assert f"Current: aaaaaaaa ({first.metadata.id})" in output.getvalue()
    assert f"Resumed bbbbbbbb ({second.metadata.id})" in output.getvalue()
    assert not error.getvalue()
    assert len(agents) == 2


def test_doctor_accepts_dulwich_fallback_when_git_executable_is_missing(
    monkeypatch, tmp_path,
) -> None:
    from dulwich import porcelain
    from hal.cli import run_doctor

    root = tmp_path / "repo"
    porcelain.init(root)
    config = parse_config({
        "provider": "openrouter", "api_key": "placeholder",
        "git": {"backend": "auto"},
    })
    monkeypatch.chdir(root)
    monkeypatch.setattr("hal.cli.load_config", lambda: config)
    monkeypatch.setattr("hal.git.shutil.which", lambda _name: None)
    output = io.StringIO()

    assert run_doctor(output) == 0
    assert "pass\tgit\tdulwich backend available" in output.getvalue()
