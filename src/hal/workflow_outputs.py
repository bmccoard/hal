"""Runtime validation and artifact conversion for declared workflow values."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Mapping

from .workflow_artifacts import (
    WorkflowArtifactHandle, WorkflowArtifactStore,
)
from .workflow_runtime import WorkflowNodeInvocation, WorkflowNodeReceipt
from .workflow_schema import WorkflowNodeStatus


_ARTIFACT_TYPES = frozenset({"markdown", "path", "diff", "check_result"})
_MEDIA_TYPES = {
    "markdown": "text/markdown; charset=utf-8",
    "path": "text/x-workflow-path; charset=utf-8",
    "diff": "text/x-diff; charset=utf-8",
    "check_result": "application/vnd.hal.check-result+json",
}


def validate_node_inputs(invocation: WorkflowNodeInvocation) -> None:
    expected = set(invocation.node.inputs)
    actual = set(invocation.inputs)
    if actual != expected:
        raise ValueError(_mapping_difference("node input", expected, actual))
    for name, definition in invocation.node.inputs.items():
        if not workflow_value_matches(invocation.inputs[name], definition.type):
            raise ValueError(
                f"workflow node input {name!r} does not match declared type {definition.type!r}"
            )


def validate_and_store_node_outputs(
    invocation: WorkflowNodeInvocation,
    receipt: WorkflowNodeReceipt,
    store: WorkflowArtifactStore,
) -> WorkflowNodeReceipt:
    """Fail a nominally successful receipt unless its exact output contract is valid."""
    if receipt.status is not WorkflowNodeStatus.SUCCEEDED:
        return receipt
    expected = set(invocation.node.outputs)
    actual = set(receipt.outputs)
    if actual != expected:
        return WorkflowNodeReceipt(
            WorkflowNodeStatus.FAILED,
            reason=_mapping_difference("node output", expected, actual),
        )
    normalized: dict[str, Any] = {}
    try:
        for name, definition in invocation.node.outputs.items():
            value = receipt.outputs[name]
            if not workflow_value_matches(value, definition.type):
                raise ValueError(
                    f"workflow node output {name!r} does not match declared type "
                    f"{definition.type!r}"
                )
            normalized[name] = (
                _store_artifact(store, value, definition.type, invocation.node.id)
                if definition.type in _ARTIFACT_TYPES else _freeze_json_value(value)
            )
    except (OSError, TypeError, ValueError) as exc:
        return WorkflowNodeReceipt(
            WorkflowNodeStatus.FAILED,
            reason=f"invalid node outputs: {exc}",
        )
    return WorkflowNodeReceipt(
        receipt.status, MappingProxyType(normalized), receipt.outcome, receipt.reason,
        receipt.approval, receipt.external_receipt,
    )


def workflow_value_matches(value: Any, type_name: str) -> bool:
    if isinstance(value, WorkflowArtifactHandle):
        return value.artifact.type == type_name
    return {
        "string": isinstance(value, str),
        "markdown": isinstance(value, str),
        "path": isinstance(value, str) and _safe_relative_path(value),
        "diff": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "json": _is_json_value(value),
        "check_result": isinstance(value, Mapping) and _is_json_value(value),
    }[type_name]


def _store_artifact(
    store: WorkflowArtifactStore, value: Any, type_name: str, producer: str,
) -> WorkflowArtifactHandle:
    if isinstance(value, WorkflowArtifactHandle):
        store.validate(value.artifact)
        return value
    content = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if type_name == "check_result" else str(value)
    )
    return WorkflowArtifactHandle(store.put(
        content, type=type_name, producer=producer, media_type=_MEDIA_TYPES[type_name],
    ))


def _safe_relative_path(value: str) -> bool:
    if not value:
        return False
    normalized = PurePosixPath(value.replace("\\", "/"))
    return not (
        normalized.is_absolute() or PureWindowsPath(value).is_absolute()
        or ".." in normalized.parts
    )


def _is_json_value(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _mapping_difference(label: str, expected: set[str], actual: set[str]) -> str:
    parts = []
    if missing := expected - actual:
        parts.append(f"missing {label}(s): {', '.join(sorted(missing))}")
    if extra := actual - expected:
        parts.append(f"undeclared {label}(s): {', '.join(sorted(extra))}")
    return "; ".join(parts)
