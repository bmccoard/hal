from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

from hal.harness import RunCounters, RunStatus
from hal.workflow_artifacts import WorkflowArtifactHandle
from hal.workflow_nodes import WorkflowNodeDispatcher
from hal.workflow_schema import WORKFLOW_DIRECTORY, WorkflowRunStatus, load_workflow


def test_agent_command_agent_pipeline_uses_typed_artifact_handoffs(tmp_path: Path) -> None:
    directory = tmp_path / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True)
    path = directory / "pipeline.yaml"
    path.write_text(f"""
version: 1
name: pipeline
nodes:
  - id: plan
    type: agent
    capability: plan
    prompt: plan
    outputs:
      plan: {{type: markdown, source: final_response}}
  - id: tests
    type: command
    depends_on: [plan]
    command:
      argv: [{sys.executable!r}, -c, "print('passed')"]
    outputs:
      report: {{type: check_result, source: result}}
  - id: review
    type: agent
    capability: review
    depends_on: [plan, tests]
    prompt: review
    inputs:
      plan: {{type: markdown, value: "${{{{ nodes.plan.outputs.plan }}}}"}}
      report: {{type: check_result, value: "${{{{ nodes.tests.outputs.report }}}}"}}
""".lstrip(), encoding="utf-8")
    definition = load_workflow(path, tmp_path)

    class FakeAgent:
        last_outcome = None

        def __init__(self):
            self.prompts = []

        def send(self, prompt, *_args, **_kwargs):
            self.prompts.append(prompt)
            self.last_outcome = SimpleNamespace(
                status=RunStatus.SUCCEEDED, reason="completed",
                counters=RunCounters(provider_calls=1),
            )
            return "implementation plan"

    agent = FakeAgent()
    dispatcher = WorkflowNodeDispatcher(tmp_path, agent=agent)

    result = dispatcher.execute(definition, {})

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert isinstance(result.node("plan").outputs["plan"], WorkflowArtifactHandle)
    assert isinstance(result.node("tests").outputs["report"], WorkflowArtifactHandle)
    assert "implementation plan" not in agent.prompts[1]
    assert "artifact" in agent.prompts[1]
    assert dispatcher.ledger.usage.node_attempts == 3
