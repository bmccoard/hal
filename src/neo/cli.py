from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import TextIO

from . import __version__
from .agent import Agent
from .config import Config, load_config
from .context import build_system, expand_user_input, load_skills, resolve_phases
from .providers import ProviderError, create_provider
from .sessions import Metadata, Session, SessionStore
from .tools import default_registry, workspace_root


USAGE = """neo — a Python coding agent

USAGE:
  neo                Interactive chat mode (default)
  neo chat           Interactive chat mode (explicit)
  neo run [options] <prompt>
                     Run one headless prompt and exit
  neo sessions       List saved chat sessions
  neo sessions search <query>
                     Search saved session transcripts
  neo doctor         Check local config and environment
  neo resume <id>    Resume a saved chat session
  neo version        Show the Neo version (also -v, --version)
  neo help           Show this help

CONFIG:
  Reads neo.yaml (cwd) -> ~/.neo/config.yaml -> embedded defaults.
  Providers: anthropic (default), openai, openrouter, or google.

HEADLESS RUN:
  neo run --json --timeout 10m "Review this repo without changing files"
  cat prompt.md | neo run --json
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

    def event(kind: str, data: dict[str, object]) -> None:
        if not interactive: return
        if kind == "assistant":
            print(data["text"], end="", flush=True, file=out)
        elif kind == "tool_call":
            print(f"\n-> {data['name']}", file=err)
        elif kind == "tool_result" and bool(data.get("is_error")):
            print(f"  error: {data['content']}", file=err)

    def confirm(prompt: str) -> bool:
        try: return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt): return False

    agent = Agent(provider, config.model, system, default_registry(
                      cwd, workspace_root(cwd), config.tool_approvals if interactive else None,
                      confirm if interactive else None),
                  messages=session.messages if session else None, usage=session.usage if session else None,
                  on_event=event)
    return agent, skills, phases


def run_headless(args: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if any(x == "--permission" or x.startswith("--permission=") for x in args):
        print("--permission has been removed; run Neo inside a sandbox and use tool_approvals for optional interactive confirmations", file=stderr); return 2
    parser = argparse.ArgumentParser(prog="neo run", add_help=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--timeout", type=_duration, default=600.0)
    parser.add_argument("prompt", nargs="*")
    try: options = parser.parse_args(args)
    except SystemExit as exc: return int(exc.code)
    parts = list(options.prompt)
    if not stdin.isatty() and (piped := stdin.read().strip()): parts.insert(0, piped)
    prompt = " ".join(parts).strip()
    if not prompt:
        print("neo run: missing prompt", file=stderr); return 2
    started = time.monotonic(); cwd = Path.cwd(); cfg = _load(cwd, stderr)
    if cfg is None: return 1
    calls = errors = 0
    result: dict[str, object] = {"ok": False, "elapsed_ms": 0, "provider": cfg.provider, "model": cfg.model, "tool_calls": 0, "tool_errors": 0}
    try:
        agent, _, _ = _make_agent(cfg, cwd, err=stderr)
        if hasattr(agent.provider, "timeout"): agent.provider.timeout = min(agent.provider.timeout, options.timeout)
        def count(kind: str, data: dict[str, object]) -> None:
            nonlocal calls, errors
            if kind == "tool_call": calls += 1
            elif kind == "tool_result" and data.get("is_error"): errors += 1
        agent.on_event = count
        final = agent.send(prompt)
        result.update(ok=True, final=final)
    except (ProviderError, OSError, ValueError, RuntimeError) as exc:
        result["error"] = str(exc)
    result.update(elapsed_ms=int((time.monotonic() - started) * 1000), tool_calls=calls, tool_errors=errors)
    if options.json_output: print(json.dumps(result), file=stdout)
    elif result["ok"]: print(result.get("final", ""), file=stdout)
    else: print(f"neo run: {result.get('error', 'failed')}", file=stderr)
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
        print("usage: neo sessions [search <query>]", file=stderr); return 2
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
    checks: list[tuple[str, str, str]] = []; failed = False
    try:
        cfg = load_config(); checks.extend([("pass", "config", f"loaded {cfg.source}"), ("pass", "provider", cfg.provider), (*_credential_status(cfg), "")])
        status, detail = _credential_status(cfg); checks[-1] = (status, "credentials", detail); checks.append(("pass", "model", cfg.model)); failed |= status == "fail"
    except ValueError as exc:
        checks.append(("fail", "config", str(exc))); failed = True
    directory = SessionStore().directory; checks.append(("pass" if directory.is_dir() else "warn", "sessions", f"store {'is available' if directory.is_dir() else 'will be created'} at {_short(str(directory))}"))
    git = shutil.which("git")
    checks.append(("pass" if git else "fail", "git", "git executable found" if git else "git executable not found in PATH")); failed |= not bool(git)
    if git:
        result = subprocess.run([git, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        checks.append(("pass" if result.returncode == 0 else "warn", "workspace", f"git root {_short(result.stdout.strip())}" if result.returncode == 0 else "current directory is not a git workspace"))
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
        print(f"Neo Python · {cfg.provider}/{cfg.model} · {cwd}", file=stdout)
        print("Type /help for commands; Ctrl-D or /exit to quit.", file=stdout)
        while True:
            try: text = input("neo> ").strip()
            except (EOFError, KeyboardInterrupt): print(file=stdout); break
            if not text: continue
            if text in {"/exit", "/quit"}: break
            if text == "/help":
                names = ", ".join(f"/{x}" for x in phases) + (", " + ", ".join(f"/{x.name}" for x in skills) if skills else "")
                print(f"Commands: /help, /clear, /model <id>, /exit; phases/skills: {names}", file=stdout); continue
            if text == "/clear": agent.messages.clear(); agent.usage = type(agent.usage)(); print("Conversation cleared.", file=stdout); continue
            if text.startswith("/model "): agent.model = text[7:].strip(); session.metadata.model = agent.model; print(f"Model: {agent.model}", file=stdout); continue
            if text.startswith("!"):
                subprocess.run(text[1:].strip(), cwd=cwd, shell=True); continue
            expanded, display = expand_user_input(text, skills, phases)
            try: agent.send(expanded, display); print(file=stdout)
            except (ProviderError, OSError, ValueError, RuntimeError) as exc: print(f"error: {exc}", file=stderr)
            session.messages, session.usage = agent.messages, agent.usage; store.save(session)
        session.messages, session.usage = agent.messages, agent.usage; store.save(session); return 0
    except (ProviderError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"chat: {exc}", file=stderr); return 1


def main(argv: list[str] | None = None, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args: return run_chat(stdout, stderr)
    command, rest = args[0], args[1:]
    if command == "chat": return run_chat(stdout, stderr)
    if command == "run": return run_headless(rest, stdin, stdout, stderr)
    if command == "sessions": return run_sessions(rest, stdout, stderr)
    if command == "doctor": return run_doctor(stdout)
    if command == "resume":
        if not rest: print("usage: neo resume <session-id>", file=stderr); return 2
        return run_chat(stdout, stderr, rest[0])
    if command in {"version", "-v", "--version"}: print(f"neo version {__version__}", file=stdout); return 0
    if command in {"help", "-h", "--help"}: print(USAGE, file=stdout); return 0
    if command == "login": print("login: ChatGPT subscription auth is not supported by the Python port; configure openai_auth: api_key", file=stderr); return 1
    if command == "logout": print("logout: no Python-port subscription credentials are stored", file=stdout); return 0
    print(f"unknown command: {command}", file=stderr); print(USAGE, file=stdout); return 2
