from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from hal.harness import RunCounters, RunStatus
from hal.models import Usage
from hal.workflow_budgets import WorkflowBudgetLedger, WorkflowBudgets
from hal.workflow_nodes import (
    WorkflowNodeDispatcher, execute_agent_node, execute_command_node, execute_git_node,
)
from hal.workflow_runtime import WorkflowNodeInvocation
from hal.workflow_schema import WORKFLOW_DIRECTORY, WorkflowNodeStatus, load_workflow
from hal.cancellation import CancellationToken
from hal.git import DulwichGitBackend, GitError
from dulwich import porcelain


def _node(tmp_path: Path, body: str, name: str = "nodes"):
    directory = tmp_path / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(
        f"version: 1\nname: {name}\ninputs:\n"
        f"  request: {{type: string}}\nnodes:\n{body}",
        encoding="utf-8",
    )
    return load_workflow(path, tmp_path).nodes[0]


def test_command_node_executes_argv_with_bounded_typed_outputs(tmp_path: Path) -> None:
    node = _node(tmp_path, f"""
  - id: command
    type: command
    command:
      argv: [{sys.executable!r}, -c, "import sys; print('ok'); print('warn', file=sys.stderr)"]
    max_output_chars: 1024
    outputs:
      report: {{type: check_result, source: result}}
      code: {{type: integer, source: exit_code}}
""")

    receipt = execute_command_node(
        WorkflowNodeInvocation(node, {}, {"inputs": {}, "nodes": {}}),
        tmp_path,
    )

    assert receipt.status is WorkflowNodeStatus.SUCCEEDED
    assert receipt.outputs["code"] == 0
    assert receipt.outputs["report"]["stdout"].strip() == "ok"
    assert receipt.outputs["report"]["stderr"].strip() == "warn"


def test_command_node_uses_allowlisted_environment_only(monkeypatch, tmp_path: Path) -> None:
    node = _node(tmp_path, """
  - id: command
    type: command
    command: {argv: [tool, argument]}
    inherit_environment: [PATH]
    environment:
      REQUEST: "${{ inputs.request }}"
""")
    captured = {}
    monkeypatch.setenv("PATH", "allowed-path")
    monkeypatch.setenv("SECRET", "must-not-leak")

    def run(*args, **kwargs):
        from hal.process import ProcessResult
        captured.update(kwargs)
        return ProcessResult(args[0], 0, "", "", False, False)

    monkeypatch.setattr("hal.workflow_nodes.run_bounded_process", run)
    invocation = WorkflowNodeInvocation(
        node, {}, {"inputs": {"request": "hello"}, "nodes": {}},
    )

    receipt = execute_command_node(invocation, tmp_path)

    assert receipt.status is WorkflowNodeStatus.SUCCEEDED
    assert captured["environment"] == {"PATH": "allowed-path", "REQUEST": "hello"}


def test_nonzero_command_is_failed_but_preserves_declared_receipt(tmp_path: Path) -> None:
    node = _node(tmp_path, f"""
  - id: command
    type: command
    command:
      argv: [{sys.executable!r}, -c, "import sys; print('bad'); sys.exit(7)"]
    outputs:
      stdout: {{type: string, source: stdout}}
      code: {{type: integer, source: exit_code}}
""")

    receipt = execute_command_node(
        WorkflowNodeInvocation(node, {}, {"inputs": {}, "nodes": {}}), tmp_path,
    )

    assert receipt.status is WorkflowNodeStatus.FAILED
    assert receipt.outputs["code"] == 7
    assert receipt.outputs["stdout"].strip() == "bad"


def test_git_status_returns_structured_repository_identity(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    porcelain.init(root)
    node = _node(root, """
  - id: status
    type: git
    operation: status
    outputs:
      receipt: {type: check_result, source: result}
""", "git-status")

    receipt = execute_git_node(
        WorkflowNodeInvocation(node, {}, {"inputs": {}, "nodes": {}}),
        root, backend_preference="dulwich",
    )

    assert receipt.status is WorkflowNodeStatus.SUCCEEDED
    assert receipt.outputs["receipt"]["operation"] == "status"
    assert receipt.outputs["receipt"]["repository"]["backend"] == "dulwich"
    assert receipt.outputs["receipt"]["repository"]["head"] is None


def test_git_stage_enforces_exact_declared_path_set(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    porcelain.init(root)
    (root / "one.txt").write_text("one\n", encoding="utf-8")
    (root / "two.txt").write_text("two\n", encoding="utf-8")
    porcelain.add(root, paths=["two.txt"])
    node = _node(root, """
  - id: stage
    type: git
    operation: stage
    artifacts: [one.txt]
    outputs:
      receipt: {type: check_result, source: result}
""", "git-stage")

    receipt = execute_git_node(
        WorkflowNodeInvocation(node, {}, {"inputs": {}, "nodes": {}}),
        root, backend_preference="dulwich",
    )

    assert receipt.outputs["receipt"]["status"]["staged"] == ["one.txt"]
    assert "two.txt" in receipt.outputs["receipt"]["status"]["untracked"]


def test_git_commit_is_bounded_to_declared_paths(monkeypatch, tmp_path: Path) -> None:
    for name, value in {
        "GIT_AUTHOR_NAME": "HAL Test", "GIT_AUTHOR_EMAIL": "hal@example.test",
        "GIT_COMMITTER_NAME": "HAL Test", "GIT_COMMITTER_EMAIL": "hal@example.test",
    }.items():
        monkeypatch.setenv(name, value)
    root = tmp_path / "repo"
    porcelain.init(root)
    (root / "kept.txt").write_text("kept\n", encoding="utf-8")
    (root / "commit.txt").write_text("commit\n", encoding="utf-8")
    node = _node(root, """
  - id: commit
    type: git
    operation: commit
    message: Commit exact set
    artifacts: [commit.txt]
    outputs:
      receipt: {type: check_result, source: result}
""", "git-commit")

    receipt = execute_git_node(
        WorkflowNodeInvocation(node, {}, {"inputs": {}, "nodes": {}}),
        root, backend_preference="dulwich",
    )

    result = receipt.outputs["receipt"]
    assert len(result["commit"]) == 40
    assert result["repository"]["head"] == result["commit"]
    assert "kept.txt" in result["status"]["untracked"]
    assert "commit.txt" not in result["status"]["untracked"]

    # Re-delivery cannot create a second commit from the same exact content.
    with pytest.raises(GitError, match="no changes are staged"):
        execute_git_node(
            WorkflowNodeInvocation(node, {}, {"inputs": {}, "nodes": {}}),
            root, backend_preference="dulwich",
        )
    assert len(DulwichGitBackend(root).log()) == 1


def test_agent_node_uses_capability_remaining_budgets_and_fresh_context(tmp_path: Path) -> None:
    node = _node(tmp_path, """
  - id: agent
    type: agent
    capability: plan
    fresh_context: true
    prompt: "Plan ${{ inputs.request }}"
    budgets: {provider_calls: 4, tool_calls: 8}
    inputs:
      context: {type: markdown, value: notes}
    outputs:
      answer: {type: markdown, source: final_response}
""")

    class FakeAgent:
        last_outcome = None

        def send(self, prompt, display, cancellation, **kwargs):
            self.call = (prompt, display, cancellation, kwargs)
            self.last_outcome = SimpleNamespace(
                status=RunStatus.SUCCEEDED,
                reason="completed",
                counters=RunCounters(
                    provider_calls=2, tool_calls=3,
                    usage=Usage(input_tokens=10, output_tokens=5),
                ),
            )
            return "the plan"

    agent = FakeAgent()
    ledger = WorkflowBudgetLedger(
        WorkflowBudgets(provider_calls=5, tool_calls=6),
    )
    token = CancellationToken()
    receipt, usage = execute_agent_node(
        WorkflowNodeInvocation(
            node, {"context": "notes"},
            {"inputs": {"request": "feature"}, "nodes": {}},
        ),
        agent, ledger, token,
    )

    prompt, display, passed_token, kwargs = agent.call
    assert prompt.startswith("Plan feature")
    assert "context (markdown): notes" in prompt
    assert display == "[workflow node: agent]"
    assert passed_token is token
    assert kwargs["include_history"] is False
    assert kwargs["capability"].name == "plan"
    assert kwargs["budgets"].provider_calls == 4
    assert kwargs["budgets"].tool_calls == 6
    assert receipt.outputs == {"answer": "the plan"}
    assert usage.provider_calls == 2 and usage.tool_calls == 3


def test_dispatcher_aggregates_agent_usage_and_node_attempts(tmp_path: Path) -> None:
    node = _node(tmp_path, """
  - {id: agent, type: agent, prompt: work}
""")

    class FakeAgent:
        last_outcome = None

        def send(self, *_args, **_kwargs):
            self.last_outcome = SimpleNamespace(
                status=RunStatus.SUCCEEDED, reason="completed",
                counters=RunCounters(provider_calls=1, usage=Usage(output_tokens=2)),
            )
            return "done"

    dispatcher = WorkflowNodeDispatcher(
        tmp_path, agent=FakeAgent(),
        budgets=WorkflowBudgets(node_attempts=2, provider_calls=3, output_tokens=5),
    )

    receipt = dispatcher(WorkflowNodeInvocation(node, {}, {"inputs": {}, "nodes": {}}))

    assert receipt.status is WorkflowNodeStatus.SUCCEEDED
    assert dispatcher.ledger.usage.node_attempts == 1
    assert dispatcher.ledger.usage.provider_calls == 1
    assert dispatcher.ledger.usage.output_tokens == 2
