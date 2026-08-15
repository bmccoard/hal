"""Effect classification and digest-pinned trust for repository workflows."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .workflow_schema import (
    WorkflowDefinition, WorkflowEffect, WorkflowIdentity, WorkflowOrigin,
)


TRUST_REQUIRED_EFFECTS = frozenset({
    WorkflowEffect.COMMAND_EXECUTION,
    WorkflowEffect.WORKSPACE_MUTATION,
    WorkflowEffect.GIT_MUTATION,
    WorkflowEffect.CREDENTIAL_USE,
    WorkflowEffect.NETWORK_ACCESS,
    WorkflowEffect.PUBLICATION,
})


def workflow_required_effects(definition: WorkflowDefinition) -> frozenset[WorkflowEffect]:
    """Return the statically declared upper-bound effects of a workflow graph."""
    effects: set[WorkflowEffect] = set()
    for node in definition.nodes:
        effects.update(node.effects)
        if node.type == "agent":
            capability = str(node.config.get("capability") or "inspect").lower()
            effects.add(WorkflowEffect.READ)
            if capability not in {"inspect", "plan"}:
                # Custom capabilities are conservative until inspection resolves
                # their exact tool policy and effects.
                effects.add(WorkflowEffect.WORKSPACE_MUTATION)
    return frozenset(effects)


def workflow_requires_trust(definition: WorkflowDefinition) -> bool:
    return bool(workflow_required_effects(definition) & TRUST_REQUIRED_EFFECTS)


@dataclass(frozen=True, slots=True)
class WorkflowTrustGrant:
    """Approval pinned to one repository, definition digest, and effect set."""

    workflow_name: str
    repository: Path
    definition_digest: str
    effects: frozenset[WorkflowEffect]

    @classmethod
    def for_definition(cls, definition: WorkflowDefinition) -> WorkflowTrustGrant:
        identity = definition.source.identity(definition.name)
        assert identity.repository is not None
        return cls(
            identity.name, identity.repository, identity.digest,
            workflow_required_effects(definition),
        )

    def authorizes(self, definition: WorkflowDefinition) -> bool:
        identity = definition.source.identity(definition.name)
        return (
            identity.origin is WorkflowOrigin.REPOSITORY
            and identity.name == self.workflow_name
            and identity.repository == self.repository.resolve()
            and identity.digest == self.definition_digest
            and workflow_required_effects(definition) <= self.effects
        )


def require_workflow_trust(
    definition: WorkflowDefinition,
    grant: WorkflowTrustGrant | None,
) -> None:
    """Require exact trust for effectful repository definitions."""
    identity: WorkflowIdentity = definition.source.identity(definition.name)
    if identity.origin is WorkflowOrigin.BUILTIN or not workflow_requires_trust(definition):
        return
    if grant is None:
        effects = ", ".join(sorted(
            effect.value for effect in workflow_required_effects(definition) & TRUST_REQUIRED_EFFECTS
        ))
        raise PermissionError(
            f"workflow {definition.name!r} requires repository trust for: {effects}"
        )
    if not grant.authorizes(definition):
        raise PermissionError(
            f"workflow trust is stale or does not authorize {definition.name!r}"
        )
