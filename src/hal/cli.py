from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from collections.abc import Callable
from typing import TextIO

from . import __version__
from .agent import Agent, Event, EventKind
from .cancellation import CancelledError, CancellationToken, cancel_on_sigint
from .config import Config, load_config
from .context import build_system, expand_user_input, load_skills, resolve_phases
from .extensions import load_extensions
from .git import GitError, create_git_backend
from .providers import ProviderError, create_provider
from .sayings import startup_saying
from .sessions import Metadata, Session, SessionStore, short_session_id
from .tools import BashTool, default_registry, workspace_root
from .workflows import WORKFLOWS, parse_workflow_command, run_workflow


USAGE = """HAL — a Python coding agent

USAGE:
  hal                Interactive chat mode (TUI; falls back to the basic REPL)
  hal chat [--no-tui]
                     Interactive chat with an optional basic-REPL fallback
  hal tui            Require the full-screen interactive interface
  hal run [options] <prompt>
                     Run one headless prompt and exit
  hal sessions [-v]  List saved chat sessions
  hal sessions search <query>
                     Search saved session transcripts
  hal doctor         Check local config and environment
  hal resume <selector>
                     Resume by full ID or unique short selector
  hal version        Show the HAL version (also -v, --version)
  hal help           Show this help

CONFIG:
  Reads hal.yaml -> ~/.hal/config.yaml -> defaults.
  Providers: anthropic (default), openai, openrouter, or google.

HEADLESS RUN:
  hal run --json --timeout 10m "Review this repo without changing files"
  cat prompt.md | hal run --json
"""


def _duration(value: str) -> float:
    multipliers = {"s": 1, "m": 60, "h": 3600}
    try:
        if value[-1].lower() in multipliers:
            return float(value[:-1]) * multipliers[value[-1].lower()]
        return float(value)
    except (ValueError, IndexError) as exc:
        raise argparse.ArgumentTypeError(f"invalid duration: {value}") from exc


def _load(cwd: Path, err: TextIO) -> Config | None:
    try: return load_config(cwd)
    except ValueError as exc:
        print(f"config: {exc}", file=err); return None


def _set_console_title_from_cwd() -> None:
    """Best-effort Windows console title update based on the current folder.

    On Windows, update the console window title to include the project name
    derived from the current working directory. This is intentionally
    conservative: failures are silently ignored and non-Windows platforms are
    left unchanged.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
        kernel32.SetConsoleTitleW.restype = wintypes.BOOL

        cwd = os.getcwd().rstrip("/\\")
        project = cwd.rsplit("\\", 1)[-1] if cwd else "-"
        title = f"HAL · {project}"
        # Ignore failures; this is a cosmetic enhancement only.
        kernel32.SetConsoleTitleW(title)
    except OSError:
        # Some hosts (e.g. embedded consoles) may not expose a traditional
        # console window; in those cases we simply leave the title alone.
        return


def _make_agent(config: Config, cwd: Path, session: Session | None = None, interactive: bool = False,
                out: TextIO = sys.stdout, err: TextIO = sys.stderr,
                event_handler: Callable[[Event], None] | None = None,
                confirm_handler: Callable[[str], bool] | None = None) -> tuple[Agent, list, dict]:
    skills = load_skills(cwd) if config.skills else []
    phases = resolve_phases(config)
    provider = create_provider(config)
    # Headless mode preserves its single buffered result/JSON contract. Streaming
    # is an interactive presentation capability and remains configurable there.
    provider.streaming_enabled = provider.streaming_enabled and interactive
    system = build_system(config, cwd, skills, phases)

    def event(activity: Event) -> None:
        if not interactive: return
        if activity.kind in {EventKind.ASSISTANT_TEXT, EventKind.ASSISTANT_COMMENTARY}:
            print(activity.text, end="", flush=True, file=out)
        elif activity.kind == EventKind.TOOL_CALL:
            print(f"\n-> {activity.name}", file=err)
        elif activity.kind == EventKind.TOOL_RESULT and activity.is_error:
            print(f"  error: {activity.text}", file=err)

    def confirm(prompt: str) -> bool:
        try: return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt): return False

    root = workspace_root(cwd)
    registry = default_registry(
        cwd, root, config.tool_approvals if interactive else None,
        (confirm_handler or confirm) if interactive else None, config.git_backend,
        config.only_write_locally, config.bash_policy,
    )
    load_extensions(registry, config.extensions, cwd, root, config.extension_config)
    agent = Agent(provider, config.model, system, registry,
                  messages=session.messages if session else None, usage=session.usage if session else None,
                  on_event=event_handler or event)
    return agent, skills, phases


def run_headless(args: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if any(x == "--permission" or x.startswith("--permission=") for x in args):
        print("--permission has been removed; run HAL inside a sandbox and use tool_approvals for optional interactive confirmations", file=stderr); return 2
    parser = argparse.ArgumentParser(prog="hal run", add_help=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--timeout", type=_duration, default=600.0)
    parser.add_argument("prompt", nargs="*")
    try: options = parser.parse_args(args)
    except SystemExit as exc: return int(exc.code)
    parts = list(options.prompt)
    if not stdin.isatty() and (piped := stdin.read().strip()): parts.insert(0, piped)
    prompt = " ".join(parts).strip()
    if not prompt:
        print("hal run: missing prompt", file=stderr); return 2
    started = time.monotonic(); cwd = Path.cwd(); cfg = _load(cwd, stderr)
    if cfg is None: return 1
    calls = errors = 0
    result: dict[str, object] = {"ok": False, "elapsed_ms": 0, "provider": cfg.provider, "model": cfg.model, "tool_calls": 0, "tool_errors": 0}
    try:
        cancellation = CancellationToken.with_timeout(options.timeout)
        agent, _, _ = _make_agent(cfg, cwd, err=stderr)
        def count(activity: Event) -> None:
            nonlocal calls, errors
            if activity.kind == EventKind.TOOL_CALL: calls += 1
            elif activity.kind == EventKind.TOOL_RESULT and activity.is_error: errors += 1
        agent.on_event = count
        final = agent.send(prompt, cancellation=cancellation)
        result.update(ok=True, final=final)
    except (CancelledError, ProviderError, OSError, ValueError, RuntimeError) as exc:
        result["error"] = str(exc)
    result.update(elapsed_ms=int((time.monotonic() - started) * 1000), tool_calls=calls, tool_errors=errors)
    if options.json_output: print(json.dumps(result), file=stdout)
    elif result["ok"]: print(result.get("final", ""), file=stdout)
    else: print(f"hal run: {result.get('error', 'failed')}", file=stderr)
    return 0 if result["ok"] else 1


def _short(path: str) -> str:
    if not path: return "-"
    try:
        relative = Path(path).relative_to(Path.home())
        return str(Path("~") / relative)
    except (OSError, ValueError): return path


def _project_name(path: str) -> str:
    value = path.rstrip("/\\").replace("\\", "/")
    return value.rsplit("/", 1)[-1] if value else "-"


def _model_name(model: str) -> str:
    return model.rsplit("/", 1)[-1] if model else "-"


def _print_sessions(items: list[Metadata], stdout: TextIO, *, verbose: bool = False,
                    current_id: str = "") -> None:
    if current_id:
        print(f"Current: {short_session_id(current_id)} ({current_id})", file=stdout)
    if verbose:
        print("SHORT\tID\tUPDATED\tPROVIDER\tMODEL\tCWD\tTITLE", file=stdout)
        for item in items:
            print(
                f"{short_session_id(item.id)}\t{item.id}\t{item.updated_at[:16].replace('T', ' ')}\t"
                f"{item.provider or '-'}\t{item.model or '-'}\t{_short(item.cwd)}\t{item.title or '(untitled)'}",
                file=stdout,
            )
        return
    print("SHORT\tID\tUPDATED\tMODEL\tPROJECT", file=stdout)
    for item in items:
        print(
            f"{short_session_id(item.id)}\t{item.id}\t{item.updated_at[:16].replace('T', ' ')}\t"
            f"{_model_name(item.model)}\t{_project_name(item.cwd)}",
            file=stdout,
        )


def run_sessions(args: list[str], stdout: TextIO, stderr: TextIO) -> int:
    store = SessionStore()
    try:
        if not args or args in (["-v"], ["--verbose"]):
            items = store.list()
            if not items: print("no saved sessions", file=stdout); return 0
            _print_sessions(items, stdout, verbose=bool(args))
            return 0
        if args[0] == "search" and len(args) >= 2:
            results = store.search(" ".join(args[1:]))
            if not results: print("no matching sessions", file=stdout); return 0
            print("SHORT\tID\tUPDATED\tMODEL\tPROJECT\tTITLE\tMATCH", file=stdout)
            for x, excerpt in results: print(f"{short_session_id(x.id)}\t{x.id}\t{x.updated_at[:16].replace('T', ' ')}\t{_model_name(x.model)}\t{_project_name(x.cwd)}\t{x.title or '(untitled)'}\t{excerpt}", file=stdout)
            return 0
        print("usage: hal sessions [-v|--verbose] | hal sessions search <query>", file=stderr); return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"sessions: {exc}", file=stderr); return 1


def _credential_status(cfg: Config) -> tuple[str, str]:
    env = cfg.credential_env()
    if cfg.backend() == "openai" and cfg.openai_auth == "subscription" and cfg.active_profile() is None: return "fail", "subscription auth is unavailable in the Python port"
    profile = cfg.active_profile()
    if (profile and profile.api_key) or cfg.api_key:
        return "pass", "inline API key is configured"
    return ("pass", f"{env} is set") if os.environ.get(env, "").strip() else ("fail", f"set api_key or {env}")


def run_doctor(stdout: TextIO) -> int:
    checks: list[tuple[str, str, str]] = []; failed = False; git_preference = "auto"
    try:
        cfg = load_config(); checks.extend([("pass", "config", f"loaded {cfg.source}"), ("pass", "provider", cfg.provider), (*_credential_status(cfg), "")])
        status, detail = _credential_status(cfg); checks[-1] = (status, "credentials", detail); checks.append(("pass", "model", cfg.model)); failed |= status == "fail"
        git_preference = cfg.git_backend
    except ValueError as exc:
        checks.append(("fail", "config", str(exc))); failed = True
    directory = SessionStore().directory; checks.append(("pass" if directory.is_dir() else "warn", "sessions", f"store {'is available' if directory.is_dir() else 'will be created'} at {_short(str(directory))}"))
    try:
        root = workspace_root(Path.cwd())
        git = create_git_backend(root, git_preference)
        checks.append(("pass", "git", f"{git.name} backend available"))
        repository = git.is_repository()
        checks.append((
            "pass" if repository else "warn", "workspace",
            f"Git repository at {_short(str(root))}" if repository else "current directory is not a Git workspace",
        ))
    except (GitError, OSError, ValueError) as exc:
        checks.append(("fail", "git", str(exc))); failed = True
    print("STATUS\tCHECK\tDETAIL", file=stdout)
    for row in checks: print("\t".join(row), file=stdout)
    return 1 if failed else 0


def _tui_supported(stdin: TextIO, stdout: TextIO) -> bool:
    """Use the full-screen UI only on a real, capable terminal."""
    if os.environ.get("HAL_NO_TUI", "").strip().lower() in {"1", "true", "yes"}:
        return False
    return bool(
        getattr(stdin, "isatty", lambda: False)()
        and getattr(stdout, "isatty", lambda: False)()
        and os.environ.get("TERM", "").lower() != "dumb"
    )


def _missing_tui_dependencies() -> list[str]:
    """Return direct TUI imports that are absent from the active environment."""
    missing = []
    for name in ("rich", "textual"):
        try:
            available = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            available = False
        if not available:
            missing.append(name)
    return missing


def _git_branch(cwd: Path, preference: str) -> str:
    try:
        backend = create_git_backend(workspace_root(cwd), preference)
        if backend.is_repository():
            return backend.status().branch or "-"
    except (GitError, OSError, ValueError):
        pass
    return "-"


def run_tui_chat(stderr: TextIO, session_id: str | None = None) -> int:
    """Initialize chat state and hand it to the event-driven terminal UI."""
    from .tui import run_tui

    store = SessionStore(); cwd = Path.cwd()
    try:
        if session_id:
            session = store.load(session_id)
            saved = Path(session.metadata.cwd)
            if saved.is_dir():
                os.chdir(saved); cwd = saved
            else:
                print(f"warning: saved working directory is unavailable: {saved}", file=stderr)
        else:
            session = None
        cfg = load_config(cwd)
        if session and session.metadata.provider and session.metadata.model:
            cfg.provider = session.metadata.provider.replace("openai-codex", "openai")
            cfg.model = session.metadata.model
        agent, skills, phases = _make_agent(
            cfg, cwd, session, interactive=True,
            event_handler=lambda _event: None, confirm_handler=lambda _prompt: False,
        )
        if session is None:
            session = store.create(Metadata(
                cwd=str(cwd), model=cfg.model, provider=cfg.provider,
                openai_auth=cfg.openai_auth if cfg.provider == "openai" else "",
            ))

        def make_session(target_cwd: Path, target: Session):
            target_cfg = load_config(target_cwd)
            if target.metadata.provider and target.metadata.model:
                target_cfg.provider = target.metadata.provider.replace("openai-codex", "openai")
                target_cfg.model = target.metadata.model
            target_agent, target_skills, target_phases = _make_agent(
                target_cfg, target_cwd, target, interactive=True,
                event_handler=lambda _event: None, confirm_handler=lambda _prompt: False,
            )
            return (
                target_cfg, target_agent, target_skills, target_phases,
                _git_branch(target_cwd, target_cfg.git_backend),
            )

        _set_console_title_from_cwd()
        return run_tui(
            agent, cfg, cwd, session, store, skills, phases,
            branch=_git_branch(cwd, cfg.git_backend), session_factory=make_session,
        )
    except (GitError, ProviderError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"tui: {exc}", file=stderr)
        return 1


def _run_interactive(
    stdin: TextIO, stdout: TextIO, stderr: TextIO,
    session_id: str | None = None, *, require_tui: bool = False,
) -> int:
    if not _tui_supported(stdin, stdout):
        if require_tui:
            print(
                "hal tui: a capable interactive terminal is required; use "
                "'hal chat --no-tui' for the basic interface",
                file=stderr,
            )
            return 1
        return run_chat(stdout, stderr, session_id)
    if missing := _missing_tui_dependencies():
        names = ", ".join(missing)
        guidance = "run 'python -m pip install -e \".[tui]\"' to install the TUI dependencies"
        if require_tui:
            print(f"hal tui: missing {names}; {guidance}", file=stderr)
            return 1
        print(f"warning: TUI unavailable (missing {names}); using basic REPL; {guidance}", file=stderr)
        return run_chat(stdout, stderr, session_id)
    return run_tui_chat(stderr, session_id)


def run_chat(stdout: TextIO, stderr: TextIO, session_id: str | None = None) -> int:
    store = SessionStore(); cwd = Path.cwd()
    try:
        if session_id:
            session = store.load(session_id)
            saved = Path(session.metadata.cwd)
            if saved.is_dir(): os.chdir(saved); cwd = saved
            else: print(f"warning: saved working directory is unavailable: {saved}", file=stderr)
        else: session = None
        cfg = load_config(cwd)
        if session and session.metadata.provider and session.metadata.model:
            cfg.provider, cfg.model = session.metadata.provider.replace("openai-codex", "openai"), session.metadata.model
        agent, skills, phases = _make_agent(cfg, cwd, session, interactive=True, out=stdout, err=stderr)
        if session is None:
            session = store.create(Metadata(cwd=str(cwd), model=cfg.model, provider=cfg.provider, openai_auth=cfg.openai_auth if cfg.provider == "openai" else ""))
        _set_console_title_from_cwd()
        print(f"HAL · {cfg.provider}/{cfg.model} · {cwd}", file=stdout)
        print(f"“{startup_saying()}”", file=stdout)
        print("Type /help for commands; Ctrl-D or /exit to quit.", file=stdout)
        while True:
            try: text = input("HAL> ").strip()
            except (EOFError, KeyboardInterrupt): print(file=stdout); break
            if not text: continue
            if text in {"/exit", "/quit"}: break
            if text == "/help":
                names = ", ".join(f"/{x}" for x in phases) + (", " + ", ".join(f"/{x.name}" for x in skills) if skills else "")
                print(f"Commands: /help, /workflows, /workflow <name> <request>, /sessions [-v], /resume <short-id>, /clear, /model <id>, /exit; phases/skills: {names}", file=stdout); continue
            if text == "/workflows":
                for workflow in WORKFLOWS.values():
                    phases_str = " -> ".join(workflow.phases)
                    print(f"{workflow.name}\t{phases_str}\t{workflow.description}", file=stdout)
                continue
            if text == "/sessions" or text in {"/sessions -v", "/sessions --verbose"}:
                try:
                    items = store.list()
                    if not items: print("no saved sessions", file=stdout)
                    else: _print_sessions(items, stdout, verbose=text != "/sessions", current_id=session.metadata.id)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    print(f"sessions: {exc}", file=stderr)
                continue
            if text == "/resume" or text.startswith("/resume "):
                selector = text[7:].strip()
                if not selector:
                    print("usage: /resume <short-id-or-full-id>", file=stderr); continue
                try:
                    target = store.load(selector)
                    if target.metadata.id == session.metadata.id:
                        print(f"Already using {short_session_id(session.metadata.id)} ({session.metadata.id}).", file=stdout); continue
                    if not _save_live_session(store, session, agent, stderr): continue
                    saved = Path(target.metadata.cwd)
                    target_cwd = saved if saved.is_dir() else cwd
                    target_cfg = load_config(target_cwd)
                    if target.metadata.provider and target.metadata.model:
                        target_cfg.provider = target.metadata.provider.replace("openai-codex", "openai")
                        target_cfg.model = target.metadata.model
                    target_agent, target_skills, target_phases = _make_agent(
                        target_cfg, target_cwd, target, interactive=True, out=stdout, err=stderr,
                    )
                    if saved.is_dir(): os.chdir(saved)
                    else: print(f"warning: saved working directory is unavailable: {saved}", file=stderr)
                    session, cwd, cfg = target, target_cwd, target_cfg
                    agent, skills, phases = target_agent, target_skills, target_phases
                    _set_console_title_from_cwd()
                    print(
                        f"Resumed {short_session_id(session.metadata.id)} ({session.metadata.id}) · "
                        f"{cfg.provider}/{cfg.model} · {cwd}", file=stdout,
                    )
                except (GitError, ProviderError, OSError, ValueError, json.JSONDecodeError) as exc:
                    print(f"resume: {exc}", file=stderr)
                continue
            if text == "/clear": agent.messages.clear(); agent.usage = type(agent.usage)(); print("Conversation cleared.", file=stdout); continue
            if text.startswith("/model "): agent.model = text[7:].strip(); session.metadata.model = agent.model; print(f"Model: {agent.model}", file=stdout); continue
            if text.startswith("!"):
                cancellation = CancellationToken()
                try:
                    with cancel_on_sigint(cancellation):
                        result = BashTool(cwd).run(
                            {"command": text[1:].strip()}, cancellation,
                        )
                    print(result, file=stdout)
                except CancelledError as exc:
                    print(f"interrupted: {exc}", file=stderr)
                except (OSError, ValueError, RuntimeError) as exc:
                    print(f"error: {exc}", file=stderr)
                continue
            cancellation = CancellationToken()
            try:
                with cancel_on_sigint(cancellation):
                    parsed = parse_workflow_command(text)
                    if parsed:
                        workflow, request = parsed
                        run_workflow(
                            agent, workflow, request, phases, cancellation,
                            lambda index, total, name: print(
                                f"\n[{index}/{total}] {name}", file=stdout,
                            ),
                        )
                    else:
                        expanded, display = expand_user_input(text, skills, phases)
                        agent.send(expanded, display, cancellation)
                print(file=stdout)
            except CancelledError as exc:
                print(f"interrupted: {exc}", file=stderr)
            except (ProviderError, OSError, ValueError, RuntimeError) as exc:
                print(f"error: {exc}", file=stderr)
            _save_live_session(store, session, agent, stderr)
        return 0 if _save_live_session(store, session, agent, stderr) else 1
    except (GitError, ProviderError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"chat: {exc}", file=stderr); return 1


def _save_live_session(store: SessionStore, session: Session, agent: Agent,
                       stderr: TextIO) -> bool:
    """Snapshot live agent state without discarding it when persistence fails."""
    session.messages, session.usage = agent.messages, agent.usage
    try:
        store.save(session)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: could not save session: {exc}", file=stderr)
        return False
    return True


def main(argv: list[str] | None = None, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _run_interactive(stdin, stdout, stderr)
    command, rest = args[0], args[1:]
    if command == "chat":
        if rest == ["--no-tui"]: return run_chat(stdout, stderr)
        if rest: print("usage: hal chat [--no-tui]", file=stderr); return 2
        return _run_interactive(stdin, stdout, stderr)
    if command == "tui":
        if rest: print("usage: hal tui", file=stderr); return 2
        return _run_interactive(stdin, stdout, stderr, require_tui=True)
    if command == "run": return run_headless(rest, stdin, stdout, stderr)
    if command == "sessions": return run_sessions(rest, stdout, stderr)
    if command == "doctor": return run_doctor(stdout)
    if command == "resume":
        if not rest: print("usage: hal resume <short-id-or-full-id>", file=stderr); return 2
        if len(rest) > 1: print("usage: hal resume <short-id-or-full-id>", file=stderr); return 2
        return _run_interactive(stdin, stdout, stderr, rest[0])
    if command in {"version", "-v", "--version"}: print(f"hal version {__version__}", file=stdout); return 0
    if command in {"help", "-h", "--help"}: print(USAGE, file=stdout); return 0
    if command == "login": print("login: ChatGPT subscription auth is not supported by the Python port; configure openai_auth: api_key", file=stderr); return 1
    if command == "logout": print("logout: no Python-port subscription credentials are stored", file=stdout); return 0
    print(f"unknown command: {command}", file=stderr); print(USAGE, file=stdout); return 2
