from __future__ import annotations

import argparse
from contextlib import nullcontext
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
from .harness import compose_run_budgets, compose_tool_policy, resolve_capability
from .providers import ProviderError, create_provider
from .sayings import startup_saying
from .sessions import Metadata, Session, SessionStore, short_session_id
from .journal import RunJournalStore
from .tools import BashTool, default_registry, workspace_root
from .workflows import WORKFLOWS, parse_workflow_command, run_workflow
from .workflow_inspect import (
    inspect_builtin_workflow, inspect_repository_workflow, workflow_summary,
)
from .workflow_schema import WorkflowNodeStatus, discover_workflows, resolve_workflow_names
from .workflow_artifacts import WorkflowArtifactStore
from .workflow_approvals import (
    WorkflowApprovalDecision, authorize_approval_decision, pending_approval,
)
from .workflow_budgets import WorkflowBudgets, WorkflowUsage
from .workflow_nodes import WorkflowNodeDispatcher
from .workflow_migration import migrate_workflow_definition
from .workflow_policy import (
    WorkflowTrustGrant, require_workflow_trust, workflow_required_effects,
    workflow_requires_trust,
)
from .workflow_publication import require_publication_isolation
from .workflow_runtime import materialize_workflow_inputs
from .workflow_worktrees import (
    WorkflowWorkspaceLock, cleanup_isolated_worktree, create_isolated_worktree, inspect_worktree,
    preflight_worktree, validate_worktree_resume,
)
from .workflow_resume import resume_persisted_workflow
from .workflow_state import WorkflowRunStore
from .subagents import DelegateTool


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
  hal harness [name] [--json]
                     Inspect resolved harness policy without starting a model run
  hal workflow list [--json]
  hal workflow inspect <name> [--json]
                     Inspect workflow definitions without executing them
  hal workflow run <name> [--input name=value] [--trust-digest sha256] [--json]
                     Start a validated repository workflow in the current workspace
  hal workflow runs <list|status|events|resume|retry-node|cancel|archive> ...
                     Inspect or recover durable workflow runs
  hal resume <selector>
                     Resume by full ID or unique short selector
  hal version        Show the HAL version (also -v, --version)
  hal help           Show this help

CONFIG:
  Reads hal.yaml -> ~/.hal/config.yaml -> defaults.
  Providers: anthropic (default), openai, openrouter, google, or meta.

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
    registry = _make_registry(
        config, cwd, root, interactive,
        (confirm_handler or confirm) if interactive else None,
    )
    agent = Agent(provider, config.model, system, registry,
                  messages=session.messages if session else None, usage=session.usage if session else None,
                  on_event=event_handler or event, budgets=config.harness_budgets,
                  capability=(
                      resolve_capability(config.default_capability, config.capabilities)
                      if config.default_capability else None
                  ), verification_checks=config.verification_checks,
                  workspace=root, repair_attempts=config.repair_attempts,
                  max_output_tokens=config.max_output_tokens,
                  max_output_continuations=config.max_output_continuations,
                  reasoning_effort=config.reasoning_effort,
                  journal_store=RunJournalStore(SessionStore().directory / "runs"))
    registry.bind_agent(agent)
    return agent, skills, phases


def _make_registry(
    config: Config, cwd: Path, root: Path, interactive: bool = False,
    confirm_handler: Callable[[str], bool] | None = None,
):
    registry = default_registry(
        cwd, root, config.tool_approvals if interactive else None,
        confirm_handler if interactive else None, config.git_backend,
        config.only_write_locally, config.bash_policy,
    )
    if config.subagents:
        registry.extend([DelegateTool(config.subagents)])
    load_extensions(registry, config.extensions, cwd, root, config.extension_config)
    registry_tools = {spec.name for spec in registry.specs}
    for capability in config.capabilities.values():
        referenced = set(capability.allowed_tools or ()) | set(capability.denied_tools)
        unknown = referenced - registry_tools
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(
                f"capability {capability.name!r} references unknown tool(s): {names}"
            )
    return registry


def run_harness(args: list[str], stdout: TextIO, stderr: TextIO) -> int:
    json_output = "--json" in args
    names = [item for item in args if item != "--json"]
    if len(names) > 1 or any(item.startswith("-") for item in names):
        print("usage: hal harness [capability] [--json]", file=stderr)
        return 2
    cwd = Path.cwd()
    config = _load(cwd, stderr)
    if config is None:
        return 1
    name = names[0] if names else (config.default_capability or "change")
    try:
        capability = resolve_capability(name, config.capabilities)
        root = workspace_root(cwd)
        registry = _make_registry(config, cwd, root)
        policy = compose_tool_policy(capability)
        budgets = compose_run_budgets(config.harness_budgets, capability.budgets)
        available = [
            spec.name for spec in registry.specs_for(
                None if policy.allowed_tools is None else set(policy.allowed_tools),
                set(policy.denied_tools),
            )
        ]
        payload = {
            "capability": capability.name,
            "description": capability.description,
            "workspace": str(root),
            "available_tools": available,
            "tool_metadata": {
                tool_name: registry.metadata(tool_name)
                for tool_name in available
            },
            "allowed_tools": (
                sorted(policy.allowed_tools)
                if policy.allowed_tools is not None else None
            ),
            "denied_tools": sorted(policy.denied_tools),
            "protect_existing_files": policy.protect_existing_files,
            "budgets": (
                {
                    field: getattr(budgets, field)
                    for field in (
                        "provider_calls", "tool_calls", "elapsed_seconds",
                        "input_tokens", "output_tokens",
                    )
                }
                if budgets is not None else None
            ),
            "verification": [check.name for check in config.verification_checks],
            "repair_attempts": config.repair_attempts,
            "max_output_tokens": config.max_output_tokens,
            "max_output_continuations": config.max_output_continuations,
            "reasoning_effort": config.reasoning_effort or None,
            "bash_policy": config.bash_policy,
            "only_write_locally": config.only_write_locally,
            "tool_approvals": list(config.tool_approvals),
        }
    except (OSError, ValueError) as exc:
        print(f"harness: {exc}", file=stderr)
        return 1
    if json_output:
        print(json.dumps(payload, indent=2), file=stdout)
    else:
        print(f"Capability: {payload['capability']} — {payload['description']}", file=stdout)
        print(f"Workspace: {payload['workspace']}", file=stdout)
        print(f"Available tools: {', '.join(available) or 'none'}", file=stdout)
        print(f"Denied tools: {', '.join(payload['denied_tools']) or 'none'}", file=stdout)
        print(f"Protect existing files: {str(policy.protect_existing_files).lower()}", file=stdout)
        print(f"Budgets: {json.dumps(payload['budgets'], sort_keys=True)}", file=stdout)
        print(
            "Provider output: "
            f"{payload['max_output_tokens']} tokens, "
            f"{payload['max_output_continuations']} continuation(s)",
            file=stdout,
        )
        print(
            f"Reasoning effort: {payload['reasoning_effort'] or 'provider default'}",
            file=stdout,
        )
        print(f"Verification: {', '.join(payload['verification']) or 'none'}", file=stdout)
        print(f"Repair attempts: {config.repair_attempts}", file=stdout)
    return 0


def run_workflow_inspection(args: list[str], stdout: TextIO, stderr: TextIO) -> int:
    """List or inspect workflow definitions without executing any node."""
    if args and args[0] == "runs":
        return run_workflow_runs(args[1:], stdout, stderr)
    if args and args[0] == "run":
        return run_repository_workflow(args[1:], stdout, stderr)
    json_output = "--json" in args
    positional = [item for item in args if item != "--json"]
    if any(item.startswith("-") for item in positional):
        print("usage: hal workflow list [--json] | inspect <name> [--json]", file=stderr)
        return 2
    if not positional or positional[0] not in {"list", "inspect"}:
        print("usage: hal workflow list [--json] | inspect <name> [--json]", file=stderr)
        return 2
    action = positional[0]
    if (action == "list" and len(positional) != 1) or (
        action == "inspect" and len(positional) != 2
    ):
        print("usage: hal workflow list [--json] | inspect <name> [--json]", file=stderr)
        return 2
    root = workspace_root(Path.cwd())
    try:
        repository = discover_workflows(root)
        names = resolve_workflow_names(frozenset(WORKFLOWS), repository)
        payloads = {
            name: (
                inspect_builtin_workflow(WORKFLOWS[name])
                if name in WORKFLOWS else inspect_repository_workflow(repository[name])
            )
            for name in names
        }
    except (OSError, ValueError) as exc:
        print(f"workflow: {exc}", file=stderr)
        return 1
    if action == "inspect":
        name = positional[1].lower()
        if name not in payloads:
            print(
                f"workflow: unknown workflow {name!r} (available: {', '.join(names)})",
                file=stderr,
            )
            return 1
        payload: Any = payloads[name]
    else:
        payload = {
            "workspace": str(root),
            "workflows": [workflow_summary(payloads[name]) for name in names],
        }
    if json_output:
        print(json.dumps(payload, indent=2), file=stdout)
    elif action == "list":
        for item in payload["workflows"]:
            effects = ",".join(item["effects"]) or "none"
            print(
                f"{item['name']}\t{item['origin']}\ttrust={str(item['trust_required']).lower()}"
                f"\teffects={effects}\t{item['description']}",
                file=stdout,
            )
    else:
        print(json.dumps(payload, indent=2), file=stdout)
    return 0


def run_repository_workflow(args: list[str], stdout: TextIO, stderr: TextIO) -> int:
    """Start one explicitly named repository workflow under pinned trust."""
    parser = argparse.ArgumentParser(prog="hal workflow run", add_help=True)
    parser.add_argument("name")
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--trust-digest")
    parser.add_argument("--json", action="store_true", dest="json_output")
    try:
        options = parser.parse_args(args)
    except SystemExit as exc:
        return int(exc.code)
    root = workspace_root(Path.cwd())
    try:
        definitions = discover_workflows(root)
        if options.name not in definitions:
            available = ", ".join(definitions) or "none"
            raise ValueError(
                f"unknown repository workflow {options.name!r} (available: {available})"
            )
        definition = definitions[options.name]
        require_publication_isolation(definition)
        if (
            any(node.type == "approval" for node in definition.nodes)
            and definition.execution.workspace != "worktree"
        ):
            raise ValueError(
                "approval workflows require execution.workspace: worktree so reviewed "
                "workspace state can be checkpointed"
            )
        if (
            any(
                node.type == "git"
                and node.config.get("operation") in {"stage", "commit", "prepare_branch"}
                for node in definition.nodes
            )
            and definition.execution.workspace != "worktree"
        ):
            raise ValueError(
                "mutating Git workflow nodes require execution.workspace: worktree"
            )
        supplied = _parse_workflow_cli_inputs(definition, options.input)
        validated_inputs = materialize_workflow_inputs(definition, supplied)
        grant = None
        if options.trust_digest is not None:
            grant = WorkflowTrustGrant(
                definition.name, root.resolve(), options.trust_digest,
                workflow_required_effects(definition),
            )
        try:
            require_workflow_trust(definition, grant)
        except PermissionError as exc:
            raise PermissionError(
                f"{exc}; inspect it with 'hal workflow inspect {definition.name}' and "
                f"rerun with --trust-digest {definition.source.digest}"
            ) from exc
        cancellation = CancellationToken()
        config = None
        agent = None
        needs_agent = _workflow_needs_agent(definition, definitions)
        if needs_agent:
            config = _load(root, stderr)
            if config is None:
                raise ValueError("could not load HAL configuration for workflow agent nodes")
        store = WorkflowRunStore(root / ".hal" / "runs")
        state = store.create(
            definition, validated_inputs, root, definition.execution.budgets,
        )
        node_positions = {
            node.id: (index, node.type)
            for index, node in enumerate(definition.nodes, start=1)
        }

        def report_progress(node_id, status, elapsed_seconds, reason):
            index, node_type = node_positions[node_id]
            prefix = (
                f"[workflow {state.run_id}] "
                f"{index}/{len(definition.nodes)} {node_id} ({node_type})"
            )
            if status is WorkflowNodeStatus.RUNNING:
                message = f"{prefix}: started"
            else:
                duration = (
                    f" in {elapsed_seconds:.1f}s"
                    if elapsed_seconds is not None else ""
                )
                message = f"{prefix}: {status.value}{duration}"
                if reason and status is not WorkflowNodeStatus.SUCCEEDED:
                    concise_reason = " ".join(str(reason).split())
                    message += f" — {concise_reason[:300]}"
            print(message, file=stderr, flush=True)

        print(
            f"[workflow {state.run_id}] {definition.name}: started "
            f"({len(definition.nodes)} nodes)",
            file=stderr,
            flush=True,
        )
        if workflow_requires_trust(definition):
            state.attach_trust(
                definition.source.digest,
                tuple(sorted(effect.value for effect in workflow_required_effects(definition))),
            )
        execution_root = root
        if definition.execution.workspace == "worktree":
            preflight = preflight_worktree(
                root, state.run_id, definition.name, cancellation,
            )
            execution_root = create_isolated_worktree(preflight, cancellation)
            state.attach_workspace(
                execution_root, branch=preflight.branch, head=preflight.head,
                source_branch=preflight.source_branch,
                source_dirty_paths=preflight.dirty_paths,
            )
            identity = inspect_worktree(root, execution_root, cancellation)
            state.update_workspace_checkpoint(**_worktree_snapshot(identity))
        if needs_agent:
            assert config is not None
            agent, _skills, _phases = _make_agent(config, execution_root)
        artifacts = WorkflowArtifactStore(
            root / ".hal" / "runs" / "artifacts" / state.run_id
        )
        dispatcher = WorkflowNodeDispatcher(
            execution_root, cancellation, agent=agent,
            capabilities=config.capabilities if config is not None else {},
            budgets=definition.execution.budgets,
            artifact_store=artifacts, workflows=definitions,
            git_backend=getattr(config, "git_backend", "auto"),
            external_intent=state.record_external_intent,
        )
        from .workflow_state import (
            execute_persisted_concurrent_workflow, execute_persisted_workflow,
        )
        lock = (
            WorkflowWorkspaceLock(root / ".hal" / "locks", execution_root, state.run_id)
            if workflow_requires_trust(definition) else nullcontext()
        )
        snapshotter = (
            lambda: _worktree_snapshot(inspect_worktree(root, execution_root, cancellation))
        ) if definition.execution.workspace == "worktree" else None
        with cancel_on_sigint(cancellation), dispatcher.workflow_scope(definition), lock:
            if definition.execution.max_parallel > 1:
                result = execute_persisted_concurrent_workflow(
                    definition, validated_inputs, dispatcher, state,
                    usage=lambda: dispatcher.ledger.usage,
                    workspace_snapshot=snapshotter,
                    on_progress=report_progress,
                )
            else:
                result = execute_persisted_workflow(
                    definition, validated_inputs, dispatcher, state,
                    usage=lambda: dispatcher.ledger.usage,
                    workspace_snapshot=snapshotter,
                    on_progress=report_progress,
                )
        payload = {
            "run_id": state.run_id,
            "workflow": definition.name,
            "digest": definition.source.digest,
            "status": result.status.value,
            "nodes": [
                {"id": item.node_id, "status": item.status.value, "reason": item.reason}
                for item in result.nodes
            ],
        }
    except (FileNotFoundError, OSError, PermissionError, ValueError) as exc:
        print(f"workflow run: {exc}", file=stderr)
        return 1
    if options.json_output:
        print(json.dumps(payload, indent=2), file=stdout)
    else:
        print(f"Workflow run: {payload['run_id']}", file=stdout)
        print(f"Status: {payload['status']}", file=stdout)
        for node in payload["nodes"]:
            print(f"{node['id']}\t{node['status']}\t{node['reason'] or ''}", file=stdout)
    return 0 if result.status.value in {"succeeded", "waiting"} else 1


def _parse_workflow_cli_inputs(definition, values: list[str]) -> dict[str, object]:
    supplied: dict[str, object] = {}
    for value in values:
        name, separator, raw = value.partition("=")
        if not separator or not name:
            raise ValueError("workflow inputs must use --input name=value")
        if name in supplied:
            raise ValueError(f"workflow input {name!r} was supplied more than once")
        item = definition.inputs.get(name)
        if item is None:
            raise ValueError(f"unknown workflow input {name!r}")
        try:
            if item.type == "boolean":
                if raw.lower() not in {"true", "false"}:
                    raise ValueError("expected true or false")
                parsed: object = raw.lower() == "true"
            elif item.type == "integer":
                parsed = int(raw)
            elif item.type in {"json", "check_result"}:
                parsed = json.loads(raw)
            else:
                parsed = raw
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"workflow input {name!r} is not valid {item.type}: {exc}"
            ) from exc
        supplied[name] = parsed
    return supplied


def _workflow_needs_agent(definition, definitions, seen=frozenset()) -> bool:
    if definition.name in seen:
        return False
    seen = seen | {definition.name}
    for node in definition.nodes:
        if node.type == "agent":
            return True
        if node.type == "workflow" and node.config["workflow"] in definitions:
            if _workflow_needs_agent(definitions[node.config["workflow"]], definitions, seen):
                return True
    return False


def run_workflow_runs(args: list[str], stdout: TextIO, stderr: TextIO) -> int:
    """Inspect and recover durable workflow runs through one CLI contract."""
    json_output = "--json" in args
    positional = [item for item in args if item != "--json"]
    if positional and positional[0] in {
        "approval", "approve", "deny", "request-changes", "cancel-approval",
    }:
        return _run_workflow_approval_cli(args, stdout, stderr)
    usage_text = (
        "usage: hal workflow runs list [--json] | status|events|resume|cancel|archive "
        "<run-id> [--json] | retry-node <run-id> <node-id> [--json] | cleanup <run-id> | "
        "migrate <run-id> <workflow-name> [--json] | trust <run-id> <digest> [--json]"
    )
    if not positional or any(item.startswith("-") for item in positional):
        print(usage_text, file=stderr)
        return 2
    action = positional[0]
    expected = {"list": 1, "status": 2, "events": 2, "resume": 2,
                "retry-node": 3, "migrate": 3, "trust": 3, "cancel": 2, "cleanup": 2,
                "archive": 2}
    if action not in expected or len(positional) != expected[action]:
        print(usage_text, file=stderr)
        return 2
    root = workspace_root(Path.cwd())
    store = WorkflowRunStore(root / ".hal" / "runs")
    try:
        if action == "list":
            payload: object = [
                {
                    "run_id": state.run_id,
                    "workflow": state.payload["workflow"]["name"],
                    "status": state.payload["status"],
                    "revision": state.payload["revision"],
                    "updated_at": state.payload["updated_at"],
                    "workspace": state.payload["workspace"]["path"],
                }
                for state in store.list()
            ]
        else:
            run_id = positional[1]
            state = store.load(run_id)
            if action == "status":
                payload = state.payload
            elif action == "events":
                payload = {
                    "run_id": run_id, "events": state.payload["events"],
                }
            elif action == "cancel":
                state.request_cancel()
                payload = {"run_id": run_id, "status": state.payload["status"],
                           "cancellation_requested": True}
            elif action == "archive":
                archived = store.archive(run_id)
                payload = {"run_id": run_id, "archived": True, "path": str(archived)}
            elif action == "cleanup":
                if state.payload["status"] not in {
                    "succeeded", "failed", "denied", "cancelled", "timed_out",
                    "budget_exhausted", "interrupted",
                }:
                    raise ValueError("workflow worktree cleanup requires a terminal run")
                workspace = state.payload["workspace"]
                if not workspace.get("branch"):
                    raise ValueError("workflow run does not own an isolated worktree")
                repository = Path(state.payload["workflow"]["repository"])
                cleanup_isolated_worktree(repository, workspace)
                state.mark_workspace_cleaned()
                payload = {"run_id": run_id, "cleaned": True}
            elif action == "migrate":
                repository = Path(state.payload["workflow"]["repository"])
                definitions = discover_workflows(repository)
                target = positional[2]
                if target not in definitions:
                    raise ValueError(f"unknown migration workflow {target!r}")
                migrate_workflow_definition(
                    state, definitions[target], actor="local-cli",
                    reason="explicit CLI migration",
                )
                payload = {
                    "run_id": run_id, "migrated": True,
                    "digest": state.payload["workflow"]["digest"],
                    "revision": state.payload["revision"],
                }
            elif action == "trust":
                repository = Path(state.payload["workflow"]["repository"])
                definitions = discover_workflows(repository)
                name = state.payload["workflow"]["name"]
                if name not in definitions:
                    raise ValueError(f"pinned workflow {name!r} is no longer available")
                definition = definitions[name]
                supplied_digest = positional[2]
                if supplied_digest != definition.source.digest:
                    raise ValueError("trust digest does not match the current workflow definition")
                state.attach_trust(
                    supplied_digest,
                    tuple(sorted(
                        effect.value for effect in workflow_required_effects(definition)
                    )),
                )
                payload = {
                    "run_id": run_id, "trusted": True,
                    "digest": supplied_digest,
                    "effects": state.payload["trust"]["effects"],
                }
            else:
                retry_nodes = (
                    frozenset({positional[2]}) if action == "retry-node" else frozenset()
                )
                payload = _resume_workflow_run(
                    state, retry_nodes, stdout, stderr,
                )
    except (FileNotFoundError, OSError, PermissionError, ValueError) as exc:
        print(f"workflow runs: {exc}", file=stderr)
        return 1
    if json_output:
        print(json.dumps(payload, indent=2), file=stdout)
    elif action == "list":
        for item in payload:
            print(
                f"{item['run_id']}\t{item['status']}\t{item['workflow']}\t{item['updated_at']}",
                file=stdout,
            )
    elif action == "events":
        for event in payload["events"]:
            print(
                f"{event['sequence']}\t{event['timestamp']}\t{event['event']}\t"
                f"{event.get('node_id', '')}\t{event.get('status', '')}",
                file=stdout,
            )
    else:
        print(json.dumps(payload, indent=2), file=stdout)
    return 0


def _run_workflow_approval_cli(
    args: list[str], stdout: TextIO, stderr: TextIO,
) -> int:
    action = args[0]
    parser = argparse.ArgumentParser(prog=f"hal workflow runs {action}")
    parser.add_argument("run_id")
    parser.add_argument("node_id")
    parser.add_argument("--revision")
    parser.add_argument("--approver")
    parser.add_argument("--feedback", default="")
    parser.add_argument("--json", action="store_true", dest="json_output")
    try:
        options = parser.parse_args(args[1:])
    except SystemExit as exc:
        return int(exc.code)
    root = workspace_root(Path.cwd())
    store = WorkflowRunStore(root / ".hal" / "runs")
    try:
        state = store.load(options.run_id)
        approval = pending_approval(state, options.node_id)
        if action == "approval":
            payload: object = approval
        else:
            if not options.revision or not options.approver:
                raise ValueError("approval decisions require --revision and --approver")
            workspace = state.payload["workspace"]
            if workspace.get("branch"):
                identity = inspect_worktree(root, Path(workspace["path"]))
                state.update_workspace_checkpoint(**_worktree_snapshot(identity))
            decision_name = {
                "approve": "approve", "deny": "deny",
                "request-changes": "request_changes", "cancel-approval": "cancel",
            }[action]
            repository = Path(state.payload["workflow"]["repository"])
            definitions = discover_workflows(repository)
            workflow_name = state.payload["workflow"]["name"]
            if workflow_name not in definitions:
                raise ValueError(f"pinned workflow {workflow_name!r} is no longer available")
            artifact_store = WorkflowArtifactStore(
                repository / ".hal" / "runs" / "artifacts" / state.run_id
            )
            authorize_approval_decision(state, definitions[workflow_name], artifact_store,
                WorkflowApprovalDecision(
                options.node_id, decision_name, options.approver,
                options.feedback, options.revision,
            ))
            if decision_name == "approve":
                payload = _resume_workflow_run(state, frozenset(), stdout, stderr)
            else:
                payload = {
                    "run_id": state.run_id, "node_id": options.node_id,
                    "decision": decision_name, "status": state.payload["status"],
                }
    except (FileNotFoundError, OSError, PermissionError, ValueError) as exc:
        print(f"workflow approval: {exc}", file=stderr)
        return 1
    if options.json_output:
        print(json.dumps(payload, indent=2), file=stdout)
    else:
        print(json.dumps(payload, indent=2), file=stdout)
    return 0


def _resume_workflow_run(
    state, retry_nodes: frozenset[str], stdout: TextIO, stderr: TextIO,
) -> dict[str, object]:
    payload = state.payload
    repository = Path(payload["workflow"]["repository"])
    workspace = Path(payload["workspace"]["path"])
    definitions = discover_workflows(repository)
    name = payload["workflow"]["name"]
    if name not in definitions:
        raise ValueError(f"pinned workflow {name!r} is no longer available")
    require_publication_isolation(definitions[name])
    config = _load(workspace, stderr)
    if config is None:
        raise ValueError("could not load HAL configuration for the workflow workspace")
    agent, _skills, _phases = _make_agent(config, workspace)
    budgets = WorkflowBudgets(**payload["budgets"])
    prior_usage = WorkflowUsage(**payload["usage"])
    cancellation = CancellationToken()
    is_worktree = bool(payload["workspace"].get("branch"))
    if is_worktree:
        identity = inspect_worktree(repository, workspace, cancellation)
        validate_worktree_resume(
            payload["workspace"], identity,
            allow_checkpoint_change=bool(retry_nodes),
        )
    artifact_store = WorkflowArtifactStore(
        repository / ".hal" / "runs" / "artifacts" / state.run_id
    )
    dispatcher = WorkflowNodeDispatcher(
        workspace, cancellation, agent=agent, capabilities=config.capabilities,
        budgets=budgets, usage=prior_usage,
        artifact_store=artifact_store,
        workflows=definitions,
        git_backend=config.git_backend,
        external_intent=state.record_external_intent,
    )
    lock = (
        WorkflowWorkspaceLock(repository / ".hal" / "locks", workspace, state.run_id)
        if workflow_requires_trust(definitions[name]) else nullcontext()
    )
    snapshotter = (
        lambda: _worktree_snapshot(inspect_worktree(repository, workspace, cancellation))
    ) if is_worktree else None
    with cancel_on_sigint(cancellation), lock:
        result = resume_persisted_workflow(
            state, definitions[name], dispatcher.artifact_store, dispatcher,
            retry_nodes=retry_nodes,
            usage=lambda: dispatcher.ledger.usage,
            workspace_snapshot=snapshotter,
            max_parallel=definitions[name].execution.max_parallel,
        )
    return {
        "run_id": state.run_id,
        "status": result.status.value,
        "nodes": [
            {"id": node.node_id, "status": node.status.value, "reason": node.reason}
            for node in result.nodes
        ],
    }


def _worktree_snapshot(identity) -> dict[str, object]:
    return {
        "head": identity.head, "branch": identity.branch,
        "dirty_digest": identity.dirty_digest, "dirty_paths": identity.dirty_paths,
    }


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
    agent: Agent | None = None
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
    outcome = getattr(agent, "last_outcome", None)
    if outcome is not None:
        counters = outcome.counters
        result["harness"] = {
            "run_id": outcome.run_id,
            "status": outcome.status.value,
            "reason": outcome.reason,
            "capability": outcome.capability,
            "provider_calls": counters.provider_calls,
            "tool_calls": counters.tool_calls,
            "elapsed_seconds": counters.elapsed_seconds,
            "input_tokens": counters.usage.input_tokens,
            "output_tokens": counters.usage.output_tokens,
            "repair_attempts": outcome.repair_attempts,
            "verification": [
                {
                    "name": item.name,
                    "status": item.status.value,
                    "passed": item.passed,
                    "required": item.required,
                    "duration_ms": item.duration_ms,
                    "returncode": item.returncode,
                }
                for item in outcome.verification
            ],
        }
    result.update(elapsed_ms=int((time.monotonic() - started) * 1000), tool_calls=calls, tool_errors=errors)
    if options.json_output: print(json.dumps(result), file=stdout)
    elif result["ok"]: print(result.get("final", ""), file=stdout)
    else: print(f"hal run: {result.get('error', 'failed')}", file=stderr)
    if result["ok"]:
        return 0
    harness = result.get("harness")
    status = harness.get("status") if isinstance(harness, dict) else ""
    reason = harness.get("reason") if isinstance(harness, dict) else ""
    if status == "cancelled":
        return 130
    if status == "budget_exhausted":
        return 3
    if reason == "verification_failed":
        return 4
    return 1


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
    interactive = bool(
        getattr(stdin, "isatty", lambda: False)()
        and getattr(stdout, "isatty", lambda: False)()
    )
    if not interactive:
        return False
    try:
        stdout.fileno()
        real_terminal = True
    except (AttributeError, OSError, ValueError):
        real_terminal = False
    return not real_terminal or os.environ.get("TERM", "").lower() != "dumb"


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
    explicitly_disabled = os.environ.get("HAL_NO_TUI", "").strip().lower() in {
        "1", "true", "yes",
    }
    if not explicitly_disabled and (missing := _missing_tui_dependencies()):
        names = ", ".join(missing)
        guidance = "run 'python -m pip install -e \".[tui]\"' to install the TUI dependencies"
        if require_tui:
            print(f"hal tui: missing {names}; {guidance}", file=stderr)
            return 1
        print(f"warning: TUI unavailable (missing {names}); using basic REPL; {guidance}", file=stderr)
        return run_chat(stdout, stderr, session_id)
    if not _tui_supported(stdin, stdout):
        if require_tui:
            print(
                "hal tui: a capable interactive terminal is required; use "
                "'hal chat --no-tui' for the basic interface",
                file=stderr,
            )
            return 1
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
    if command == "harness": return run_harness(rest, stdout, stderr)
    if command == "workflow": return run_workflow_inspection(rest, stdout, stderr)
    if command == "resume":
        if not rest: print("usage: hal resume <short-id-or-full-id>", file=stderr); return 2
        if len(rest) > 1: print("usage: hal resume <short-id-or-full-id>", file=stderr); return 2
        return _run_interactive(stdin, stdout, stderr, rest[0])
    if command in {"version", "-v", "--version"}: print(f"hal version {__version__}", file=stdout); return 0
    if command in {"help", "-h", "--help"}: print(USAGE, file=stdout); return 0
    if command == "login": print("login: ChatGPT subscription auth is not supported by the Python port; configure openai_auth: api_key", file=stderr); return 1
    if command == "logout": print("logout: no Python-port subscription credentials are stored", file=stdout); return 0
    print(f"unknown command: {command}", file=stderr); print(USAGE, file=stdout); return 2
