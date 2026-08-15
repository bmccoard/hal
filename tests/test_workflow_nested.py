from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hal.harness import RunCounters, RunStatus
from hal.workflow_budgets import WorkflowBudgets
from hal.workflow_nodes import WorkflowNodeDispatcher
from hal.workflow_schema import WORKFLOW_DIRECTORY, WorkflowRunStatus, load_workflow


def _write(root: Path, name: str, body: str):
    directory = root / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    return load_workflow(path, root)


class FakeAgent:
    last_outcome = None

    def send(self, *_args, **_kwargs):
        self.last_outcome = SimpleNamespace(
            status=RunStatus.SUCCEEDED, reason="completed",
            counters=RunCounters(provider_calls=1),
        )
        return "done"


def test_nested_workflow_uses_pinned_definition_and_shared_budget(tmp_path: Path) -> None:
    child = _write(tmp_path, "child", """
version: 1
name: child
inputs:
  request: {type: string, required: true}
nodes:
  - {id: work, type: agent, prompt: "${{ inputs.request }}"}
""".lstrip())
    parent = _write(tmp_path, "parent", f"""
version: 1
name: parent
inputs:
  request: {{type: string, required: true}}
nodes:
  - id: child
    type: workflow
    workflow: child
    digest: {child.source.digest}
    inputs:
      request: {{type: string, value: "${{{{ inputs.request }}}}"}}
    outputs:
      result: {{type: json, source: result}}
""".lstrip())
    dispatcher = WorkflowNodeDispatcher(
        tmp_path, agent=FakeAgent(), workflows={"child": child, "parent": parent},
        budgets=WorkflowBudgets(node_attempts=2, provider_calls=1),
    )

    result = dispatcher.execute(parent, {"request": "do it"})

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert result.node("child").outputs["result"]["status"] == "succeeded"
    assert dispatcher.ledger.usage.node_attempts == 2
    assert dispatcher.ledger.usage.provider_calls == 1


def test_nested_workflow_rejects_changed_digest_before_execution(tmp_path: Path) -> None:
    child = _write(tmp_path, "child", """
version: 1
name: child
nodes:
  - {id: work, type: agent, prompt: work}
""".lstrip())
    parent = _write(tmp_path, "parent", f"""
version: 1
name: parent
nodes:
  - id: child
    type: workflow
    workflow: child
    digest: {child.source.digest}
""".lstrip())
    changed = _write(tmp_path, "child", """
version: 1
name: child
description: changed after approval
nodes:
  - {id: work, type: agent, prompt: changed}
""".lstrip())
    agent = FakeAgent()
    dispatcher = WorkflowNodeDispatcher(
        tmp_path, agent=agent, workflows={"child": changed},
    )

    result = dispatcher.execute(parent, {})

    assert result.status is WorkflowRunStatus.FAILED
    assert "digest changed" in result.node("child").reason
    assert agent.last_outcome is None


def test_nested_workflow_cycles_fail_closed(tmp_path: Path) -> None:
    child = _write(tmp_path, "child", """
version: 1
name: child
nodes:
  - id: parent
    type: workflow
    workflow: parent
    digest: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
""".lstrip())
    parent = _write(tmp_path, "parent", f"""
version: 1
name: parent
nodes:
  - id: child
    type: workflow
    workflow: child
    digest: {child.source.digest}
""".lstrip())
    dispatcher = WorkflowNodeDispatcher(
        tmp_path, workflows={"child": child, "parent": parent},
    )

    result = dispatcher.execute(parent, {})

    assert result.status is WorkflowRunStatus.FAILED
    child_result = result.node("child")
    assert child_result.status.value == "failed"
