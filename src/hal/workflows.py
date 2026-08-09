"""Built-in, bounded multi-phase workflows."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .agent import Agent
from .cancellation import CancellationToken
from .context import Phase


@dataclass(frozen=True, slots=True)
class Workflow:
    name: str
    description: str
    phases: tuple[str, ...]


WORKFLOWS = {
    "feature": Workflow(
        "feature",
        "Design, plan, build, and review one requested repository change",
        ("design", "plan", "build", "review"),
    ),
}


def parse_workflow_command(text: str) -> tuple[Workflow, str] | None:
    """Parse ``/workflow NAME REQUEST`` or return ``None`` for other input."""
    if text != "/workflow" and not text.startswith("/workflow "):
        return None
    arguments = text[len("/workflow"):].strip()
    name, _, request = arguments.partition(" ")
    if not name or not request.strip():
        raise ValueError("usage: /workflow <name> <request>")
    workflow = WORKFLOWS.get(name.lower())
    if workflow is None:
        available = ", ".join(sorted(WORKFLOWS))
        raise ValueError(f"unknown workflow {name!r} (available: {available})")
    return workflow, request.strip()


def run_workflow(
    agent: Agent,
    workflow: Workflow,
    request: str,
    phases: dict[str, Phase],
    cancellation: CancellationToken,
    on_step: Callable[[int, int, str], None] | None = None,
) -> list[str]:
    """Run each named phase in order in the current agent transcript."""
    missing = [name for name in workflow.phases if name not in phases]
    if missing:
        raise ValueError(
            f"workflow {workflow.name!r} requires missing phase(s): {', '.join(missing)}"
        )
    results: list[str] = []
    total = len(workflow.phases)
    for index, name in enumerate(workflow.phases, 1):
        cancellation.raise_if_cancelled()
        if on_step:
            on_step(index, total, name)
        phase = phases[name]
        prompt = (
            f"[workflow: {workflow.name}; step {index}/{total}; named phase: {name}]\n"
            f"{phase.prompt}\n\nOriginal workflow request:\n{request}"
        )
        results.append(agent.send(
            prompt,
            f"/workflow {workflow.name} [{name}] {request}",
            cancellation,
        ))
    return results
