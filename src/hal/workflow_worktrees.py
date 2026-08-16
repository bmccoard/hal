"""Deterministic Git preflight and isolated workflow worktree creation."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import AbstractContextManager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil

from .cancellation import CancellationToken, cancellation_or_default
from .process import run_bounded_process


@dataclass(frozen=True, slots=True)
class WorkflowWorkspacePreflight:
    repository: Path
    git_executable: str
    head: str
    source_branch: str
    dirty_paths: tuple[str, ...]
    target: Path
    branch: str
    available_bytes: int


@dataclass(frozen=True, slots=True)
class WorkflowWorktreeIdentity:
    path: Path
    head: str
    branch: str
    dirty_digest: str
    dirty_paths: tuple[str, ...]
    registered: bool


class ValidatedWorkflowWorkspace:
    """Opaque proof that a scheduler workspace was validated by the host."""

    __slots__ = ("_path", "_repository", "_branch")
    _creation_token = object()

    def __init__(
        self, path: Path, repository: Path, branch: str, *, _token: object = None,
    ) -> None:
        if _token is not self._creation_token:
            raise TypeError(
                "validated workflow workspaces must be created by validate_workspace_claim"
            )
        self._path = path.resolve()
        self._repository = repository.resolve()
        self._branch = branch

    @property
    def path(self) -> Path:
        return self._path

    @property
    def repository(self) -> Path:
        return self._repository

    @property
    def branch(self) -> str:
        return self._branch


def validate_workspace_claim(
    repository: Path,
    workspace: Path,
    stored: dict[str, object],
    cancellation: CancellationToken | None = None,
) -> ValidatedWorkflowWorkspace:
    """Return an opaque scheduler claim only for a current registered worktree."""
    repository, workspace = repository.resolve(), workspace.resolve()
    identity = inspect_worktree(repository, workspace, cancellation)
    validate_worktree_resume(stored, identity)
    return ValidatedWorkflowWorkspace(
        identity.path, repository, identity.branch,
        _token=ValidatedWorkflowWorkspace._creation_token,
    )


def preflight_worktree(
    repository: Path,
    run_id: str,
    workflow_name: str,
    cancellation: CancellationToken | None = None,
    *,
    minimum_free_bytes: int = 512 * 1024 * 1024,
) -> WorkflowWorkspacePreflight:
    cancellation = cancellation_or_default(cancellation)
    repository = repository.resolve()
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("workspace: worktree requires the native Git executable")

    def git(*arguments: str, check: bool = True) -> str:
        result = run_bounded_process(
            [executable, *arguments], repository, cancellation, output_limit=256 * 1024,
        )
        if check and result.returncode:
            raise ValueError((result.stderr or result.stdout).strip() or "Git command failed")
        return result.stdout

    if git("rev-parse", "--is-inside-work-tree", check=False).strip() != "true":
        raise ValueError("workspace: worktree requires a Git repository")
    head = git("rev-parse", "HEAD").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
        raise ValueError("Git HEAD could not be resolved to a commit")
    source_branch = git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
    source_branch = source_branch or "(detached)"
    status = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    dirty_paths = tuple(sorted(
        item[3:].strip().replace("\\", "/")
        for item in status.split("\0") if len(item) >= 4
    ))
    safe_name = re.sub(r"[^a-z0-9-]+", "-", workflow_name.lower()).strip("-") or "workflow"
    suffix = run_id.removeprefix("wfrun_")
    branch = f"hal/{safe_name}/{suffix}"
    result = run_bounded_process(
        [executable, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        repository, cancellation,
    )
    if result.returncode == 0:
        raise ValueError(f"workflow branch already exists: {branch}")
    if result.returncode not in {1}:
        raise ValueError((result.stderr or "could not inspect workflow branch").strip())
    target_root = repository.parent / f".{repository.name}-hal-worktrees"
    target = (target_root / run_id).resolve()
    if target.exists():
        raise ValueError(f"workflow worktree target already exists: {target}")
    worktrees = git("worktree", "list", "--porcelain")
    existing = {
        Path(line[9:]).resolve()
        for line in worktrees.splitlines() if line.startswith("worktree ")
    }
    if target in existing:
        raise ValueError(f"workflow worktree is already registered: {target}")
    available = shutil.disk_usage(repository).free
    if available < minimum_free_bytes:
        raise ValueError(
            f"insufficient disk space for workflow worktree: {available} bytes available, "
            f"{minimum_free_bytes} required"
        )
    return WorkflowWorkspacePreflight(
        repository, executable, head.lower(), source_branch, dirty_paths,
        target, branch, available,
    )


def create_isolated_worktree(
    preflight: WorkflowWorkspacePreflight,
    cancellation: CancellationToken | None = None,
) -> Path:
    cancellation = cancellation_or_default(cancellation)
    cancellation.raise_if_cancelled()
    preflight.target.parent.mkdir(parents=True, exist_ok=True)
    result = run_bounded_process(
        [
            preflight.git_executable, "worktree", "add", "--no-track", "-b",
            preflight.branch, str(preflight.target), preflight.head,
        ],
        preflight.repository, cancellation, timeout=120,
    )
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip() or "could not create worktree")
    if not preflight.target.is_dir():
        raise ValueError("Git reported success but workflow worktree is missing")
    return preflight.target


def inspect_worktree(
    repository: Path, workspace: Path,
    cancellation: CancellationToken | None = None,
) -> WorkflowWorktreeIdentity:
    cancellation = cancellation_or_default(cancellation)
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("worktree validation requires the native Git executable")
    repository, workspace = repository.resolve(), workspace.resolve()
    if not workspace.is_dir():
        raise ValueError(f"workflow worktree is missing: {workspace}")

    def git(cwd: Path, *arguments: str, check: bool = True):
        result = run_bounded_process([executable, *arguments], cwd, cancellation)
        if check and result.returncode:
            raise ValueError((result.stderr or result.stdout).strip() or "Git command failed")
        return result

    top = Path(git(workspace, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != workspace:
        raise ValueError("workflow worktree path was moved, nested, or reused")
    head = git(workspace, "rev-parse", "HEAD").stdout.strip().lower()
    branch = git(
        workspace, "symbolic-ref", "--quiet", "--short", "HEAD", check=False,
    ).stdout.strip() or "(detached)"
    status = git(
        workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all",
    ).stdout
    paths = tuple(sorted(
        item[3:].strip().replace("\\", "/")
        for item in status.split("\0") if len(item) >= 4
    ))
    registered = False
    current_path: Path | None = None
    for line in git(repository, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line[9:]).resolve()
            if current_path == workspace:
                registered = True
    return WorkflowWorktreeIdentity(
        workspace, head, branch, sha256(status.encode("utf-8")).hexdigest(),
        paths, registered,
    )


def validate_worktree_resume(
    stored: dict[str, object], identity: WorkflowWorktreeIdentity,
    *, allow_checkpoint_change: bool = False,
) -> None:
    if Path(str(stored.get("path"))).resolve() != identity.path:
        raise ValueError("workflow worktree path identity changed")
    if not identity.registered:
        raise ValueError("workflow worktree is no longer registered")
    if stored.get("checkpoint_head", stored.get("head")) != identity.head:
        raise ValueError("workflow worktree HEAD changed")
    if stored.get("branch") != identity.branch:
        raise ValueError("workflow worktree branch changed")
    checkpoint = stored.get("checkpoint_dirty_digest")
    if checkpoint is not None and checkpoint != identity.dirty_digest and not allow_checkpoint_change:
        raise ValueError(
            "workflow worktree changed since its last durable checkpoint; explicit retry or "
            "recovery is required"
        )


class WorkflowWorkspaceLock(AbstractContextManager["WorkflowWorkspaceLock"]):
    def __init__(self, directory: Path, workspace: Path, run_id: str) -> None:
        key = sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()
        self.path = directory.resolve() / f"{key}.lock"
        self.workspace = workspace.resolve()
        self.run_id = run_id
        self._held = False

    def __enter__(self) -> "WorkflowWorkspaceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "run_id": self.run_id, "workspace": str(self.workspace), "pid": os.getpid(),
        }).encode("utf-8")
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            owner = self.path.read_text(encoding="utf-8", errors="replace")
            raise ValueError(f"workflow workspace is locked: {owner}") from exc
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._held = True
        return self

    def __exit__(self, *_args: object) -> None:
        if self._held:
            try:
                owner = json.loads(self.path.read_text(encoding="utf-8"))
                if owner.get("run_id") == self.run_id and owner.get("pid") == os.getpid():
                    self.path.unlink()
            finally:
                self._held = False


def cleanup_isolated_worktree(
    repository: Path, stored: dict[str, object],
    cancellation: CancellationToken | None = None,
) -> None:
    workspace = Path(str(stored["path"]))
    identity = inspect_worktree(repository, workspace, cancellation)
    validate_worktree_resume(stored, identity, allow_checkpoint_change=True)
    if identity.dirty_paths:
        raise ValueError("refusing to remove a dirty workflow worktree")
    if identity.head != stored.get("base_head", stored.get("head")):
        raise ValueError("refusing to remove a worktree with generated or unpushed commits")
    executable = shutil.which("git")
    assert executable is not None
    result = run_bounded_process(
        [executable, "worktree", "remove", str(workspace)], repository, cancellation,
    )
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip())
    result = run_bounded_process(
        [executable, "branch", "-d", str(stored["branch"])], repository, cancellation,
    )
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip())
