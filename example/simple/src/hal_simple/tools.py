"""A few small tools demonstrating HAL's extension interface."""
from __future__ import annotations

import json
from typing import Any

from hal.cancellation import CancellationToken, cancellation_or_default
from hal.extensions import ExtensionContext
from hal.models import ToolSpec
from hal.tools import Tool


class EchoTool(Tool):
    parallel_safe = True

    def __init__(self, greeting: str) -> None:
        self.greeting = greeting

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "simple_echo",
            "Echo a message with the configured greeting.",
            {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        )

    def run(
        self,
        arguments: dict[str, Any],
        cancellation: CancellationToken | None = None,
    ) -> str:
        cancellation_or_default(cancellation).raise_if_cancelled()
        message = arguments.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        return f"{self.greeting}, {message.strip()}"


class AddTool(Tool):
    parallel_safe = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "simple_add",
            "Add two numbers.",
            {
                "type": "object",
                "properties": {
                    "left": {"type": "number"},
                    "right": {"type": "number"},
                },
                "required": ["left", "right"],
            },
        )

    def run(
        self,
        arguments: dict[str, Any],
        cancellation: CancellationToken | None = None,
    ) -> str:
        cancellation_or_default(cancellation).raise_if_cancelled()
        left, right = arguments.get("left"), arguments.get("right")
        if isinstance(left, bool) or not isinstance(left, (int, float)):
            raise ValueError("left must be a number")
        if isinstance(right, bool) or not isinstance(right, (int, float)):
            raise ValueError("right must be a number")
        return json.dumps({"left": left, "right": right, "sum": left + right})


class ProjectInfoTool(Tool):
    parallel_safe = True

    def __init__(self, context: ExtensionContext) -> None:
        self.context = context

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "simple_project_info",
            "Show the extension name and HAL working directories.",
            {"type": "object", "properties": {}},
        )

    def run(
        self,
        arguments: dict[str, Any],
        cancellation: CancellationToken | None = None,
    ) -> str:
        cancellation_or_default(cancellation).raise_if_cancelled()
        return json.dumps({
            "extension": self.context.name,
            "cwd": str(self.context.cwd),
            "workspace_root": str(self.context.root),
        })


def create_tools(context: ExtensionContext) -> list[Tool]:
    """Entry-point factory called by HAL when this extension is enabled."""
    greeting = str(context.settings.get("greeting") or "Hello")
    return [EchoTool(greeting), AddTool(), ProjectInfoTool(context)]
