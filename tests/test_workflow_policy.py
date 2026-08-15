from __future__ import annotations

from pathlib import Path

import pytest

from hal.workflow_policy import (
    WorkflowTrustGrant,
    require_workflow_trust,
    workflow_required_effects,
    workflow_requires_trust,
)
from hal.workflow_schema import WorkflowEffect, load_workflow


def _definition(tmp_path: Path, name: str, node: str, *, worktree: bool = False):
    directory = tmp_path / ".hal" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    execution = "execution:\n  workspace: worktree\n" if worktree else ""
    path.write_text(
        f"version: 1\nname: {name}\n{execution}nodes:\n{node}", encoding="utf-8",
    )
    return load_workflow(path, tmp_path)


def test_read_model_workflow_does_not_require_repository_trust(tmp_path: Path) -> None:
    definition = _definition(tmp_path, "inspect", "  - id: inspect\n    type: agent\n    prompt: inspect\n")
    assert workflow_required_effects(definition) == frozenset({
        WorkflowEffect.MODEL, WorkflowEffect.READ,
    })
    assert workflow_requires_trust(definition) is False
    require_workflow_trust(definition, None)


def test_command_workflow_requires_exact_digest_pinned_trust(tmp_path: Path) -> None:
    definition = _definition(
        tmp_path, "build",
        "  - id: build\n    type: command\n    command:\n      argv: [python, build.py]\n",
    )
    assert workflow_requires_trust(definition) is True
    with pytest.raises(PermissionError, match="workspace_mutation"):
        require_workflow_trust(definition, None)

    grant = WorkflowTrustGrant.for_definition(definition)
    assert grant.authorizes(definition) is True
    require_workflow_trust(definition, grant)

    changed = _definition(
        tmp_path, "build",
        "  - id: build\n    type: command\n    command:\n      argv: [python, changed.py]\n",
    )
    assert grant.authorizes(changed) is False
    with pytest.raises(PermissionError, match="stale"):
        require_workflow_trust(changed, grant)


def test_publish_declares_credentials_network_and_publication(tmp_path: Path) -> None:
    definition = _definition(
        tmp_path, "publish",
        """  - id: approve
    type: approval
    prompt: approve
    inputs:
      remote: {type: string, value: origin}
      title: {type: string, value: Change}
      body: {type: markdown, value: Body}
      base: {type: string, value: main}
      head: {type: string, value: feature}
      commits: {type: json, value: [1111111111111111111111111111111111111111]}
      checks: {type: check_result, value: {passed: true}}
      review: {type: markdown, value: Reviewed}
  - id: publish
    type: publish
    depends_on: [approve]
    provider: github
    operation: pull_request
    remote: origin
    approval: approve
    inputs:
      title: {type: string, value: Change}
      body: {type: markdown, value: Body}
      base: {type: string, value: main}
      head: {type: string, value: feature}
      commits: {type: json, value: [1111111111111111111111111111111111111111]}
      checks: {type: check_result, value: {passed: true}}
      review: {type: markdown, value: Reviewed}
""",
        worktree=True,
    )
    assert workflow_required_effects(definition) == frozenset({
        WorkflowEffect.APPROVAL,
        WorkflowEffect.PUBLICATION,
        WorkflowEffect.CREDENTIAL_USE,
        WorkflowEffect.NETWORK_ACCESS,
    })
    with pytest.raises(PermissionError) as error:
        require_workflow_trust(definition, None)
    assert "credential_use" in str(error.value)
    assert "network_access" in str(error.value)
    assert "publication" in str(error.value)


def test_mutating_or_custom_agent_capability_requires_trust(tmp_path: Path) -> None:
    definition = _definition(
        tmp_path, "agent-change",
        "  - id: build\n    type: agent\n    capability: change\n    prompt: build\n",
    )
    assert WorkflowEffect.WORKSPACE_MUTATION in workflow_required_effects(definition)
    with pytest.raises(PermissionError, match="workspace_mutation"):
        require_workflow_trust(definition, None)


def test_trust_is_pinned_to_repository_and_effect_set(tmp_path: Path) -> None:
    definition = _definition(
        tmp_path / "one", "build",
        "  - id: build\n    type: command\n    command:\n      argv: [python, build.py]\n",
    )
    same_text_other_repository = _definition(
        tmp_path / "two", "build",
        "  - id: build\n    type: command\n    command:\n      argv: [python, build.py]\n",
    )
    grant = WorkflowTrustGrant.for_definition(definition)
    assert grant.authorizes(same_text_other_repository) is False

    narrowed = WorkflowTrustGrant(
        grant.workflow_name, grant.repository, grant.definition_digest, frozenset(),
    )
    assert narrowed.authorizes(definition) is False
