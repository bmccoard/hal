from __future__ import annotations

import io
import json
from pathlib import Path

from hal.cli import main
from hal.workflow_budgets import WorkflowBudgets
from hal.workflow_schema import WORKFLOW_DIRECTORY, load_workflow
from hal.workflow_state import WorkflowRunStore
from hal.workflow_nodes import WorkflowNodeDispatcher
from hal.workflow_state import execute_persisted_workflow


def _run(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    directory = tmp_path / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True)
    path = directory / "managed.yaml"
    path.write_text("""
version: 1
name: managed
nodes:
  - {id: work, type: agent, prompt: work}
""".lstrip(), encoding="utf-8")
    definition = load_workflow(path, tmp_path)
    store = WorkflowRunStore(tmp_path / ".hal" / "runs")
    return store, store.create(definition, {}, tmp_path, WorkflowBudgets())


def test_run_list_status_and_events_have_json_contracts(monkeypatch, tmp_path: Path) -> None:
    _store, state = _run(tmp_path)
    monkeypatch.chdir(tmp_path)

    listed, status, events = io.StringIO(), io.StringIO(), io.StringIO()
    assert main(["workflow", "runs", "list", "--json"], stdout=listed) == 0
    assert main(["workflow", "runs", "status", state.run_id, "--json"], stdout=status) == 0
    assert main(["workflow", "runs", "events", state.run_id, "--json"], stdout=events) == 0

    assert json.loads(listed.getvalue())[0]["run_id"] == state.run_id
    assert json.loads(status.getvalue())["workflow"]["name"] == "managed"
    assert json.loads(events.getvalue())["events"][0]["event"] == "run_created"


def test_cancel_then_archive_is_explicit_and_recoverable(monkeypatch, tmp_path: Path) -> None:
    store, state = _run(tmp_path)
    monkeypatch.chdir(tmp_path)
    cancelled = io.StringIO()

    assert main(
        ["workflow", "runs", "cancel", state.run_id, "--json"], stdout=cancelled,
    ) == 0
    assert json.loads(cancelled.getvalue())["status"] == "cancelled"
    assert store.load(state.run_id).payload["nodes"]["work"]["status"] == "cancelled"

    archived = io.StringIO()
    assert main(
        ["workflow", "runs", "archive", state.run_id, "--json"], stdout=archived,
    ) == 0
    payload = json.loads(archived.getvalue())
    assert payload["archived"] is True
    assert not (store.directory / f"{state.run_id}.json").exists()
    assert (store.directory / "archive" / f"{state.run_id}.json").is_file()


def test_resume_and_retry_node_use_shared_recovery_entrypoint(
    monkeypatch, tmp_path: Path,
) -> None:
    _store, state = _run(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls = []

    def resume(run_state, retry_nodes, _stdout, _stderr):
        calls.append((run_state.run_id, retry_nodes))
        return {"run_id": run_state.run_id, "status": "succeeded", "nodes": []}

    monkeypatch.setattr("hal.cli._resume_workflow_run", resume)
    assert main(
        ["workflow", "runs", "resume", state.run_id], stdout=io.StringIO(),
    ) == 0
    assert main(
        ["workflow", "runs", "retry-node", state.run_id, "work"],
        stdout=io.StringIO(),
    ) == 0
    assert calls == [(state.run_id, frozenset()), (state.run_id, frozenset({"work"}))]


def test_run_commands_reject_bad_arguments_and_missing_records(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    error = io.StringIO()
    assert main(["workflow", "runs"], stdout=io.StringIO(), stderr=error) == 2
    assert "usage:" in error.getvalue()
    error = io.StringIO()
    assert main(
        ["workflow", "runs", "status", "wfrun_0123456789abcdef"],
        stdout=io.StringIO(), stderr=error,
    ) == 1
    assert "workflow runs:" in error.getvalue()


def test_cleanup_rejects_nonterminal_or_nonisolated_runs(monkeypatch, tmp_path: Path) -> None:
    _store, state = _run(tmp_path)
    monkeypatch.chdir(tmp_path)
    error = io.StringIO()
    assert main(
        ["workflow", "runs", "cleanup", state.run_id],
        stdout=io.StringIO(), stderr=error,
    ) == 1
    assert "requires a terminal run" in error.getvalue()


def test_run_migrate_command_records_new_digest(monkeypatch, tmp_path: Path) -> None:
    store, state = _run(tmp_path)
    path = tmp_path / WORKFLOW_DIRECTORY / "managed.yaml"
    path.write_text("""
version: 1
name: managed
nodes:
  - {id: work, type: agent, prompt: revised}
  - {id: review, type: agent, prompt: review, depends_on: [work]}
""".lstrip(), encoding="utf-8")
    changed = load_workflow(path, tmp_path)
    monkeypatch.chdir(tmp_path)
    output = io.StringIO()

    assert main(
        ["workflow", "runs", "migrate", state.run_id, "managed", "--json"],
        stdout=output,
    ) == 0

    assert json.loads(output.getvalue())["digest"] == changed.source.digest
    assert store.load(state.run_id).payload["migrations"][0]["from_digest"] != changed.source.digest

    trusted = io.StringIO()
    assert main([
        "workflow", "runs", "trust", state.run_id, changed.source.digest, "--json",
    ], stdout=trusted, stderr=io.StringIO()) == 0
    assert json.loads(trusted.getvalue())["trusted"] is True
    assert store.load(state.run_id).payload["trust"]["digest"] == changed.source.digest


def test_approval_cli_displays_revision_and_durably_denies(
    monkeypatch, tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    directory = tmp_path / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True)
    path = directory / "gate.yaml"
    path.write_text("""
version: 1
name: gate
nodes:
  - {id: gate, type: approval, prompt: "Approve?"}
  - {id: after, type: agent, prompt: after, depends_on: [gate]}
""".lstrip(), encoding="utf-8")
    definition = load_workflow(path, tmp_path)
    store = WorkflowRunStore(tmp_path / ".hal" / "runs")
    state = store.create(definition, {}, tmp_path, WorkflowBudgets())
    execute_persisted_workflow(
        definition, {}, WorkflowNodeDispatcher(tmp_path), state,
    )
    monkeypatch.chdir(tmp_path)
    shown = io.StringIO()
    assert main([
        "workflow", "runs", "approval", state.run_id, "gate", "--json",
    ], stdout=shown, stderr=io.StringIO()) == 0
    revision = json.loads(shown.getvalue())["revision_token"]

    denied = io.StringIO()
    assert main([
        "workflow", "runs", "deny", state.run_id, "gate",
        "--revision", revision, "--approver", "local-user",
        "--feedback", "not ready", "--json",
    ], stdout=denied, stderr=io.StringIO()) == 0
    assert json.loads(denied.getvalue())["status"] == "denied"
    persisted = store.load(state.run_id).payload
    assert persisted["nodes"]["after"]["status"] == "skipped"
    assert persisted["approvals"][0]["feedback"] == "not ready"
