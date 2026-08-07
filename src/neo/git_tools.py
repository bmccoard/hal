from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from .cancellation import CancellationToken
from .git import GitBackend, create_git_backend, normalize_paths
from .models import ToolSpec


class GitStatusTool:
    parallel_safe = True

    def __init__(self, backend: GitBackend) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "git_status",
            "Inspect the repository branch and staged, unstaged, and untracked paths.",
            {"type": "object", "properties": {}},
        )

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        return json.dumps({"backend": self.backend.name, **asdict(self.backend.status(cancellation))})


class GitDiffTool:
    parallel_safe = True

    def __init__(self, backend: GitBackend) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "git_diff",
            "Show unstaged or staged repository changes without modifying them.",
            {
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
            },
        )

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        raw_paths = arguments.get("paths")
        paths = normalize_paths(self.backend.root, raw_paths) if raw_paths is not None else None
        staged = arguments.get("staged", False)
        if not isinstance(staged, bool):
            raise ValueError("staged must be true or false")
        output = self.backend.diff(staged, paths, cancellation)
        return output or "no matching changes"


class GitLogTool:
    parallel_safe = True

    def __init__(self, backend: GitBackend) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "git_log",
            "Read recent local commit history.",
            {
                "type": "object",
                "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 50}},
            },
        )

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        count = arguments.get("count", 10)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 50:
            raise ValueError("count must be between 1 and 50")
        return json.dumps([asdict(item) for item in self.backend.log(count, cancellation)])


class GitCommitTool:
    parallel_safe = False

    def __init__(self, backend: GitBackend) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "git_commit",
            "Stage only the explicitly listed paths and create one local commit. Never pushes.",
            {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["message", "paths"],
            },
        )

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        message = arguments.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        paths = normalize_paths(self.backend.root, arguments.get("paths"))
        sensitive = [
            path for path in paths
            if Path(path).name.casefold() in {".env", "neo.yaml"}
            or path.casefold() == ".neo/auth.json"
        ]
        if sensitive:
            raise ValueError(
                "refusing to commit local configuration or credentials: "
                + ", ".join(sensitive)
            )
        commit_id = self.backend.commit(message.strip(), paths, cancellation)
        return json.dumps({
            "backend": self.backend.name,
            "commit": commit_id,
            "message": message.strip(),
            "paths": paths,
            "pushed": False,
        })


class GitPushTool:
    parallel_safe = False

    def __init__(self, backend: GitBackend) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "git_push",
            "Push a local branch to a remote. Use only when the user explicitly requests a push or publish.",
            {
                "type": "object",
                "properties": {
                    "remote": {"type": "string"},
                    "branch": {"type": "string"},
                },
            },
        )

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        remote = arguments.get("remote", "origin")
        branch = arguments.get("branch", "")
        if not isinstance(remote, str) or not remote.strip():
            raise ValueError("remote must be a non-empty string")
        if not isinstance(branch, str):
            raise ValueError("branch must be a string")
        if remote.startswith("-") or branch.startswith("-"):
            raise ValueError("remote and branch must not begin with '-'")
        if any(character in remote + branch for character in {"\0", "\r", "\n"}):
            raise ValueError("remote and branch must not contain control characters")
        result = self.backend.push(remote.strip(), branch.strip(), cancellation)
        return json.dumps({"backend": self.backend.name, "result": result})


def git_tools(root: Path, preference: str = "auto") -> list[object]:
    backend = create_git_backend(root, preference)
    return [
        GitStatusTool(backend), GitDiffTool(backend), GitLogTool(backend),
        GitCommitTool(backend), GitPushTool(backend),
    ]
