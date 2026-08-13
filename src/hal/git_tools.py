from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from .cancellation import CancellationToken
from .git import GitBackend, create_git_backend, normalize_paths
from .models import ToolSpec


def sensitive_git_paths(paths: list[str]) -> list[str]:
    sensitive: list[str] = []
    for path in paths:
        normalized = path.replace("\\", "/").casefold()
        name = Path(normalized).name
        if (
            name == ".env" or name.startswith(".env.")
            or name.endswith(".local.yaml")
            or normalized == ".hal/auth.json"
        ):
            sensitive.append(path)
    return sensitive


def _reject_sensitive(paths: list[str], action: str) -> None:
    sensitive = sensitive_git_paths(paths)
    if sensitive:
        raise ValueError(
            f"refusing to {action} local configuration or credentials: "
            + ", ".join(sensitive)
        )


class GitInitTool:
    parallel_safe = False

    def __init__(self, backend: GitBackend) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "git_init",
            "Initialize the current workspace as a new Git repository on main using HAL's configured backend. Use this instead of shell Git or ad hoc Dulwich scripts.",
            {"type": "object", "properties": {}},
        )

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        branch = self.backend.init(cancellation)
        return json.dumps({
            "backend": self.backend.name,
            "branch": branch,
            "root": str(self.backend.root),
            "initialized": True,
        })


class GitStageTool:
    parallel_safe = False

    def __init__(self, backend: GitBackend) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "git_stage",
            "Stage only explicitly listed safe paths. Local config and credential files are refused; omit and ignore them without reading or rewriting their contents.",
            {
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["paths"],
            },
        )

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        paths = normalize_paths(self.backend.root, arguments.get("paths"))
        _reject_sensitive(paths, "stage")
        self.backend.stage(paths, cancellation)
        return json.dumps({"backend": self.backend.name, "paths": paths, "staged": True})


class GitUnstageTool:
    parallel_safe = False

    def __init__(self, backend: GitBackend) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "git_unstage",
            "Remove explicitly listed paths from the staging area without changing working files.",
            {
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["paths"],
            },
        )

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        paths = normalize_paths(self.backend.root, arguments.get("paths"))
        self.backend.unstage(paths, cancellation)
        return json.dumps({"backend": self.backend.name, "paths": paths, "staged": False})


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
            "Show unstaged or staged repository changes without modifying them. Sensitive local config is omitted and must not be inspected through another tool.",
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
        if paths is not None:
            _reject_sensitive(paths, "display a diff for")
        else:
            status = self.backend.status(cancellation)
            candidates = status.staged if staged else status.unstaged
            omitted = sensitive_git_paths(candidates)
            if omitted:
                paths = [path for path in candidates if path not in omitted]
                output = self.backend.diff(staged, paths, cancellation) if paths else ""
                prefix = output or "no matching changes"
                return prefix + "\nomitted sensitive paths: " + ", ".join(omitted)
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
            "Stage only explicitly listed safe paths and create one local commit. Never read or rewrite refused config files, and never push.",
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
        _reject_sensitive(paths, "commit")
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


class GitCheckoutTool:
    parallel_safe = False

    def __init__(self, backend: GitBackend) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "git_checkout",
            "Switch to an existing branch or commit, or create and switch to a new branch. "
            "Use create=true to create the branch before switching.",
            {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "create": {"type": "boolean"},
                },
                "required": ["target"],
            },
        )

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        target = arguments.get("target")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("target must be a non-empty string")
        target = target.strip()
        if target.startswith("-"):
            raise ValueError("target must not begin with '-'")
        if any(c in target for c in {"\0", "\r", "\n"}):
            raise ValueError("target must not contain control characters")
        create = arguments.get("create", False)
        if not isinstance(create, bool):
            raise ValueError("create must be true or false")
        branch = self.backend.checkout(target, create, cancellation)
        return json.dumps({"backend": self.backend.name, "branch": branch, "created": create})


class GitShowTool:
    parallel_safe = True

    def __init__(self, backend: GitBackend) -> None:
        self.backend = backend

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            "git_show",
            "Show a commit, tag, or the contents of a file at a given ref. "
            "Provide ref (e.g. HEAD, a branch name, or a commit SHA). "
            "Optionally provide path to retrieve the file content at that ref.",
            {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["ref"],
            },
        )

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        ref = arguments.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("ref must be a non-empty string")
        ref = ref.strip()
        raw_path = arguments.get("path")
        path: str | None = None
        if raw_path is not None:
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError("path must be a non-empty string")
            normalized = normalize_paths(self.backend.root, [raw_path.strip()])
            path = normalized[0]
            _reject_sensitive([path], "show")
        return self.backend.show(ref, path, cancellation) or "(empty)"


def git_tools(root: Path, preference: str = "auto") -> list[object]:
    backend = create_git_backend(root, preference)
    return [
        GitInitTool(backend), GitStageTool(backend), GitUnstageTool(backend),
        GitStatusTool(backend), GitDiffTool(backend), GitLogTool(backend),
        GitCommitTool(backend), GitPushTool(backend),
        GitCheckoutTool(backend), GitShowTool(backend),
    ]
