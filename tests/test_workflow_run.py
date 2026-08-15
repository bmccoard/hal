from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys

from hal.cli import main
from hal.harness import RunCounters, RunStatus
from hal.workflow_schema import WORKFLOW_DIRECTORY, load_workflow


def _write(root: Path, text: str, name: str = "runnable"):
    (root / ".git").mkdir(exist_ok=True)
    directory = root / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return load_workflow(path, root)


def test_command_workflow_requires_exact_digest_then_runs_durably(
    monkeypatch, tmp_path: Path,
) -> None:
    definition = _write(tmp_path, f"""
version: 1
name: runnable
inputs:
  message: {{type: string, required: true}}
nodes:
  - id: command
    type: command
    command:
      argv: [{sys.executable!r}, -c, "print('ok')"]
""".lstrip())
    monkeypatch.chdir(tmp_path)
    error = io.StringIO()

    assert main(
        ["workflow", "run", "runnable", "--input", "message=hello"],
        stdout=io.StringIO(), stderr=error,
    ) == 1
    assert f"--trust-digest {definition.source.digest}" in error.getvalue()
    assert not (tmp_path / ".hal" / "runs").exists()

    output = io.StringIO()
    assert main([
        "workflow", "run", "runnable", "--input", "message=hello",
        "--trust-digest", definition.source.digest, "--json",
    ], stdout=output, stderr=io.StringIO()) == 0

    payload = json.loads(output.getvalue())
    assert payload["status"] == "succeeded"
    record = tmp_path / ".hal" / "runs" / f"{payload['run_id']}.json"
    assert json.loads(record.read_text(encoding="utf-8"))["inputs"] == {"message": "hello"}


def test_read_only_agent_workflow_uses_existing_harness_without_trust(
    monkeypatch, tmp_path: Path,
) -> None:
    _write(tmp_path, """
version: 1
name: runnable
inputs:
  count: {type: integer, required: true}
nodes:
  - id: plan
    type: agent
    capability: plan
    prompt: plan
""".lstrip())
    monkeypatch.chdir(tmp_path)

    class Agent:
        last_outcome = None

        def send(self, *_args, **kwargs):
            self.kwargs = kwargs
            self.last_outcome = SimpleNamespace(
                status=RunStatus.SUCCEEDED, reason="completed",
                counters=RunCounters(provider_calls=1),
            )
            return "done"

    agent = Agent()
    monkeypatch.setattr("hal.cli._load", lambda *_args: SimpleNamespace(capabilities={}))
    monkeypatch.setattr("hal.cli._make_agent", lambda *_args, **_kwargs: (agent, [], {}))
    output = io.StringIO()

    assert main([
        "workflow", "run", "runnable", "--input", "count=3", "--json",
    ], stdout=output, stderr=io.StringIO()) == 0
    assert agent.kwargs["capability"].name == "plan"
    assert json.loads(output.getvalue())["status"] == "succeeded"


def test_run_validates_inputs_before_creating_state(monkeypatch, tmp_path: Path) -> None:
    _write(tmp_path, """
version: 1
name: runnable
inputs:
  enabled: {type: boolean, required: true}
nodes:
  - {id: plan, type: agent, capability: plan, prompt: plan}
""".lstrip())
    monkeypatch.chdir(tmp_path)
    error = io.StringIO()

    assert main([
        "workflow", "run", "runnable", "--input", "enabled=maybe",
    ], stdout=io.StringIO(), stderr=error) == 1
    assert "not valid boolean" in error.getvalue()
    assert not (tmp_path / ".hal" / "runs").exists()


def test_run_attaches_isolated_worktree_before_execution(monkeypatch, tmp_path: Path) -> None:
    definition = _write(tmp_path, f"""
version: 1
name: runnable
execution: {{workspace: worktree}}
nodes:
  - id: command
    type: command
    command:
      argv: [{sys.executable!r}, -c, "print('isolated')"]
""".lstrip())
    monkeypatch.chdir(tmp_path)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    preflight = SimpleNamespace(
        branch="hal/runnable/test", head="a" * 40, source_branch="main",
        dirty_paths=("user-change.txt",),
    )
    monkeypatch.setattr("hal.cli.preflight_worktree", lambda *_args: preflight)
    monkeypatch.setattr("hal.cli.create_isolated_worktree", lambda *_args: isolated)
    monkeypatch.setattr("hal.cli.inspect_worktree", lambda *_args: SimpleNamespace(
        head="a" * 40, branch="hal/runnable/test", dirty_digest="0" * 64,
        dirty_paths=(), registered=True, path=isolated,
    ))
    output = io.StringIO()

    assert main([
        "workflow", "run", "runnable", "--trust-digest", definition.source.digest,
        "--json",
    ], stdout=output, stderr=io.StringIO()) == 0
    payload = json.loads(output.getvalue())
    record = json.loads(
        (tmp_path / ".hal" / "runs" / f"{payload['run_id']}.json").read_text(encoding="utf-8")
    )
    assert record["workspace"]["path"] == str(isolated)
    assert record["workspace"]["branch"] == "hal/runnable/test"
    assert record["workspace"]["source_dirty_paths"] == ["user-change.txt"]
