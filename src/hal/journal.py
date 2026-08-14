"""Atomic, versioned persistence for completed harness outcomes."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import warnings

from .harness import RunBudgets, RunOutcome, ToolPolicy


JOURNAL_VERSION = 1


class RunJournalStore:
    """Persist sanitized post-run records independently from session transcripts."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (
            directory if directory is not None
            else Path.home() / ".hal" / "sessions" / "runs"
        )

    def save(
        self,
        outcome: RunOutcome,
        policy: ToolPolicy,
        budgets: RunBudgets | None,
        workspace: Path,
        event_count: int,
    ) -> Path:
        """Atomically save a journal containing no prompt or environment data."""
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": JOURNAL_VERSION,
            "run_id": outcome.run_id,
            "parent_run_id": outcome.parent_run_id,
            "workspace": str(workspace),
            "capability": outcome.capability,
            "status": outcome.status.value,
            "reason": outcome.reason,
            "policy": {
                "allowed_tools": (
                    sorted(policy.allowed_tools)
                    if policy.allowed_tools is not None else None
                ),
                "denied_tools": sorted(policy.denied_tools),
                "protect_existing_files": policy.protect_existing_files,
            },
            "budgets": asdict(budgets) if budgets is not None else None,
            "counters": asdict(outcome.counters),
            "verification": [
                {**asdict(result), "status": result.status.value}
                for result in outcome.verification
            ],
            "repair_attempts": outcome.repair_attempts,
            "event_count": event_count,
            "children": [
                {
                    "run_id": child.run_id,
                    "capability": child.capability,
                    "status": child.status.value,
                    "reason": child.reason,
                    "counters": asdict(child.counters),
                    "repair_attempts": child.repair_attempts,
                }
                for child in outcome.child_outcomes
            ],
        }
        path = self.directory / f"{outcome.run_id}.json"
        self._atomic(path, payload)
        return path

    def load(self, run_id: str) -> dict[str, object] | None:
        """Load one journal; warn and skip corrupt or unsupported records."""
        if not run_id.startswith("run_") or any(char in run_id for char in "/\\"):
            raise ValueError(f"invalid run id {run_id!r}")
        path = self.directory / f"{run_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != JOURNAL_VERSION:
                raise ValueError("unsupported journal version")
            if payload.get("run_id") != run_id:
                raise ValueError("journal run id mismatch")
            return payload
        except FileNotFoundError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            warnings.warn(f"could not read run journal {path.name}: {exc}", RuntimeWarning)
            return None

    @staticmethod
    def _atomic(path: Path, payload: dict[str, object]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
