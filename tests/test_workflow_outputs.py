from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hal.harness import RunCounters, RunStatus
from hal.workflow_artifacts import WorkflowArtifactHandle, WorkflowArtifactStore
from hal.workflow_nodes import WorkflowNodeDispatcher, execute_agent_node
from hal.workflow_outputs import validate_and_store_node_outputs
from hal.workflow_runtime import WorkflowNodeInvocation, WorkflowNodeReceipt
from hal.workflow_schema import WORKFLOW_DIRECTORY, WorkflowNodeStatus, load_workflow
from hal.workflow_budgets import WorkflowBudgetLedger, WorkflowBudgets
from hal.cancellation import CancellationToken


def _node(tmp_path: Path, body: str, name: str = "outputs"):
    directory = tmp_path / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(f"version: 1\nname: {name}\nnodes:\n{body}", encoding="utf-8")
    return load_workflow(path, tmp_path).nodes[-1]


def test_success_requires_exact_declared_output_mapping(tmp_path: Path) -> None:
    node = _node(tmp_path, """
  - id: plan
    type: agent
    prompt: plan
    outputs:
      answer: {type: string, source: final_response}
""")
    invocation = WorkflowNodeInvocation(node, {}, {"inputs": {}, "nodes": {}})
    store = WorkflowArtifactStore(tmp_path / "artifacts")

    missing = validate_and_store_node_outputs(
        invocation, WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED), store,
    )
    extra = validate_and_store_node_outputs(
        invocation,
        WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED, {"answer": "ok", "secret": "bad"}),
        store,
    )
    wrong = validate_and_store_node_outputs(
        invocation,
        WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED, {"answer": 7}),
        store,
    )

    assert missing.status is WorkflowNodeStatus.FAILED and "missing" in missing.reason
    assert extra.status is WorkflowNodeStatus.FAILED and "undeclared" in extra.reason
    assert wrong.status is WorkflowNodeStatus.FAILED and "declared type" in wrong.reason


def test_artifact_oriented_outputs_are_replaced_with_verified_handles(tmp_path: Path) -> None:
    node = _node(tmp_path, """
  - id: tests
    type: command
    command: {argv: [test]}
    outputs:
      report: {type: check_result, source: result}
""")
    invocation = WorkflowNodeInvocation(node, {}, {"inputs": {}, "nodes": {}})
    store = WorkflowArtifactStore(tmp_path / "artifacts")

    receipt = validate_and_store_node_outputs(
        invocation,
        WorkflowNodeReceipt(
            WorkflowNodeStatus.SUCCEEDED,
            {"report": {"exit_code": 0, "stdout": "passed"}},
        ),
        store,
    )

    handle = receipt.outputs["report"]
    assert isinstance(handle, WorkflowArtifactHandle)
    assert handle.artifact.type == "check_result"
    assert b'"exit_code":0' in store.read(handle.artifact)


def test_path_outputs_must_be_workspace_relative(tmp_path: Path) -> None:
    node = _node(tmp_path, """
  - id: agent
    type: agent
    prompt: path
    outputs:
      path: {type: path, source: final_response}
""")
    receipt = validate_and_store_node_outputs(
        WorkflowNodeInvocation(node, {}, {"inputs": {}, "nodes": {}}),
        WorkflowNodeReceipt(WorkflowNodeStatus.SUCCEEDED, {"path": "../outside"}),
        WorkflowArtifactStore(tmp_path / "artifacts"),
    )
    assert receipt.status is WorkflowNodeStatus.FAILED


def test_agent_projection_contains_only_bounded_artifact_metadata(tmp_path: Path) -> None:
    producer = _node(tmp_path, """
  - id: producer
    type: agent
    prompt: produce
    outputs:
      document: {type: markdown, source: final_response}
  - id: consumer
    type: agent
    prompt: consume
    depends_on: [producer]
    inputs:
      document: {type: markdown, value: "${{ nodes.producer.outputs.document }}"}
""", "projection")
    store = WorkflowArtifactStore(tmp_path / "artifacts")
    secret_body = "PRIVATE-BODY-" * 10_000
    handle = WorkflowArtifactHandle(store.put(
        secret_body, type="markdown", producer="producer", media_type="text/markdown",
    ))

    class FakeAgent:
        last_outcome = None

        def send(self, prompt, *_args, **_kwargs):
            self.prompt = prompt
            self.last_outcome = SimpleNamespace(
                status=RunStatus.SUCCEEDED, reason="completed", counters=RunCounters(),
            )
            return "done"

    agent = FakeAgent()
    receipt, _usage = execute_agent_node(
        WorkflowNodeInvocation(
            producer, {"document": handle}, {"inputs": {}, "nodes": {}},
        ),
        agent, WorkflowBudgetLedger(WorkflowBudgets()), CancellationToken(),
    )

    assert receipt.status is WorkflowNodeStatus.SUCCEEDED
    assert "PRIVATE-BODY" not in agent.prompt
    assert handle.artifact.digest in agent.prompt
    assert str(handle.artifact.size) in agent.prompt
