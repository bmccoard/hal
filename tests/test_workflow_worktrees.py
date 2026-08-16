from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from hal.workflow_worktrees import (
    ValidatedWorkflowWorkspace, WorkflowWorkspaceLock, WorkflowWorktreeIdentity,
    cleanup_isolated_worktree, create_isolated_worktree, inspect_worktree,
    preflight_worktree, validate_workspace_claim, validate_worktree_resume,
)


@pytest.mark.skipif(shutil.which("git") is None, reason="native Git is unavailable")
def test_preflight_and_creation_preserve_dirty_source_in_isolated_branch(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args, cwd=repository):
        return subprocess.run(
            [shutil.which("git"), *args], cwd=cwd, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    git("init", "--quiet")
    git("config", "user.name", "HAL Test")
    git("config", "user.email", "hal@example.invalid")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "--quiet", "-m", "base")
    head = git("rev-parse", "HEAD")
    (repository / "tracked.txt").write_text("user change\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("keep me\n", encoding="utf-8")

    preflight = preflight_worktree(
        repository, "wfrun_0123456789abcdef", "feature", minimum_free_bytes=0,
    )
    target = create_isolated_worktree(preflight)
    try:
        assert preflight.head == head
        assert preflight.dirty_paths == ("tracked.txt", "untracked.txt")
        assert target != repository
        assert (target / "tracked.txt").read_text(encoding="utf-8") == "base\n"
        assert not (target / "untracked.txt").exists()
        assert git("rev-parse", "HEAD", cwd=target) == head
        assert git("branch", "--show-current", cwd=target) == preflight.branch
        assert (repository / "tracked.txt").read_text(encoding="utf-8") == "user change\n"
        assert (repository / "untracked.txt").read_text(encoding="utf-8") == "keep me\n"
        identity = inspect_worktree(repository, target)
        stored = {
            "path": str(target), "head": identity.head, "branch": identity.branch,
            "checkpoint_dirty_digest": identity.dirty_digest,
        }
        validate_worktree_resume(stored, identity)
        (target / "tracked.txt").write_text("workflow change\n", encoding="utf-8")
        changed = inspect_worktree(repository, target)
        with pytest.raises(ValueError, match="changed since"):
            validate_worktree_resume(stored, changed)
        with pytest.raises(ValueError, match="dirty workflow worktree"):
            cleanup_isolated_worktree(repository, stored)
        (target / "tracked.txt").write_text("base\n", encoding="utf-8")
        cleanup_isolated_worktree(repository, stored)
        assert not target.exists()
        assert preflight.branch not in git("branch", "--format=%(refname:short)").splitlines()
    finally:
        if target.exists():
            git("worktree", "remove", "--force", str(target))


def test_preflight_rejects_non_git_without_fallback(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("native Git is unavailable")
    with pytest.raises(ValueError, match="requires a Git repository"):
        preflight_worktree(
            tmp_path, "wfrun_0123456789abcdef", "feature", minimum_free_bytes=0,
        )


def test_workspace_lock_is_exclusive_and_owner_checked(tmp_path: Path) -> None:
    directory = tmp_path / "locks"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = WorkflowWorkspaceLock(directory, workspace, "wfrun_first")
    second = WorkflowWorkspaceLock(directory, workspace, "wfrun_second")

    with first:
        assert first.path.is_file()
        with pytest.raises(ValueError, match="is locked"):
            with second:
                pass
    assert not first.path.exists()


def test_workspace_claims_can_only_be_created_after_current_identity_validation(
    tmp_path: Path, monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    repository.mkdir()
    workspace.mkdir()
    identity = WorkflowWorktreeIdentity(
        workspace.resolve(), "abc123", "hal/test", "clean", (), True,
    )
    monkeypatch.setattr("hal.workflow_worktrees.inspect_worktree", lambda *_args: identity)
    stored = {
        "path": str(workspace), "head": "abc123", "branch": "hal/test",
        "checkpoint_dirty_digest": "clean",
    }

    claim = validate_workspace_claim(repository, workspace, stored)

    assert claim.path == workspace.resolve()
    assert claim.repository == repository.resolve()
    assert claim.branch == "hal/test"
    with pytest.raises(TypeError, match="must be created"):
        ValidatedWorkflowWorkspace(workspace, repository, "hal/test")

    stale = WorkflowWorktreeIdentity(
        workspace.resolve(), "changed", "hal/test", "clean", (), True,
    )
    monkeypatch.setattr("hal.workflow_worktrees.inspect_worktree", lambda *_args: stale)
    with pytest.raises(ValueError, match="HEAD changed"):
        validate_workspace_claim(repository, workspace, stored)
