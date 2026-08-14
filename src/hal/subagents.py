"""Model-facing delegation through trusted, preconfigured child profiles."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .cancellation import CancellationToken
from .harness import SubagentProfile
from .models import ToolSpec
from .tools import Tool, ToolEffect

if TYPE_CHECKING:
    from .agent import Agent


class DelegateTool(Tool):
    """Delegate a bounded task without accepting policy from model arguments."""

    parallel_safe = False
    effect = ToolEffect.EXTERNAL

    def __init__(self, profiles: dict[str, SubagentProfile]) -> None:
        self.profiles = dict(profiles)
        self.parent: Agent | None = None

    def bind_agent(self, agent: Agent) -> None:
        self.parent = agent

    @property
    def spec(self) -> ToolSpec:
        descriptions = "; ".join(
            f"{name}: {profile.description or profile.capability.description}"
            for name, profile in sorted(self.profiles.items())
        )
        return ToolSpec(
            "delegate",
            "Run a trusted bounded subagent profile. " + descriptions,
            {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string", "enum": sorted(self.profiles),
                    },
                    "task": {"type": "string"},
                },
                "required": ["profile", "task"],
                "additionalProperties": False,
            },
        )

    def run(
        self, arguments: dict[str, Any],
        cancellation: CancellationToken | None = None,
    ) -> str:
        if self.parent is None:
            raise RuntimeError("delegate tool is not bound to an active agent")
        name = arguments.get("profile")
        task = arguments.get("task")
        if not isinstance(name, str) or name not in self.profiles:
            available = ", ".join(sorted(self.profiles)) or "none"
            raise ValueError(f"unknown subagent profile {name!r}; available: {available}")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("subagent task must be a non-empty string")
        profile = self.profiles[name]
        text, outcome = self.parent.run_subagent(
            task, profile.capability, profile.budgets,
            model=profile.model or None,
            cancellation=cancellation,
        )
        return json.dumps({
            "profile": name,
            "run_id": outcome.run_id,
            "status": outcome.status.value,
            "result": text,
        })
