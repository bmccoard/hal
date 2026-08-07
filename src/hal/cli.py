from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import TextIO

from . import __version__
from .agent import Agent, Event, EventKind
from .cancellation import CancelledError, CancellationToken, cancel_on_sigint
from .config import Config, load_config
from .context import build_system, expand_user_input, load_skills, resolve_phases
from .git import GitError, create_git_backend
from .providers import ProviderError, create_provider
from .sessions import Metadata, Session, SessionStore
from .tools import BashTool, default_registry, workspace_root


USAGE = """HAL — a Python coding agent

USAGE:
  hal                Interactive chat mode (default)
  hal chat           Interactive chat mode (explicit)
  hal run [options] <prompt>
                     Run one headless prompt and exit
  hal sessions       List saved chat sessions
  hal sessions search <query>
                     Search saved session transcripts
  hal doctor         Check local config and environment
  hal resume <id>    Resume a saved chat session
  hal version        Show the HAL version (also -v, --version)
  hal help           Show this help

CONFIG:
  Reads hal.yaml -> ~/.hal/config.yaml -> legacy Neo paths -> defaults.
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


def _make_agent(config: Config, cwd: Path, session: Session | None = None, interactive: bool = False,
                out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> tuple[Agent, list, dict]:
    skills = load_skills(cwd) if config.skills else []
    phases = resolve_phases(config)
    provider = create_provider(config)
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

    agent = Agent(provider, config.model, system, default_registry(
                      cwd, workspace_root(cwd), config.tool_approvals if interactive else None,
                      confirm if interactive else None, config.git_backend),
                  messages=session.messages if session else None, usage=session.usage if session else None,
                  on_event=event)
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


def run_sessions(args: list[str], stdout: TextIO, stderr: TextIO) -> int:
    store = SessionStore()
    try:
        if not args:
            items = store.list()
            if not items: print("no saved sessions", file=stdout); return 0
            print("ID\tUPDATED\tMODEL\tCWD\tTITLE", file=stdout)
            for x in items: print(f"{x.id}\t{x.updated_at[:16].replace('T', ' ')}\t{x.model}\t{_short(x.cwd)}\t{x.title or '(untitled)'}", file=stdout)
            return 0
        if args[0] == "search" and len(args) >= 2:
            results = store.search(" ".join(args[1:]))
            if not results: print("no matching sessions", file=stdout); return 0
            print("ID\tUPDATED\tMODEL\tCWD\tTITLE\tMATCH", file=stdout)
            for x, excerpt in results: print(f"{x.id}\t{x.updated_at[:16].replace('T', ' ')}\t{x.model}\t{_short(x.cwd)}\t{x.title or '(untitled)'}\t{excerpt}", file=stdout)
            return 0
        print("usage: hal sessions [search <query>]", file=stderr); return 2
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
        print(f"HAL · {cfg.provider}/{cfg.model} · {cwd}", file=stdout)
        print("Type /help for commands; Ctrl-D or /exit to quit.", file=stdout)
        while True:
            try: text = input("hal> ").strip()
            except (EOFError, KeyboardInterrupt): print(file=stdout); break
            if not text: continue
            if text in {"/exit", "/quit"}: break
            if text == "/help":
                names = ", ".join(f"/{x}" for x in phases) + (", " + ", ".join(f"/{x.name}" for x in skills) if skills else "")
                print(f"Commands: /help, /clear, /model <id>, /exit; phases/skills: {names}", file=stdout); continue
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
            expanded, display = expand_user_input(text, skills, phases)
            cancellation = CancellationToken()
            try:
                with cancel_on_sigint(cancellation):
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
    if not args: return run_chat(stdout, stderr)
    command, rest = args[0], args[1:]
    if command == "chat": return run_chat(stdout, stderr)
    if command == "run": return run_headless(rest, stdin, stdout, stderr)
    if command == "sessions": return run_sessions(rest, stdout, stderr)
    if command == "doctor": return run_doctor(stdout)
    if command == "resume":
        if not rest: print("usage: hal resume <session-id>", file=stderr); return 2
        return run_chat(stdout, stderr, rest[0])
    if command in {"version", "-v", "--version"}: print(f"hal version {__version__}", file=stdout); return 0
    if command in {"help", "-h", "--help"}: print(USAGE, file=stdout); return 0
    if command == "login": print("login: ChatGPT subscription auth is not supported by the Python port; configure openai_auth: api_key", file=stderr); return 1
    if command == "logout": print("logout: no Python-port subscription credentials are stored", file=stdout); return 0
    print(f"unknown command: {command}", file=stderr); print(USAGE, file=stdout); return 2
