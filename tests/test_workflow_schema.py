from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from hal.workflow_schema import (
    ATTEMPT_TERMINAL_STATUSES,
    ATTEMPT_STATUS_TRANSITIONS,
    BUILTIN_NODE_TYPES,
    NODE_TERMINAL_STATUSES,
    NODE_STATUS_TRANSITIONS,
    WORKFLOW_DIRECTORY,
    WORKFLOW_SCHEMA_VERSION,
    WORKFLOW_TERMINAL_STATUSES,
    WORKFLOW_STATUS_TRANSITIONS,
    NodeTypeRegistry,
    NodeTypeSpec,
    WorkflowAttemptStatus,
    WorkflowEffect,
    WorkflowNodeStatus,
    WorkflowOrigin,
    WorkflowRunStatus,
    builtin_workflow_identity,
    discover_workflow_files,
    discover_workflows,
    load_workflow,
    require_status_transition,
    resolve_workflow_names,
    workflow_directory,
)


def _write_workflow(repository: Path, name: str = "idea-to-pr", text: str | None = None) -> Path:
    directory = repository / WORKFLOW_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(text or f"""
version: 1
name: {name}
description: Plan, implement, and verify
inputs:
  request:
    type: string
    required: true
execution:
  workspace: worktree
  max_parallel: 2
  timeout_seconds: 3600
  budgets:
    node_attempts: 10
    provider_calls: 30
nodes:
  - id: plan
    type: agent
    capability: plan
    prompt: Create a plan
    outputs:
      plan:
        type: markdown
        source: final_response
  - id: tests
    type: command
    depends_on: [plan]
    condition: "${{{{ nodes.plan.status == 'succeeded' }}}}"
    command:
      argv: [python, -m, pytest, -q]
    timeout_seconds: 300
    outputs:
      report:
        type: check_result
        source: result
""".lstrip(), encoding="utf-8")
    return path


def test_discovers_only_direct_yaml_files_in_stable_order(tmp_path: Path) -> None:
    directory = workflow_directory(tmp_path)
    directory.mkdir(parents=True)
    (directory / "b.yaml").write_text("", encoding="utf-8")
    (directory / "a.yaml").write_text("", encoding="utf-8")
    (directory / "ignored.yml").write_text("", encoding="utf-8")
    (directory / "ignored.txt").write_text("", encoding="utf-8")
    (directory / "nested").mkdir()
    (directory / "nested" / "hidden.yaml").write_text("", encoding="utf-8")

    assert discover_workflow_files(tmp_path) == (
        directory / "a.yaml", directory / "b.yaml",
    )


def test_missing_workflow_directory_discovers_nothing(tmp_path: Path) -> None:
    assert discover_workflow_files(tmp_path) == ()
    assert dict(discover_workflows(tmp_path)) == {}


def test_loads_strict_inert_definition_with_identity_and_digest(tmp_path: Path) -> None:
    path = _write_workflow(tmp_path)
    raw = path.read_bytes()

    workflow = load_workflow(path, tmp_path)

    assert workflow.version == WORKFLOW_SCHEMA_VERSION
    assert workflow.name == "idea-to-pr"
    assert workflow.source.repository == tmp_path.resolve()
    assert workflow.source.relative_path == ".hal/workflows/idea-to-pr.yaml"
    assert workflow.source.digest == sha256(raw).hexdigest()
    assert workflow.source.identity(workflow.name).origin == WorkflowOrigin.REPOSITORY
    assert workflow.source.identity(workflow.name).repository == tmp_path.resolve()
    assert workflow.inputs["request"].required is True
    assert workflow.execution.workspace == "worktree"
    assert workflow.execution.max_parallel == 2
    assert workflow.execution.budgets["node_attempts"] == 10
    assert [node.id for node in workflow.nodes] == ["plan", "tests"]
    assert workflow.nodes[0].effect == WorkflowEffect.MODEL
    assert workflow.nodes[1].effect == WorkflowEffect.COMMAND_EXECUTION
    assert workflow.nodes[1].effects == frozenset({
        WorkflowEffect.COMMAND_EXECUTION, WorkflowEffect.WORKSPACE_MUTATION,
    })
    assert workflow.nodes[1].config["command"]["argv"] == (
        "python", "-m", "pytest", "-q",
    )
    assert workflow.nodes[1].outputs["report"].type == "check_result"
    assert workflow.nodes[1].config["max_output_chars"] == 100_000
    assert workflow.nodes[1].config["inherit_environment"] == ()
    with pytest.raises(TypeError):
        workflow.execution.budgets["tool_calls"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        workflow.nodes[1].config["command"]["argv"] = ()  # type: ignore[index]


def test_discovery_loads_by_name_and_rejects_duplicate_identity(tmp_path: Path) -> None:
    _write_workflow(tmp_path, "one")
    _write_workflow(tmp_path, "two")
    assert tuple(discover_workflows(tmp_path)) == ("one", "two")

    # A definition cannot acquire a second identity through its file name.
    path = tmp_path / WORKFLOW_DIRECTORY / "two.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("name: two", "name: one"), encoding="utf-8")
    with pytest.raises(ValueError, match="must match file name"):
        discover_workflows(tmp_path)


def test_builtin_identity_and_repository_precedence_are_explicit(tmp_path: Path) -> None:
    first = builtin_workflow_identity("feature", "design\0plan\0build\0review")
    second = builtin_workflow_identity("feature", "design\0plan\0build\0review")
    changed = builtin_workflow_identity("feature", "design\0build\0review")
    assert first == second
    assert first.origin == WorkflowOrigin.BUILTIN
    assert first.repository is None
    assert first.digest != changed.digest

    _write_workflow(tmp_path, "custom")
    repository = discover_workflows(tmp_path)
    assert resolve_workflow_names({"feature"}, repository) == ("custom", "feature")
    with pytest.raises(ValueError, match="cannot shadow built-in.*custom"):
        resolve_workflow_names({"custom"}, repository)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("name: missing-version\nnodes: []\n", "workflow.version"),
        ("version: 2\nname: wrong-version\nnodes: [x]\n", "workflow.version"),
        ("version: 1\nname: bad.name\nnodes: [x]\n", "workflow.name"),
        ("version: 1\nname: unknown\nextra: true\nnodes: [x]\n", "unknown workflow setting"),
        ("version: 1\nname: empty\nnodes: []\n", "non-empty list"),
    ],
)
def test_rejects_invalid_root_schema(tmp_path: Path, text: str, message: str) -> None:
    name = next(line[6:] for line in text.splitlines() if line.startswith("name: "))
    path = _write_workflow(tmp_path, name, text)
    with pytest.raises(ValueError, match=message):
        load_workflow(path, tmp_path)


def test_rejects_yaml_aliases_and_non_utf8(tmp_path: Path) -> None:
    aliases = _write_workflow(tmp_path, "aliases", """
version: 1
name: aliases
nodes:
  - &shared
    id: plan
    type: agent
    prompt: plan
  - *shared
""".lstrip())
    with pytest.raises(ValueError, match="aliases are not allowed"):
        load_workflow(aliases, tmp_path)

    binary = tmp_path / WORKFLOW_DIRECTORY / "binary.yaml"
    binary.write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="must be UTF-8"):
        load_workflow(binary, tmp_path)


def test_workflow_path_must_be_canonical(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text("version: 1\nname: outside\nnodes: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be inside"):
        load_workflow(outside, tmp_path)

    nested = tmp_path / WORKFLOW_DIRECTORY / "nested"
    nested.mkdir(parents=True)
    path = nested / "nested.yaml"
    path.write_text("version: 1\nname: nested\nnodes: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="directly inside"):
        load_workflow(path, tmp_path)


@pytest.mark.parametrize(
    ("node", "message"),
    [
        ({"id": "x", "type": "missing"}, "unknown workflow node type"),
        ({"id": "x", "type": "agent"}, "missing required setting.*prompt"),
        ({"id": "x", "type": "agent", "prompt": "ok", "mystery": True}, "unknown workflow.nodes"),
        ({"id": "x", "type": "command", "command": {}}, "exactly one of argv or shell"),
        ({"id": "x", "type": "command", "command": {"argv": []}}, "non-empty list"),
        ({"id": "x", "type": "command", "command": {"argv": ["ok"], "shell": "ok"}}, "exactly one"),
    ],
)
def test_rejects_invalid_node_schema(tmp_path: Path, node: dict, message: str) -> None:
    import yaml

    text = yaml.safe_dump({"version": 1, "name": "invalid", "nodes": [node]}, sort_keys=False)
    path = _write_workflow(tmp_path, "invalid", text)
    with pytest.raises(ValueError, match=message):
        load_workflow(path, tmp_path)


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        (
            [
                {"id": "same", "type": "agent", "prompt": "one"},
                {"id": "same", "type": "agent", "prompt": "two"},
            ],
            "duplicate workflow node ID",
        ),
        (
            [{"id": "one", "type": "agent", "prompt": "one", "depends_on": ["missing"]}],
            "depends on missing node",
        ),
        (
            [{"id": "one", "type": "agent", "prompt": "one", "depends_on": ["one"]}],
            "cannot depend on itself",
        ),
        (
            [
                {"id": "one", "type": "agent", "prompt": "one", "depends_on": ["two"]},
                {"id": "two", "type": "agent", "prompt": "two", "depends_on": ["one"]},
            ],
            "dependency cycle",
        ),
    ],
)
def test_rejects_invalid_dependency_graph(tmp_path: Path, nodes: list[dict], message: str) -> None:
    import yaml

    text = yaml.safe_dump({"version": 1, "name": "graph", "nodes": nodes}, sort_keys=False)
    path = _write_workflow(tmp_path, "graph", text)
    with pytest.raises(ValueError, match=message):
        load_workflow(path, tmp_path)


def test_node_registry_is_typed_and_rejects_collisions() -> None:
    spec = NodeTypeSpec(
        "custom", frozenset({"query"}), frozenset({"query"}), WorkflowEffect.READ,
        resumable=True, idempotent=True,
    )
    registry = NodeTypeRegistry([spec])
    assert registry.get("custom") is spec
    assert registry.specs == (spec,)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(spec)
    with pytest.raises(ValueError, match="unknown workflow node type"):
        registry.get("agent")
    with pytest.raises(ValueError, match="requires undeclared fields"):
        NodeTypeSpec(
            "broken", frozenset(), frozenset({"missing"}), WorkflowEffect.READ,
        )


def test_builtin_node_registry_exposes_effect_and_recovery_metadata() -> None:
    assert BUILTIN_NODE_TYPES.get("approval").resumable is True
    assert BUILTIN_NODE_TYPES.get("approval").idempotent is True
    assert BUILTIN_NODE_TYPES.get("publish").effect == WorkflowEffect.PUBLICATION
    assert BUILTIN_NODE_TYPES.get("command").idempotent is False


def test_terminal_status_sets_are_explicit_and_typed() -> None:
    assert WorkflowRunStatus.RUNNING not in WORKFLOW_TERMINAL_STATUSES
    assert WorkflowRunStatus.INTERRUPTED in WORKFLOW_TERMINAL_STATUSES
    assert WorkflowNodeStatus.READY not in NODE_TERMINAL_STATUSES
    assert WorkflowNodeStatus.SKIPPED in NODE_TERMINAL_STATUSES
    assert WorkflowAttemptStatus.WAITING not in ATTEMPT_TERMINAL_STATUSES
    assert WorkflowAttemptStatus.DENIED in ATTEMPT_TERMINAL_STATUSES
    assert WorkflowRunStatus.RUNNING in WORKFLOW_STATUS_TRANSITIONS[WorkflowRunStatus.PENDING]
    assert WorkflowNodeStatus.RUNNING in NODE_STATUS_TRANSITIONS[WorkflowNodeStatus.READY]
    assert WorkflowAttemptStatus.SUCCEEDED in ATTEMPT_STATUS_TRANSITIONS[WorkflowAttemptStatus.RUNNING]


def test_status_transition_validation_rejects_terminal_and_cross_layer_moves() -> None:
    require_status_transition(WorkflowRunStatus.PENDING, WorkflowRunStatus.RUNNING)
    require_status_transition(WorkflowNodeStatus.RUNNING, WorkflowNodeStatus.WAITING)
    require_status_transition(WorkflowAttemptStatus.INTERRUPTED, WorkflowAttemptStatus.PENDING)
    with pytest.raises(ValueError, match="illegal.*succeeded -> running"):
        require_status_transition(WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.RUNNING)
    with pytest.raises(ValueError, match="cannot transition between"):
        require_status_transition(WorkflowRunStatus.RUNNING, WorkflowNodeStatus.SUCCEEDED)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("loop: {until: done}", "requires max_attempts or timeout_seconds"),
        ("loop: {max_attempts: 0, until: done}", "max_attempts must be a positive integer"),
        ("loop: {max_attempts: 2, until: ''}", "loop.until must be a non-empty string"),
        ("loop: {max_attempts: 2, until: done, surprise: true}", "unknown.*loop setting"),
    ],
)
def test_agent_loop_schema_is_strict(tmp_path: Path, extra: str, message: str) -> None:
    path = _write_workflow(tmp_path, "loop", f"""
version: 1
name: loop
nodes:
  - id: work
    type: agent
    prompt: work
    {extra}
""".lstrip())
    with pytest.raises(ValueError, match=message):
        load_workflow(path, tmp_path)


def test_input_defaults_match_their_declared_type(tmp_path: Path) -> None:
    path = _write_workflow(tmp_path, "defaults", """
version: 1
name: defaults
inputs:
  count:
    type: integer
    default: nope
nodes:
  - id: plan
    type: agent
    prompt: plan
""".lstrip())
    with pytest.raises(ValueError, match="default does not match declared type"):
        load_workflow(path, tmp_path)


def test_typed_node_inputs_accept_ancestor_outputs(tmp_path: Path) -> None:
    path = _write_workflow(tmp_path, "typed", """
version: 1
name: typed
inputs:
  request: {type: string, required: true}
nodes:
  - id: plan
    type: agent
    prompt: "Plan ${{ inputs.request }}"
    outputs:
      plan: {type: markdown, source: final_response}
  - id: build
    type: agent
    depends_on: [plan]
    prompt: Build it
    inputs:
      plan:
        type: markdown
        value: "${{ nodes.plan.outputs.plan }}"
""".lstrip())

    workflow = load_workflow(path, tmp_path)

    assert workflow.nodes[1].inputs["plan"].type == "markdown"


@pytest.mark.parametrize(
    ("value", "declared_type", "message"),
    [
        ("${{ nodes.future.outputs.answer }}", "string", "dependency ancestors"),
        ("${{ nodes.plan.outputs.missing }}", "string", "unknown workflow output"),
        ("${{ nodes.plan.outputs.answer }}", "integer", "has type 'string'.*integer"),
        ("${{ inputs.missing }}", "string", "unknown workflow input"),
    ],
)
def test_rejects_invalid_or_mismatched_typed_references(
    tmp_path: Path, value: str, declared_type: str, message: str,
) -> None:
    path = _write_workflow(tmp_path, "references", f"""
version: 1
name: references
nodes:
  - id: plan
    type: agent
    prompt: plan
    outputs:
      answer: {{type: string, source: final_response}}
  - id: build
    type: agent
    depends_on: [plan]
    prompt: build
    inputs:
      value:
        type: {declared_type}
        value: "{value}"
  - id: future
    type: agent
    depends_on: [build]
    prompt: future
    outputs:
      answer: {{type: string, source: final_response}}
""".lstrip())
    with pytest.raises(ValueError, match=message):
        load_workflow(path, tmp_path)


def test_conditions_and_loop_until_must_be_boolean(tmp_path: Path) -> None:
    condition = _write_workflow(tmp_path, "condition", """
version: 1
name: condition
inputs:
  request: {type: string}
nodes:
  - id: plan
    type: agent
    prompt: plan
    condition: "${{ inputs.request }}"
""".lstrip())
    with pytest.raises(ValueError, match="condition must be a boolean expression"):
        load_workflow(condition, tmp_path)

    loop = _write_workflow(tmp_path, "loop-type", """
version: 1
name: loop-type
nodes:
  - id: work
    type: agent
    prompt: work
    loop:
      max_attempts: 2
      until: "${{ attempt.index }}"
""".lstrip())
    with pytest.raises(ValueError, match="loop.until must be a boolean expression"):
        load_workflow(loop, tmp_path)


@pytest.mark.parametrize(
    ("command", "extra", "message"),
    [
        ("{shell: echo hi}", "", "shell_kind must be"),
        ("{shell: echo hi, shell_kind: fish}", "", "shell_kind must be"),
        ("{argv: [echo, hi]}", "working_directory: ../outside", "must stay within"),
        ("{argv: [echo, hi]}", "environment: {BAD-NAME: value}", "invalid variable"),
        ("{argv: [echo, hi]}", "inherit_environment: [PATH, PATH]", "duplicates"),
        ("{argv: [echo, hi]}", "max_output_chars: 0", "at least 512"),
    ],
)
def test_command_contract_rejects_ambiguous_or_unbounded_settings(
    tmp_path: Path, command: str, extra: str, message: str,
) -> None:
    path = _write_workflow(tmp_path, "commands", f"""
version: 1
name: commands
nodes:
  - id: run
    type: command
    command: {command}
    {extra}
""".lstrip())
    with pytest.raises(ValueError, match=message):
        load_workflow(path, tmp_path)


def test_output_sources_are_node_type_specific(tmp_path: Path) -> None:
    path = _write_workflow(tmp_path, "source", """
version: 1
name: source
nodes:
  - id: run
    type: command
    command: {argv: [echo, hi]}
    outputs:
      bad: {type: string, source: final_response}
""".lstrip())
    with pytest.raises(ValueError, match="source must be one of"):
        load_workflow(path, tmp_path)
