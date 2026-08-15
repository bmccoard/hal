from __future__ import annotations

import io
import json
from pathlib import Path

from hal.cli import main
from hal.workflow_schema import WORKFLOW_DIRECTORY


def _write_repository_workflow(root: Path) -> None:
    directory = root / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True)
    (directory / "checks.yaml").write_text("""
version: 1
name: checks
description: Run repository checks
execution:
  workspace: worktree
nodes:
  - id: tests
    type: command
    command:
      argv: [python, -m, pytest, -q]
    environment:
      API_TOKEN: literal-secret-that-must-not-appear
    inherit_environment: [PATH]
    max_output_chars: 2048
""".lstrip(), encoding="utf-8")


def test_workflow_list_includes_builtin_and_repository_definitions(
    monkeypatch, tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    _write_repository_workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    output = io.StringIO()

    assert main(["workflow", "list", "--json"], stdout=output, stderr=io.StringIO()) == 0

    payload = json.loads(output.getvalue())
    assert [item["name"] for item in payload["workflows"]] == ["checks", "feature"]
    checks = payload["workflows"][0]
    assert checks["trust_required"] is True
    assert "command_execution" in checks["effects"]


def test_workflow_inspect_reports_contract_without_environment_values(
    monkeypatch, tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    _write_repository_workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    output = io.StringIO()

    assert main(
        ["workflow", "inspect", "checks", "--json"],
        stdout=output, stderr=io.StringIO(),
    ) == 0

    rendered = output.getvalue()
    payload = json.loads(rendered)
    assert payload["origin"] == "repository"
    assert payload["execution"]["workspace"] == "worktree"
    assert payload["nodes"][0]["config"]["command"]["argv"] == [
        "python", "-m", "pytest", "-q",
    ]
    assert payload["nodes"][0]["config"]["environment"] == {"names": ["API_TOKEN"]}
    assert "literal-secret-that-must-not-appear" not in rendered


def test_workflow_inspection_is_strict_and_never_executes_invalid_yaml(
    monkeypatch, tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    directory = tmp_path / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True)
    (directory / "broken.yaml").write_text(
        "version: 1\nname: broken\nnodes:\n  - id: run\n    type: command\n"
        "    command: {shell: echo should-not-run}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    error = io.StringIO()

    assert main(["workflow", "list"], stdout=io.StringIO(), stderr=error) == 1
    assert "shell_kind" in error.getvalue()


def test_workflow_inspection_rejects_unknown_names_and_bad_arguments(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    error = io.StringIO()
    assert main(
        ["workflow", "inspect", "missing"], stdout=io.StringIO(), stderr=error,
    ) == 1
    assert "unknown workflow" in error.getvalue()
    assert main(
        ["workflow", "inspect"], stdout=io.StringIO(), stderr=io.StringIO(),
    ) == 2
