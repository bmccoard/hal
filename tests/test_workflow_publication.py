from pathlib import Path

import pytest

from hal.workflow_inspect import inspect_repository_workflow
from hal.workflow_publication import publication_isolation, require_publication_isolation
from hal.workflow_schema import WORKFLOW_DIRECTORY, load_workflow


def _workflow(tmp_path: Path, nodes: str):
    path = tmp_path / WORKFLOW_DIRECTORY / "publication.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "version: 1\nname: publication\nexecution:\n  workspace: worktree\nnodes:\n"
        + nodes,
        encoding="utf-8",
    )
    return load_workflow(path, tmp_path)


def test_non_publication_workflow_does_not_require_isolation(tmp_path: Path) -> None:
    definition = _workflow(tmp_path, """
  - id: inspect
    type: command
    command: {argv: [python, --version]}
""")

    assert publication_isolation(definition) == {
        "required": False, "enforceable": True, "ordinary_nodes": ["inspect"],
    }


def test_publication_workflow_reports_host_boundary_and_fails_closed(tmp_path: Path) -> None:
    definition = _workflow(tmp_path, """
  - id: prepare
    type: command
    command: {argv: [python, --version]}
  - id: approve
    type: approval
    depends_on: [prepare]
    prompt: approve
    inputs:
      remote: {type: string, value: origin}
      branch: {type: string, value: main}
      commit: {type: string, value: "1111111111111111111111111111111111111111"}
  - id: publish
    type: publish
    depends_on: [approve]
    provider: git
    operation: push
    remote: origin
    branch: main
    commit: "1111111111111111111111111111111111111111"
    approval: approve
""")

    report = inspect_repository_workflow(definition)["publication_isolation"]
    assert report["required"] is True
    assert report["enforceable"] is False
    assert report["ordinary_nodes"] == ["prepare"]
    with pytest.raises(PermissionError, match="no enforceable network"):
        require_publication_isolation(definition)
