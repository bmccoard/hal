from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys

from hal.cli import main
from hal.workflow_policy import workflow_required_effects
from hal.workflow_schema import (
    WORKFLOW_DIRECTORY, WorkflowEffect, load_workflow,
)
from hal.workflow_templates import discover_workflow_templates


EXPECTED_TEMPLATES = ("project-setup", "reviewed-change", "simple-change")
FORBIDDEN_EFFECTS = {
    WorkflowEffect.GIT_MUTATION,
    WorkflowEffect.CREDENTIAL_USE,
    WorkflowEffect.NETWORK_ACCESS,
    WorkflowEffect.PUBLICATION,
}


def test_packaged_workflow_templates_are_valid_bounded_and_fail_closed() -> None:
    templates = discover_workflow_templates()

    assert tuple(templates) == EXPECTED_TEMPLATES
    for template in templates.values():
        definition = template.definition
        assert definition.name == template.path.stem
        assert definition.execution.workspace == "current"
        assert definition.execution.max_parallel == 1
        assert definition.execution.timeout_seconds is not None
        assert definition.execution.budgets.provider_calls is not None
        assert definition.execution.budgets.tool_calls is not None
        assert definition.execution.budgets.elapsed_seconds is not None
        assert not (workflow_required_effects(definition) & FORBIDDEN_EFFECTS)
        assert not (
            {"git", "publish", "approval"}
            & {node.type for node in definition.nodes}
        )

        sentinel_commands = [
            node
            for node in definition.nodes
            if node.type == "command"
            and "HAL_TEMPLATE_NOT_CONFIGURED"
            in " ".join(node.config["command"]["argv"])
        ]
        assert len(sentinel_commands) == 2
        assert definition.nodes[-1].id == "diff_check"

        gate = definition.nodes[0]
        assert gate.id == "configuration_gate"
        assert gate.type == "command"
        argv = list(gate.config["command"]["argv"])
        assert "HAL_TEMPLATE_NOT_CONFIGURED" in " ".join(argv)
        result = subprocess.run(
            [sys.executable, *argv[1:]], capture_output=True, text=True, timeout=5,
        )
        assert result.returncode != 0
        assert "HAL_TEMPLATE_NOT_CONFIGURED" in result.stdout + result.stderr


def test_workflow_templates_command_lists_only_packaged_templates() -> None:
    output = io.StringIO()

    assert main(
        ["workflow", "templates", "--json"],
        stdout=output,
        stderr=io.StringIO(),
    ) == 0

    payload = json.loads(output.getvalue())
    assert tuple(item["name"] for item in payload["templates"]) == EXPECTED_TEMPLATES
    assert all(item["configured"] is False for item in payload["templates"])
    assert all(len(item["digest"]) == 64 for item in payload["templates"])


def test_workflow_init_copies_valid_template_without_overwriting(
    monkeypatch, tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    output = io.StringIO()

    assert main(
        ["workflow", "init", "simple-change", "--json"],
        stdout=output,
        stderr=io.StringIO(),
    ) == 0

    payload = json.loads(output.getvalue())
    path = tmp_path / WORKFLOW_DIRECTORY / "simple-change.yaml"
    original = path.read_bytes()
    definition = load_workflow(path, tmp_path)
    assert payload["path"] == ".hal/workflows/simple-change.yaml"
    assert payload["template_digest"] == definition.source.digest
    assert payload["configured"] is False
    assert "HAL_TEMPLATE_NOT_CONFIGURED" in path.read_text(encoding="utf-8")

    error = io.StringIO()
    assert main(
        ["workflow", "init", "simple-change"],
        stdout=io.StringIO(),
        stderr=error,
    ) == 1
    assert "was not overwritten" in error.getvalue()
    assert path.read_bytes() == original


def test_workflow_init_rejects_unknown_template(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    error = io.StringIO()

    assert main(
        ["workflow", "init", "missing"],
        stdout=io.StringIO(),
        stderr=error,
    ) == 1

    assert "unknown workflow template 'missing'" in error.getvalue()
    assert not (tmp_path / WORKFLOW_DIRECTORY).exists()
