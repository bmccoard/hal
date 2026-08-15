"""Strict, side-effect-free definitions for repository workflow orchestration."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Any, Mapping

import yaml
from yaml.composer import ComposerError
from yaml.events import AliasEvent

from .workflow_expressions import (
    BinaryExpression, Expression, LiteralExpression, ReferenceExpression,
    UnaryExpression, WorkflowExpressionError, parse_workflow_expression,
    validate_workflow_template,
)
from .workflow_budgets import WorkflowBudgets, compose_workflow_budgets


WORKFLOW_SCHEMA_VERSION = 1
WORKFLOW_DIRECTORY = Path(".hal") / "workflows"
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_VALUE_TYPES = frozenset({
    "string", "boolean", "integer", "json", "markdown", "path", "diff",
    "check_result",
})
_DEPENDENCY_POLICIES = frozenset({"all_succeeded", "all_terminal"})
_WORKSPACE_POLICIES = frozenset({"current", "worktree"})
_BUDGET_FIELDS = frozenset({
    "node_attempts", "provider_calls", "tool_calls", "elapsed_seconds",
    "input_tokens", "output_tokens",
})
_OUTPUT_SOURCES = {
    "agent": frozenset({"final_response", "structured_response", "harness_outcome"}),
    "command": frozenset({"result", "stdout", "stderr", "exit_code"}),
    "approval": frozenset({"outcome", "feedback"}),
    "git": frozenset({"result", "commit", "branch", "head"}),
    "publish": frozenset({"result", "provider_id", "url"}),
    "workflow": frozenset({"result"}),
}


class WorkflowEffect(str, Enum):
    """Declared upper bound on the effects of a node implementation."""

    READ = "read"
    MODEL = "model"
    COMMAND_EXECUTION = "command_execution"
    WORKSPACE_MUTATION = "workspace_mutation"
    GIT_MUTATION = "git_mutation"
    APPROVAL = "approval"
    CREDENTIAL_USE = "credential_use"
    NETWORK_ACCESS = "network_access"
    PUBLICATION = "publication"


class WorkflowOrigin(str, Enum):
    BUILTIN = "builtin"
    REPOSITORY = "repository"


class WorkflowRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INTERRUPTED = "interrupted"


class WorkflowNodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INTERRUPTED = "interrupted"


class WorkflowAttemptStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INTERRUPTED = "interrupted"


WORKFLOW_TERMINAL_STATUSES = frozenset({
    WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED,
    WorkflowRunStatus.DENIED, WorkflowRunStatus.CANCELLED,
    WorkflowRunStatus.TIMED_OUT, WorkflowRunStatus.BUDGET_EXHAUSTED,
    WorkflowRunStatus.INTERRUPTED,
})
NODE_TERMINAL_STATUSES = frozenset({
    WorkflowNodeStatus.SUCCEEDED, WorkflowNodeStatus.FAILED,
    WorkflowNodeStatus.SKIPPED, WorkflowNodeStatus.DENIED,
    WorkflowNodeStatus.CANCELLED, WorkflowNodeStatus.TIMED_OUT,
    WorkflowNodeStatus.BUDGET_EXHAUSTED, WorkflowNodeStatus.INTERRUPTED,
})
ATTEMPT_TERMINAL_STATUSES = frozenset({
    WorkflowAttemptStatus.SUCCEEDED, WorkflowAttemptStatus.FAILED,
    WorkflowAttemptStatus.DENIED, WorkflowAttemptStatus.CANCELLED,
    WorkflowAttemptStatus.TIMED_OUT, WorkflowAttemptStatus.BUDGET_EXHAUSTED,
    WorkflowAttemptStatus.INTERRUPTED,
})

WORKFLOW_STATUS_TRANSITIONS: Mapping[WorkflowRunStatus, frozenset[WorkflowRunStatus]] = MappingProxyType({
    WorkflowRunStatus.PENDING: frozenset({WorkflowRunStatus.RUNNING, WorkflowRunStatus.CANCELLED}),
    WorkflowRunStatus.RUNNING: frozenset({
        WorkflowRunStatus.WAITING, WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED,
        WorkflowRunStatus.DENIED, WorkflowRunStatus.CANCELLED, WorkflowRunStatus.TIMED_OUT,
        WorkflowRunStatus.BUDGET_EXHAUSTED, WorkflowRunStatus.INTERRUPTED,
    }),
    WorkflowRunStatus.WAITING: frozenset({
        WorkflowRunStatus.RUNNING, WorkflowRunStatus.DENIED, WorkflowRunStatus.CANCELLED,
        WorkflowRunStatus.TIMED_OUT, WorkflowRunStatus.INTERRUPTED,
    }),
    WorkflowRunStatus.INTERRUPTED: frozenset({WorkflowRunStatus.RUNNING, WorkflowRunStatus.CANCELLED}),
    **{status: frozenset() for status in WORKFLOW_TERMINAL_STATUSES if status is not WorkflowRunStatus.INTERRUPTED},
})
NODE_STATUS_TRANSITIONS: Mapping[WorkflowNodeStatus, frozenset[WorkflowNodeStatus]] = MappingProxyType({
    WorkflowNodeStatus.PENDING: frozenset({
        WorkflowNodeStatus.READY, WorkflowNodeStatus.SKIPPED, WorkflowNodeStatus.CANCELLED,
    }),
    WorkflowNodeStatus.READY: frozenset({
        WorkflowNodeStatus.RUNNING, WorkflowNodeStatus.SKIPPED, WorkflowNodeStatus.CANCELLED,
        WorkflowNodeStatus.BUDGET_EXHAUSTED,
    }),
    WorkflowNodeStatus.RUNNING: frozenset({
        WorkflowNodeStatus.WAITING, WorkflowNodeStatus.SUCCEEDED, WorkflowNodeStatus.FAILED,
        WorkflowNodeStatus.DENIED, WorkflowNodeStatus.CANCELLED, WorkflowNodeStatus.TIMED_OUT,
        WorkflowNodeStatus.BUDGET_EXHAUSTED, WorkflowNodeStatus.INTERRUPTED,
    }),
    WorkflowNodeStatus.WAITING: frozenset({
        WorkflowNodeStatus.RUNNING, WorkflowNodeStatus.DENIED, WorkflowNodeStatus.CANCELLED,
        WorkflowNodeStatus.TIMED_OUT, WorkflowNodeStatus.INTERRUPTED,
    }),
    WorkflowNodeStatus.INTERRUPTED: frozenset({WorkflowNodeStatus.READY, WorkflowNodeStatus.CANCELLED}),
    **{status: frozenset() for status in NODE_TERMINAL_STATUSES if status is not WorkflowNodeStatus.INTERRUPTED},
})
ATTEMPT_STATUS_TRANSITIONS: Mapping[WorkflowAttemptStatus, frozenset[WorkflowAttemptStatus]] = MappingProxyType({
    WorkflowAttemptStatus.PENDING: frozenset({
        WorkflowAttemptStatus.RUNNING, WorkflowAttemptStatus.CANCELLED,
        WorkflowAttemptStatus.BUDGET_EXHAUSTED,
    }),
    WorkflowAttemptStatus.RUNNING: frozenset({
        WorkflowAttemptStatus.WAITING, WorkflowAttemptStatus.SUCCEEDED,
        WorkflowAttemptStatus.FAILED, WorkflowAttemptStatus.DENIED,
        WorkflowAttemptStatus.CANCELLED, WorkflowAttemptStatus.TIMED_OUT,
        WorkflowAttemptStatus.BUDGET_EXHAUSTED, WorkflowAttemptStatus.INTERRUPTED,
    }),
    WorkflowAttemptStatus.WAITING: frozenset({
        WorkflowAttemptStatus.RUNNING, WorkflowAttemptStatus.DENIED,
        WorkflowAttemptStatus.CANCELLED, WorkflowAttemptStatus.TIMED_OUT,
        WorkflowAttemptStatus.INTERRUPTED,
    }),
    WorkflowAttemptStatus.INTERRUPTED: frozenset({
        WorkflowAttemptStatus.PENDING, WorkflowAttemptStatus.CANCELLED,
    }),
    **{status: frozenset() for status in ATTEMPT_TERMINAL_STATUSES if status is not WorkflowAttemptStatus.INTERRUPTED},
})


def require_status_transition(
    current: WorkflowRunStatus | WorkflowNodeStatus | WorkflowAttemptStatus,
    target: WorkflowRunStatus | WorkflowNodeStatus | WorkflowAttemptStatus,
) -> None:
    """Reject illegal or cross-layer workflow state transitions."""
    if type(current) is not type(target):
        raise ValueError(
            f"cannot transition between {type(current).__name__} and {type(target).__name__}"
        )
    transitions = {
        WorkflowRunStatus: WORKFLOW_STATUS_TRANSITIONS,
        WorkflowNodeStatus: NODE_STATUS_TRANSITIONS,
        WorkflowAttemptStatus: ATTEMPT_STATUS_TRANSITIONS,
    }[type(current)]
    if target not in transitions[current]:
        raise ValueError(f"illegal {type(current).__name__} transition: {current.value} -> {target.value}")


@dataclass(frozen=True, slots=True)
class NodeTypeSpec:
    """Trusted metadata and accepted fields for one workflow node kind."""

    name: str
    fields: frozenset[str]
    required_fields: frozenset[str]
    effect: WorkflowEffect
    resumable: bool = False
    idempotent: bool = False
    additional_effects: frozenset[WorkflowEffect] = frozenset()

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.name):
            raise ValueError("node type must match [a-z0-9][a-z0-9_-]*")
        if not self.required_fields <= self.fields:
            raise ValueError(f"node type {self.name!r} requires undeclared fields")

    @property
    def effects(self) -> frozenset[WorkflowEffect]:
        return frozenset({self.effect}) | self.additional_effects


class NodeTypeRegistry:
    """Registry that prevents workflow files from redefining trusted node kinds."""

    def __init__(self, specs: tuple[NodeTypeSpec, ...] | list[NodeTypeSpec] = ()) -> None:
        self._specs: dict[str, NodeTypeSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: NodeTypeSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"workflow node type {spec.name!r} is already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> NodeTypeSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._specs))
            raise ValueError(
                f"unknown workflow node type {name!r} (available: {available})"
            ) from exc

    @property
    def specs(self) -> tuple[NodeTypeSpec, ...]:
        return tuple(self._specs[name] for name in sorted(self._specs))


_BASE_NODE_FIELDS = frozenset({
    "id", "type", "depends_on", "dependency_policy", "condition", "inputs",
    "outputs",
})
BUILTIN_NODE_TYPES = NodeTypeRegistry([
    NodeTypeSpec(
        "agent", frozenset({"capability", "prompt", "fresh_context", "budgets", "loop"}),
        frozenset({"prompt"}), WorkflowEffect.MODEL,
    ),
    NodeTypeSpec(
        "command", frozenset({
            "command", "timeout_seconds", "working_directory", "environment",
            "inherit_environment", "max_output_chars",
        }),
        frozenset({"command"}), WorkflowEffect.COMMAND_EXECUTION,
        additional_effects=frozenset({WorkflowEffect.WORKSPACE_MUTATION}),
    ),
    NodeTypeSpec(
        "approval", frozenset({"prompt", "feedback_output"}),
        frozenset({"prompt"}), WorkflowEffect.APPROVAL, resumable=True, idempotent=True,
    ),
    NodeTypeSpec(
        "git", frozenset({
            "operation", "message", "artifacts", "branch", "from_ref", "to_ref",
            "staged",
        }),
        frozenset({"operation"}), WorkflowEffect.GIT_MUTATION,
    ),
    NodeTypeSpec(
        "publish", frozenset({
            "provider", "operation", "title", "body", "base", "head",
            "remote", "branch", "commit", "approval",
        }),
        frozenset({"provider", "operation"}), WorkflowEffect.PUBLICATION,
        idempotent=True,
        additional_effects=frozenset({
            WorkflowEffect.CREDENTIAL_USE, WorkflowEffect.NETWORK_ACCESS,
        }),
    ),
    NodeTypeSpec(
        "workflow", frozenset({"workflow", "digest"}), frozenset({"workflow", "digest"}),
        WorkflowEffect.MODEL,
        additional_effects=frozenset({WorkflowEffect.WORKSPACE_MUTATION}),
    ),
])


@dataclass(frozen=True, slots=True)
class WorkflowIdentity:
    name: str
    origin: WorkflowOrigin
    digest: str
    repository: Path | None = None


def builtin_workflow_identity(name: str, definition: str) -> WorkflowIdentity:
    """Return a stable identity for one trusted built-in workflow definition."""
    _validate_identifier(name, "built-in workflow name")
    digest = sha256(
        f"hal-builtin-workflow-v{WORKFLOW_SCHEMA_VERSION}\0{name}\0{definition}".encode("utf-8")
    ).hexdigest()
    return WorkflowIdentity(name, WorkflowOrigin.BUILTIN, digest)


@dataclass(frozen=True, slots=True)
class WorkflowSource:
    repository: Path
    path: Path
    relative_path: str
    digest: str

    def identity(self, name: str) -> WorkflowIdentity:
        return WorkflowIdentity(name, WorkflowOrigin.REPOSITORY, self.digest, self.repository)


@dataclass(frozen=True, slots=True)
class WorkflowInputDefinition:
    type: str
    required: bool = False
    has_default: bool = False
    default: Any = None


@dataclass(frozen=True, slots=True)
class WorkflowOutputDefinition:
    type: str
    source: str


@dataclass(frozen=True, slots=True)
class WorkflowNodeInputDefinition:
    type: str
    value: Any


@dataclass(frozen=True, slots=True)
class WorkflowExecution:
    workspace: str = "current"
    max_parallel: int = 1
    timeout_seconds: float | None = None
    budgets: WorkflowBudgets = WorkflowBudgets()


@dataclass(frozen=True, slots=True)
class WorkflowNodeDefinition:
    id: str
    type: str
    depends_on: tuple[str, ...]
    dependency_policy: str
    condition: str
    inputs: Mapping[str, WorkflowNodeInputDefinition]
    outputs: Mapping[str, WorkflowOutputDefinition]
    config: Mapping[str, Any]
    effect: WorkflowEffect
    effects: frozenset[WorkflowEffect]
    resumable: bool
    idempotent: bool


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    version: int
    name: str
    description: str
    inputs: Mapping[str, WorkflowInputDefinition]
    execution: WorkflowExecution
    nodes: tuple[WorkflowNodeDefinition, ...]
    source: WorkflowSource


class _NoAliasSafeLoader(yaml.SafeLoader):
    """Safe loader that also rejects aliases to keep input expansion bounded."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            event = self.get_event()
            raise ComposerError(
                None, None, "YAML aliases are not allowed in workflow definitions",
                event.start_mark,
            )
        return super().compose_node(parent, index)


def workflow_directory(repository: Path) -> Path:
    """Return the canonical repository workflow directory."""
    return repository.resolve() / WORKFLOW_DIRECTORY


def discover_workflow_files(repository: Path) -> tuple[Path, ...]:
    """Discover repository workflow definitions without parsing or executing them."""
    directory = workflow_directory(repository)
    if not directory.is_dir():
        return ()
    return tuple(sorted(
        (path for path in directory.iterdir() if path.is_file() and path.suffix == ".yaml"),
        key=lambda path: path.name,
    ))


def discover_workflows(
    repository: Path, registry: NodeTypeRegistry = BUILTIN_NODE_TYPES,
) -> Mapping[str, WorkflowDefinition]:
    """Load all repository definitions and reject ambiguous workflow identities."""
    definitions: dict[str, WorkflowDefinition] = {}
    for path in discover_workflow_files(repository):
        definition = load_workflow(path, repository, registry)
        if definition.name in definitions:
            other = definitions[definition.name].source.relative_path
            raise ValueError(
                f"duplicate workflow {definition.name!r} in {other} and "
                f"{definition.source.relative_path}"
            )
        definitions[definition.name] = definition
    return MappingProxyType(definitions)


def resolve_workflow_names(
    builtin_names: set[str] | frozenset[str],
    repository_definitions: Mapping[str, WorkflowDefinition],
) -> tuple[str, ...]:
    """Resolve visible names while forbidding silent built-in shadowing."""
    collisions = set(builtin_names) & set(repository_definitions)
    if collisions:
        names = ", ".join(sorted(collisions))
        raise ValueError(f"repository workflows cannot shadow built-in workflow(s): {names}")
    return tuple(sorted(set(builtin_names) | set(repository_definitions)))


def load_workflow(
    path: Path,
    repository: Path | None = None,
    registry: NodeTypeRegistry = BUILTIN_NODE_TYPES,
) -> WorkflowDefinition:
    """Load and strictly validate one inert repository workflow definition."""
    path = path.resolve()
    repository = (repository or path.parent.parent.parent).resolve()
    directory = workflow_directory(repository)
    try:
        relative = path.relative_to(repository).as_posix()
        path.relative_to(directory)
    except ValueError as exc:
        raise ValueError(f"workflow path must be inside {directory}") from exc
    if path.parent != directory or path.suffix != ".yaml":
        raise ValueError(f"workflow path must be a .yaml file directly inside {directory}")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{relative}: workflow must be UTF-8") from exc
    try:
        data = yaml.load(text, Loader=_NoAliasSafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{relative}: invalid YAML: {exc}") from exc
    source = WorkflowSource(repository, path, relative, sha256(raw).hexdigest())
    return parse_workflow_definition(data, source, registry)


def parse_workflow_definition(
    data: Any,
    source: WorkflowSource,
    registry: NodeTypeRegistry = BUILTIN_NODE_TYPES,
) -> WorkflowDefinition:
    """Validate already-decoded workflow data without performing side effects."""
    root = _mapping(data, "workflow")
    _unknown(root, {"version", "name", "description", "inputs", "execution", "nodes"}, "workflow")
    version = root.get("version")
    if isinstance(version, bool) or version != WORKFLOW_SCHEMA_VERSION:
        raise ValueError(
            f"workflow.version must be the integer {WORKFLOW_SCHEMA_VERSION}"
        )
    name = _required_string(root, "name", "workflow")
    _validate_identifier(name, "workflow.name")
    if source.path.stem != name:
        raise ValueError(
            f"workflow.name {name!r} must match file name {source.path.name!r}"
        )
    description = _optional_string(root, "description", "workflow")
    inputs = _parse_inputs(root.get("inputs", {}))
    execution = _parse_execution(root.get("execution", {}))
    raw_nodes = root.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("workflow.nodes must be a non-empty list")
    nodes = tuple(_parse_node(value, index, registry) for index, value in enumerate(raw_nodes))
    _validate_graph(nodes, inputs)
    if any(node.type == "publish" for node in nodes) and execution.workspace != "worktree":
        raise ValueError("publication workflows require execution.workspace: worktree")
    return WorkflowDefinition(
        WORKFLOW_SCHEMA_VERSION, name, description, MappingProxyType(inputs),
        execution, nodes, source,
    )


def _parse_inputs(value: Any) -> dict[str, WorkflowInputDefinition]:
    raw = _mapping(value, "workflow.inputs")
    inputs: dict[str, WorkflowInputDefinition] = {}
    for name, item in raw.items():
        _validate_identifier(name, "workflow input")
        path = f"workflow.inputs.{name}"
        spec = _mapping(item, path)
        _unknown(spec, {"type", "required", "default"}, path)
        type_name = _required_string(spec, "type", path)
        _validate_value_type(type_name, f"{path}.type")
        required = spec.get("required", False)
        if not isinstance(required, bool):
            raise ValueError(f"{path}.required must be true or false")
        has_default = "default" in spec
        if required and has_default:
            raise ValueError(f"{path} cannot be required and have a default")
        default = _freeze(spec.get("default")) if has_default else None
        if has_default:
            _validate_literal_type(default, type_name, f"{path}.default")
        inputs[name] = WorkflowInputDefinition(type_name, required, has_default, default)
    return inputs


def _parse_execution(value: Any) -> WorkflowExecution:
    raw = _mapping(value, "workflow.execution")
    _unknown(raw, {"workspace", "max_parallel", "timeout_seconds", "budgets"}, "workflow.execution")
    workspace = raw.get("workspace", "current")
    if workspace not in _WORKSPACE_POLICIES:
        raise ValueError("workflow.execution.workspace must be 'current' or 'worktree'")
    max_parallel = raw.get("max_parallel", 1)
    if isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or max_parallel <= 0:
        raise ValueError("workflow.execution.max_parallel must be a positive integer")
    timeout = raw.get("timeout_seconds")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
    ):
        raise ValueError("workflow.execution.timeout_seconds must be a positive number or null")
    budgets = _parse_budgets(raw.get("budgets", {}), "workflow.execution.budgets")
    if timeout is not None:
        budgets = compose_workflow_budgets(
            budgets, WorkflowBudgets(elapsed_seconds=timeout),
        ) or budgets
    return WorkflowExecution(workspace, max_parallel, timeout, budgets)


def _parse_budgets(
    value: Any, path: str, *, allow_node_attempts: bool = True,
) -> WorkflowBudgets:
    raw = _mapping(value, path)
    allowed = _BUDGET_FIELDS if allow_node_attempts else _BUDGET_FIELDS - {"node_attempts"}
    _unknown(raw, allowed, path)
    try:
        return WorkflowBudgets(**raw)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _parse_node(value: Any, index: int, registry: NodeTypeRegistry) -> WorkflowNodeDefinition:
    path = f"workflow.nodes[{index}]"
    raw = _mapping(value, path)
    node_id = _required_string(raw, "id", path)
    _validate_identifier(node_id, f"{path}.id")
    node_type = _required_string(raw, "type", path)
    spec = registry.get(node_type)
    _unknown(raw, _BASE_NODE_FIELDS | spec.fields, path)
    missing = spec.required_fields - set(raw)
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"{path} missing required setting(s): {names}")
    depends_value = raw.get("depends_on", [])
    if not isinstance(depends_value, list) or any(
        not isinstance(item, str) or not item for item in depends_value
    ):
        raise ValueError(f"{path}.depends_on must be a list of node IDs")
    depends_on = tuple(depends_value)
    if len(set(depends_on)) != len(depends_on):
        raise ValueError(f"{path}.depends_on must not contain duplicates")
    policy = raw.get("dependency_policy", "all_succeeded")
    if policy not in _DEPENDENCY_POLICIES:
        raise ValueError(
            f"{path}.dependency_policy must be 'all_succeeded' or 'all_terminal'"
        )
    condition = raw.get("condition", "")
    if not isinstance(condition, str):
        raise ValueError(f"{path}.condition must be a string")
    if condition.strip():
        _validate_expression(condition, f"{path}.condition")
    inputs = _parse_node_inputs(raw.get("inputs", {}), f"{path}.inputs")
    outputs = _parse_outputs(raw.get("outputs", {}), f"{path}.outputs")
    for output_name, output in outputs.items():
        if output.source not in _OUTPUT_SOURCES[node_type]:
            available = ", ".join(sorted(_OUTPUT_SOURCES[node_type]))
            raise ValueError(
                f"{path}.outputs.{output_name}.source must be one of: {available}"
            )
    config = {name: _freeze(raw[name]) for name in spec.fields if name in raw}
    if node_type == "command":
        # Command execution is bounded and hermetic by default. Environment
        # inheritance must always be explicitly allow-listed by the workflow.
        config.setdefault("timeout_seconds", 120)
        config.setdefault("working_directory", ".")
        config.setdefault("environment", MappingProxyType({}))
        config.setdefault("inherit_environment", ())
        config.setdefault("max_output_chars", 100_000)
    _validate_builtin_node_config(node_type, config, path)
    _validate_templates_in_value(
        {name: item.value for name, item in inputs.items()}, f"{path}.inputs",
    )
    _validate_templates_in_value(config, path)
    return WorkflowNodeDefinition(
        node_id, node_type, depends_on, policy, condition.strip(),
        MappingProxyType(inputs),
        MappingProxyType(outputs), MappingProxyType(config), spec.effect,
        spec.effects, spec.resumable, spec.idempotent,
    )


def _parse_outputs(value: Any, path: str) -> dict[str, WorkflowOutputDefinition]:
    raw = _mapping(value, path)
    outputs: dict[str, WorkflowOutputDefinition] = {}
    for name, item in raw.items():
        _validate_identifier(name, "workflow output")
        item_path = f"{path}.{name}"
        spec = _mapping(item, item_path)
        _unknown(spec, {"type", "source"}, item_path)
        type_name = _required_string(spec, "type", item_path)
        _validate_value_type(type_name, f"{item_path}.type")
        outputs[name] = WorkflowOutputDefinition(
            type_name, _required_string(spec, "source", item_path),
        )
    return outputs


def _parse_node_inputs(value: Any, path: str) -> dict[str, WorkflowNodeInputDefinition]:
    raw = _mapping(value, path)
    inputs: dict[str, WorkflowNodeInputDefinition] = {}
    for name, item in raw.items():
        _validate_identifier(name, "workflow node input")
        item_path = f"{path}.{name}"
        spec = _mapping(item, item_path)
        _unknown(spec, {"type", "value"}, item_path)
        type_name = _required_string(spec, "type", item_path)
        _validate_value_type(type_name, f"{item_path}.type")
        if "value" not in spec:
            raise ValueError(f"{item_path}.value is required")
        inputs[name] = WorkflowNodeInputDefinition(type_name, _freeze(spec["value"]))
    return inputs


def _validate_builtin_node_config(node_type: str, config: Mapping[str, Any], path: str) -> None:
    if node_type == "agent":
        _nonempty_config_string(config, "prompt", path)
        if "capability" in config:
            _nonempty_config_string(config, "capability", path)
        fresh = config.get("fresh_context", False)
        if not isinstance(fresh, bool):
            raise ValueError(f"{path}.fresh_context must be true or false")
        if "budgets" in config:
            _parse_budgets(
                config["budgets"], f"{path}.budgets", allow_node_attempts=False,
            )
        if "loop" in config:
            loop = _mapping(config["loop"], f"{path}.loop")
            _unknown(loop, {"max_attempts", "timeout_seconds", "until"}, f"{path}.loop")
            if "max_attempts" not in loop and "timeout_seconds" not in loop:
                raise ValueError(f"{path}.loop requires max_attempts or timeout_seconds")
            if "max_attempts" in loop and (
                isinstance(loop["max_attempts"], bool)
                or not isinstance(loop["max_attempts"], int)
                or loop["max_attempts"] <= 0
            ):
                raise ValueError(f"{path}.loop.max_attempts must be a positive integer")
            if "timeout_seconds" in loop and (
                isinstance(loop["timeout_seconds"], bool)
                or not isinstance(loop["timeout_seconds"], (int, float))
                or loop["timeout_seconds"] <= 0
            ):
                raise ValueError(f"{path}.loop.timeout_seconds must be a positive number")
            _nonempty_config_string(loop, "until", f"{path}.loop")
            _validate_expression(str(loop["until"]), f"{path}.loop.until")
    elif node_type == "command":
        command = config.get("command")
        command_map = _mapping(command, f"{path}.command")
        _unknown(command_map, {"argv", "shell", "shell_kind"}, f"{path}.command")
        if ("argv" in command_map) == ("shell" in command_map):
            raise ValueError(f"{path}.command must contain exactly one of argv or shell")
        if "argv" in command_map:
            if "shell_kind" in command_map:
                raise ValueError(f"{path}.command.shell_kind is valid only with shell")
            argv = command_map["argv"]
            if not isinstance(argv, (list, tuple)) or not argv or any(
                not isinstance(item, str) or not item for item in argv
            ):
                raise ValueError(f"{path}.command.argv must be a non-empty list of strings")
        if "shell" in command_map and (
            not isinstance(command_map["shell"], str) or not command_map["shell"].strip()
        ):
            raise ValueError(f"{path}.command.shell must be a non-empty string")
        if "shell" in command_map:
            shell_kind = command_map.get("shell_kind")
            if shell_kind not in {"native", "powershell", "bash", "cmd"}:
                raise ValueError(
                    f"{path}.command.shell_kind must be native, powershell, bash, or cmd"
                )
        if "timeout_seconds" in config and (
            isinstance(config["timeout_seconds"], bool)
            or not isinstance(config["timeout_seconds"], (int, float))
            or config["timeout_seconds"] <= 0
        ):
            raise ValueError(f"{path}.timeout_seconds must be a positive number")
        if "working_directory" in config:
            _nonempty_config_string(config, "working_directory", path)
            working_directory = str(config["working_directory"])
            if (
                PurePosixPath(working_directory).is_absolute()
                or PureWindowsPath(working_directory).is_absolute()
                or ".." in PurePosixPath(working_directory.replace("\\", "/")).parts
            ):
                raise ValueError(f"{path}.working_directory must stay within the workflow workspace")
        if "environment" in config:
            environment = _mapping(config["environment"], f"{path}.environment")
            if any(
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                for name in environment
            ):
                raise ValueError(f"{path}.environment has an invalid variable name")
            if any(not isinstance(item, str) for item in environment.values()):
                raise ValueError(f"{path}.environment values must be strings")
        if "inherit_environment" in config:
            inherited = config["inherit_environment"]
            if not isinstance(inherited, tuple) or any(
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                for name in inherited
            ):
                raise ValueError(f"{path}.inherit_environment must be a list of variable names")
            if len(set(inherited)) != len(inherited):
                raise ValueError(f"{path}.inherit_environment must not contain duplicates")
        if "max_output_chars" in config and (
            isinstance(config["max_output_chars"], bool)
            or not isinstance(config["max_output_chars"], int)
            or config["max_output_chars"] < 512
        ):
            raise ValueError(f"{path}.max_output_chars must be an integer of at least 512")
    elif node_type in {"approval", "publish", "git", "workflow"}:
        for field in {
            "approval": ("prompt",), "publish": ("provider", "operation"),
            "git": ("operation",), "workflow": ("workflow", "digest"),
        }[node_type]:
            _nonempty_config_string(config, field, path)
        optional_strings = {
            "approval": ("feedback_output",),
            "publish": ("title", "body", "base", "head"),
            "git": ("message", "branch", "from_ref", "to_ref"),
            "workflow": (),
        }[node_type]
        for field in optional_strings:
            if field in config and not isinstance(config[field], str):
                raise ValueError(f"{path}.{field} must be a string")
        if node_type == "publish" and config["operation"] not in {"push", "pull_request"}:
            raise ValueError(f"{path}.operation must be push or pull_request")
        if node_type == "workflow" and not re.fullmatch(r"[0-9a-f]{64}", str(config["digest"])):
            raise ValueError(f"{path}.digest must be a lowercase SHA-256 digest")
        if node_type == "publish" and config["operation"] == "push":
            for field in ("remote", "branch", "commit", "approval"):
                _nonempty_config_string(config, field, path)
            extras = set(config) - {
                "provider", "operation", "remote", "branch", "commit", "approval",
            }
            if extras:
                raise ValueError(
                    f"{path}.push does not accept: {', '.join(sorted(extras))}"
                )
            remote = str(config["remote"])
            if "${{" not in remote and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
                raise ValueError(f"{path}.remote must name an allowlisted remote")
        if node_type == "publish" and config["operation"] == "pull_request":
            for field in ("remote", "approval"):
                _nonempty_config_string(config, field, path)
            extras = set(config) - {"provider", "operation", "remote", "approval"}
            if extras:
                raise ValueError(
                    f"{path}.pull_request does not accept: {', '.join(sorted(extras))}"
                )
            remote = str(config["remote"])
            if "${{" not in remote and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
                raise ValueError(f"{path}.remote must name an allowlisted remote")
        if node_type == "git" and "artifacts" in config:
            artifacts = config["artifacts"]
            if not isinstance(artifacts, tuple) or any(
                not isinstance(item, str) or not item for item in artifacts
            ):
                raise ValueError(f"{path}.artifacts must be a list of artifact references")
        if node_type == "git":
            operation = str(config["operation"])
            allowed = {"status", "diff", "stage", "commit", "prepare_branch"}
            if operation not in allowed:
                raise ValueError(
                    f"{path}.operation must be one of: {', '.join(sorted(allowed))}"
                )
            if "staged" in config and not isinstance(config["staged"], bool):
                raise ValueError(f"{path}.staged must be true or false")
            if operation == "stage" and not config.get("artifacts"):
                raise ValueError(f"{path}.artifacts is required for stage")
            if operation == "commit":
                if not config.get("artifacts"):
                    raise ValueError(f"{path}.artifacts is required for commit")
                _nonempty_config_string(config, "message", path)
            if operation == "prepare_branch":
                _nonempty_config_string(config, "branch", path)
                if "${{" not in str(config["branch"]) and (not re.fullmatch(
                    r"(?!.*(?:\.\.|@\{|//))[A-Za-z0-9][A-Za-z0-9._/-]*",
                    str(config["branch"]),
                ) or str(config["branch"]).endswith(("/", ".", ".lock"))):
                    raise ValueError(f"{path}.branch is not a safe Git branch name")
            fields_by_operation = {
                "status": set(),
                "diff": {"artifacts", "from_ref", "to_ref", "staged"},
                "stage": {"artifacts"},
                "commit": {"artifacts", "message"},
                "prepare_branch": {"branch"},
            }
            extras = (set(config) - {"operation"}) - fields_by_operation[operation]
            if extras:
                raise ValueError(
                    f"{path}.{operation} does not accept: {', '.join(sorted(extras))}"
                )


def _validate_graph(
    nodes: tuple[WorkflowNodeDefinition, ...],
    workflow_inputs: Mapping[str, WorkflowInputDefinition],
) -> None:
    by_id: dict[str, WorkflowNodeDefinition] = {}
    for node in nodes:
        if node.id in by_id:
            raise ValueError(f"duplicate workflow node ID {node.id!r}")
        by_id[node.id] = node
    for node in nodes:
        if node.dependency_policy != "all_succeeded" and not node.depends_on:
            raise ValueError(
                f"workflow node {node.id!r} cannot use dependency_policy "
                f"{node.dependency_policy!r} without dependencies"
            )
        for dependency in node.depends_on:
            if dependency == node.id:
                raise ValueError(f"workflow node {node.id!r} cannot depend on itself")
            if dependency not in by_id:
                raise ValueError(
                    f"workflow node {node.id!r} depends on missing node {dependency!r}"
                )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str, trail: tuple[str, ...]) -> None:
        if node_id in visiting:
            start = trail.index(node_id)
            cycle = " -> ".join((*trail[start:], node_id))
            raise ValueError(f"workflow dependency cycle: {cycle}")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in by_id[node_id].depends_on:
            visit(dependency, (*trail, node_id))
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in by_id:
        visit(node_id, ())

    ancestor_cache: dict[str, frozenset[str]] = {}

    def ancestors(node_id: str) -> frozenset[str]:
        cached = ancestor_cache.get(node_id)
        if cached is not None:
            return cached
        result: set[str] = set()
        for dependency in by_id[node_id].depends_on:
            result.add(dependency)
            result.update(ancestors(dependency))
        frozen = frozenset(result)
        ancestor_cache[node_id] = frozen
        return frozen

    for index, node in enumerate(nodes):
        path = f"workflow.nodes[{index}]"
        available_nodes = ancestors(node.id)
        if node.condition:
            expression = parse_workflow_expression(node.condition)
            actual = _infer_expression_type(
                expression, node, by_id, workflow_inputs, available_nodes, f"{path}.condition",
            )
            if actual != "boolean":
                raise ValueError(f"{path}.condition must be a boolean expression")
        for name, item in node.inputs.items():
            _validate_typed_node_input(
                item, node, by_id, workflow_inputs, available_nodes,
                f"{path}.inputs.{name}",
            )
        _validate_static_templates(
            node.config, node, by_id, workflow_inputs, available_nodes, path,
        )
        if node.type == "publish" and node.config.get("operation") == "push":
            approval_id = str(node.config["approval"])
            approval = by_id.get(approval_id)
            if approval is None or approval.type != "approval":
                raise ValueError(
                    f"{path}.approval must name an approval node"
                )
            if approval_id not in available_nodes:
                raise ValueError(
                    f"{path}.approval must be an ancestor of the push node"
                )
            for field in ("remote", "branch", "commit"):
                review_input = approval.inputs.get(field)
                if (
                    review_input is None
                    or review_input.type != "string"
                    or review_input.value != node.config[field]
                ):
                    raise ValueError(
                        f"{path}.approval node must review the exact push {field}"
                    )
        if node.type == "publish" and node.config.get("operation") == "pull_request":
            required_inputs = {
                "title": "string", "body": "markdown", "base": "string",
                "head": "string", "commits": "json", "checks": "check_result",
                "review": "markdown",
            }
            if set(node.inputs) != set(required_inputs):
                missing = set(required_inputs) - set(node.inputs)
                extra = set(node.inputs) - set(required_inputs)
                detail = []
                if missing:
                    detail.append("missing: " + ", ".join(sorted(missing)))
                if extra:
                    detail.append("unexpected: " + ", ".join(sorted(extra)))
                raise ValueError(f"{path}.inputs must define the PR contract ({'; '.join(detail)})")
            for field, type_name in required_inputs.items():
                if node.inputs[field].type != type_name:
                    raise ValueError(f"{path}.inputs.{field} must have type {type_name!r}")
            approval_id = str(node.config["approval"])
            approval = by_id.get(approval_id)
            if approval is None or approval.type != "approval":
                raise ValueError(f"{path}.approval must name an approval node")
            if approval_id not in available_nodes:
                raise ValueError(f"{path}.approval must be an ancestor of the PR node")
            expected_review = {"remote": ("string", node.config["remote"]), **{
                field: (definition.type, definition.value)
                for field, definition in node.inputs.items()
            }}
            for field, (type_name, value) in expected_review.items():
                review_input = approval.inputs.get(field)
                if (
                    review_input is None or review_input.type != type_name
                    or review_input.value != value
                ):
                    raise ValueError(
                        f"{path}.approval node must review the exact PR {field}"
                    )
        if node.type == "agent" and "loop" in node.config:
            loop = _mapping(node.config["loop"], f"{path}.loop")
            expression = parse_workflow_expression(str(loop["until"]))
            actual = _infer_expression_type(
                expression, node, by_id, workflow_inputs, available_nodes,
                f"{path}.loop.until",
            )
            if actual != "boolean":
                raise ValueError(f"{path}.loop.until must be a boolean expression")


def _validate_typed_node_input(
    item: WorkflowNodeInputDefinition,
    node: WorkflowNodeDefinition,
    by_id: Mapping[str, WorkflowNodeDefinition],
    workflow_inputs: Mapping[str, WorkflowInputDefinition],
    available_nodes: frozenset[str],
    path: str,
) -> None:
    value = item.value
    if not isinstance(value, str) or ("${{" not in value and "}}" not in value):
        _validate_literal_type(value, item.type, f"{path}.value")
        return
    expressions = validate_workflow_template(value)
    direct = re.fullmatch(r"\s*\$\{\{.*\}\}\s*", value, re.DOTALL)
    if direct and len(expressions) == 1:
        actual = _infer_expression_type(
            expressions[0], node, by_id, workflow_inputs, available_nodes, f"{path}.value",
        )
    else:
        for expression in expressions:
            _infer_expression_type(
                expression, node, by_id, workflow_inputs, available_nodes, f"{path}.value",
            )
        actual = "string"
    if not _types_compatible(actual, item.type):
        raise ValueError(
            f"{path}.value has type {actual!r}, expected declared type {item.type!r}"
        )


def _validate_static_templates(
    value: Any,
    node: WorkflowNodeDefinition,
    by_id: Mapping[str, WorkflowNodeDefinition],
    workflow_inputs: Mapping[str, WorkflowInputDefinition],
    available_nodes: frozenset[str],
    path: str,
) -> None:
    if isinstance(value, str):
        if "${{" in value or "}}" in value:
            for expression in validate_workflow_template(value):
                _infer_expression_type(
                    expression, node, by_id, workflow_inputs, available_nodes, path,
                )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_static_templates(
                item, node, by_id, workflow_inputs, available_nodes, f"{path}.{key}",
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_static_templates(
                item, node, by_id, workflow_inputs, available_nodes, f"{path}[{index}]",
            )


def _infer_expression_type(
    expression: Expression,
    node: WorkflowNodeDefinition,
    by_id: Mapping[str, WorkflowNodeDefinition],
    workflow_inputs: Mapping[str, WorkflowInputDefinition],
    available_nodes: frozenset[str],
    path: str,
) -> str:
    if isinstance(expression, LiteralExpression):
        if isinstance(expression.value, bool):
            return "boolean"
        if isinstance(expression.value, int):
            return "integer"
        if isinstance(expression.value, str):
            return "string"
        return "json"
    if isinstance(expression, ReferenceExpression):
        return _reference_type(
            expression, node, by_id, workflow_inputs, available_nodes, path,
        )
    if isinstance(expression, UnaryExpression):
        operand = _infer_expression_type(
            expression.operand, node, by_id, workflow_inputs, available_nodes, path,
        )
        if operand != "boolean":
            raise ValueError(f"{path}: operator 'not' requires a boolean operand")
        return "boolean"
    left = _infer_expression_type(
        expression.left, node, by_id, workflow_inputs, available_nodes, path,
    )
    right = _infer_expression_type(
        expression.right, node, by_id, workflow_inputs, available_nodes, path,
    )
    if expression.operator in {"and", "or"} and (left != "boolean" or right != "boolean"):
        raise ValueError(f"{path}: operator {expression.operator!r} requires boolean operands")
    if expression.operator in {"==", "!="} and not (
        _types_compatible(left, right) or _types_compatible(right, left)
    ):
        raise ValueError(
            f"{path}: cannot compare workflow values of type {left!r} and {right!r}"
        )
    return "boolean"


def _reference_type(
    reference: ReferenceExpression,
    node: WorkflowNodeDefinition,
    by_id: Mapping[str, WorkflowNodeDefinition],
    workflow_inputs: Mapping[str, WorkflowInputDefinition],
    available_nodes: frozenset[str],
    path: str,
) -> str:
    parts = reference.path
    rendered = ".".join(parts)
    if parts[0] == "inputs":
        if len(parts) < 2 or parts[1] not in workflow_inputs:
            raise ValueError(f"{path}: unknown workflow input reference {rendered!r}")
        base = workflow_inputs[parts[1]].type
        return _nested_reference_type(base, parts[2:], path, rendered)
    if parts[0] == "nodes":
        if len(parts) < 3 or parts[1] not in by_id:
            raise ValueError(f"{path}: unknown workflow node reference {rendered!r}")
        producer_id = parts[1]
        if producer_id not in available_nodes:
            raise ValueError(
                f"{path}: node {node.id!r} may reference only dependency ancestors; "
                f"{producer_id!r} is unavailable"
            )
        if parts[2] in {"status", "outcome"} and len(parts) == 3:
            return "string"
        if len(parts) < 4 or parts[2] != "outputs":
            raise ValueError(f"{path}: malformed workflow node reference {rendered!r}")
        output = by_id[producer_id].outputs.get(parts[3])
        if output is None:
            raise ValueError(f"{path}: unknown workflow output reference {rendered!r}")
        return _nested_reference_type(output.type, parts[4:], path, rendered)
    if parts[0] == "node":
        if parts == ("node", "status"):
            return "string"
        if len(parts) >= 3 and parts[1] == "outputs":
            output = node.outputs.get(parts[2])
            if output is None:
                raise ValueError(f"{path}: unknown current-node output reference {rendered!r}")
            return _nested_reference_type(output.type, parts[3:], path, rendered)
        raise ValueError(f"{path}: malformed current-node reference {rendered!r}")
    if parts[0] == "attempt":
        if parts == ("attempt", "status"):
            return "string"
        if parts == ("attempt", "index"):
            return "integer"
        raise ValueError(f"{path}: malformed attempt reference {rendered!r}")
    if parts == ("workflow", "status"):
        return "string"
    raise ValueError(f"{path}: malformed workflow reference {rendered!r}")


def _nested_reference_type(
    base: str, remainder: tuple[str, ...], path: str, rendered: str,
) -> str:
    if not remainder:
        return base
    if base not in {"json", "check_result"}:
        raise ValueError(f"{path}: reference {rendered!r} cannot select fields from type {base!r}")
    return "json"


def _types_compatible(actual: str, expected: str) -> bool:
    return (
        actual == expected
        or expected == "json"
        or (expected == "string" and actual in {"markdown", "path", "diff"})
    )


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{path} must be a mapping with string keys")
    return dict(value)


def _unknown(value: Mapping[str, Any], allowed: set[str] | frozenset[str], path: str) -> None:
    unknown = set(value) - set(allowed)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unknown {path} setting(s): {names}")


def _required_string(value: Mapping[str, Any], name: str, path: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{path}.{name} must be a non-empty string")
    return item.strip()


def _optional_string(value: Mapping[str, Any], name: str, path: str) -> str:
    item = value.get(name, "")
    if not isinstance(item, str):
        raise ValueError(f"{path}.{name} must be a string")
    return item.strip()


def _nonempty_config_string(config: Mapping[str, Any], name: str, path: str) -> None:
    value = config.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}.{name} must be a non-empty string")


def _validate_identifier(value: str, path: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{path} must match [a-z0-9][a-z0-9_-]*"
        )


def _validate_value_type(value: str, path: str) -> None:
    if value not in _VALUE_TYPES:
        allowed = ", ".join(sorted(_VALUE_TYPES))
        raise ValueError(f"{path} has unknown type {value!r} (available: {allowed})")


def _validate_literal_type(value: Any, type_name: str, path: str) -> None:
    valid = {
        "string": isinstance(value, str),
        "markdown": isinstance(value, str),
        "path": isinstance(value, str),
        "diff": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "json": True,
        "check_result": isinstance(value, Mapping),
    }[type_name]
    if not valid:
        raise ValueError(f"{path} does not match declared type {type_name!r}")


def _validate_expression(value: str, path: str) -> None:
    try:
        parse_workflow_expression(value)
    except WorkflowExpressionError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _validate_templates_in_value(value: Any, path: str) -> None:
    if isinstance(value, str):
        if "${{" in value or "}}" in value:
            try:
                validate_workflow_template(value)
            except WorkflowExpressionError as exc:
                raise ValueError(f"{path}: {exc}") from exc
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_templates_in_value(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_templates_in_value(item, f"{path}[{index}]")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("workflow values must use string mapping keys")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError(f"unsupported workflow value type: {type(value).__name__}")
